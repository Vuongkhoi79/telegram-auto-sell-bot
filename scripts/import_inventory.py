"""Import account inventory from an .xlsx or .csv file into database/store.db.

This script is not wired into the Telegram bot.  Run with --help for usage.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from openpyxl import load_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "database" / "store.db"
REQUIRED_COLUMNS = (
    "product_code", "category", "product_name", "account_type", "duration",
    "price_vnd", "warranty_days", "credential_text", "note", "active",
)
PRODUCT_IMPORT_COLUMNS = {
    "category": "TEXT NOT NULL DEFAULT ''",
    "account_type": "TEXT NOT NULL DEFAULT ''",
    "duration": "TEXT NOT NULL DEFAULT ''",
    "price_vnd": "INTEGER NOT NULL DEFAULT 0",
    "warranty_days": "INTEGER NOT NULL DEFAULT 0",
    "note": "TEXT NOT NULL DEFAULT ''",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_int(value: Any, column: str, *, default: int = 0) -> int:
    raw = text(value)
    if not raw:
        return default
    try:
        return int(float(raw.replace(",", "")))
    except ValueError as exc:
        raise ValueError(f"{column} must be an integer") from exc


def parse_active(value: Any) -> int:
    raw = text(value).lower()
    if raw in {"", "1", "true", "yes", "y", "active", "on"}:
        return 1
    if raw in {"0", "false", "no", "n", "inactive", "off"}:
        return 0
    raise ValueError("active must be true/false or 1/0")


def validate_credential(value: Any) -> str:
    credential = text(value)
    parts = credential.split("|")
    if len(parts) not in {2, 3, 4} or any(not part.strip() for part in parts):
        raise ValueError("credential_text must be email|password, email|password|2fa, or email|password|2fa|recovery_email")
    return credential


def read_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("CSV has no header row")
            missing = set(REQUIRED_COLUMNS) - {field.strip() for field in reader.fieldnames if field}
            if missing:
                raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
            for row_number, row in enumerate(reader, start=2):
                yield row_number, {key: row.get(key, "") for key in REQUIRED_COLUMNS}
        return
    if suffix != ".xlsx":
        raise ValueError("Input must be an .xlsx or .csv file")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        header = [text(value) for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        positions = {name: index for index, name in enumerate(header) if name}
        missing = set(REQUIRED_COLUMNS) - set(positions)
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
        for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if not any(value is not None and text(value) for value in values):
                continue
            yield row_number, {
                column: values[positions[column]] if positions[column] < len(values) else ""
                for column in REQUIRED_COLUMNS
            }
    finally:
        workbook.close()


def ensure_import_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    for name, definition in PRODUCT_IMPORT_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")


def import_inventory(input_path: Path, database_path: Path = DEFAULT_DATABASE) -> dict[str, Any]:
    if not database_path.is_file():
        raise FileNotFoundError(f"Store database not found: {database_path}")
    report: dict[str, Any] = {
        "products_created": 0, "products_updated": 0, "credentials_added": 0,
        "credentials_duplicate": 0, "row_errors": 0, "invalid_rows": 0,
        "errors": [], "stock": {},
    }
    created_product_codes: set[str] = set()
    updated_product_codes: set[str] = set()
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_import_schema(connection)
        for row_index, (row_number, row) in enumerate(read_rows(input_path), start=1):
            savepoint = f"inventory_row_{row_index}"
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                product_code = text(row["product_code"]).upper()
                product_name = text(row["product_name"])
                if not product_code or not product_name:
                    raise ValueError("product_code and product_name are required")
                credential = validate_credential(row["credential_text"])
                price_vnd = parse_int(row["price_vnd"], "price_vnd")
                warranty_days = parse_int(row["warranty_days"], "warranty_days")
                if price_vnd < 0 or warranty_days < 0:
                    raise ValueError("price_vnd and warranty_days must not be negative")
                now = utc_now_iso()
                product = connection.execute("SELECT id FROM products WHERE code = ?", (product_code,)).fetchone()
                product_values = (
                    product_name, text(row["category"]), text(row["account_type"]), text(row["duration"]),
                    price_vnd, warranty_days, text(row["note"]), parse_active(row["active"]), now,
                )
                if product:
                    product_id = product["id"]
                    connection.execute(
                        """
                        UPDATE products SET name = ?, category = ?, account_type = ?, duration = ?,
                            price_vnd = ?, warranty_days = ?, note = ?, active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (*product_values, product_id),
                    )
                    product_was_updated = product_code not in created_product_codes
                else:
                    product_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO products
                            (id, code, name, active, delivery_type, created_at, updated_at,
                             category, account_type, duration, price_vnd, warranty_days, note)
                        VALUES (?, ?, ?, ?, 'account', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (product_id, product_code, product_name, product_values[7], now, now, *product_values[1:7]),
                    )
                    product_was_created = True
                    product_was_updated = False

                exists = connection.execute(
                    "SELECT 1 FROM inventory_items WHERE product_id = ? AND secret_value = ?",
                    (product_id, credential),
                ).fetchone()
                if exists:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    report["credentials_duplicate"] += 1
                    if product:
                        if product_was_updated:
                            updated_product_codes.add(product_code)
                    else:
                        created_product_codes.add(product_code)
                    continue
                item_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                    (item_id, product_id, credential, now),
                )
                connection.execute(
                    """
                    INSERT INTO inventory_movements
                        (id, inventory_item_id, action, source, created_at)
                    VALUES (?, ?, 'import', 'import_inventory', ?)
                    """,
                    (str(uuid.uuid4()), item_id, now),
                )
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                report["credentials_added"] += 1
                if product:
                    if product_was_updated:
                        updated_product_codes.add(product_code)
                else:
                    created_product_codes.add(product_code)
            except Exception as exc:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                report["row_errors"] += 1
                report["invalid_rows"] += 1
                report["errors"].append(f"row {row_number}: {exc}")
        stock_rows = connection.execute(
            """
            SELECT p.code, COUNT(i.id) AS stock
            FROM products AS p
            LEFT JOIN inventory_items AS i ON i.product_id = p.id AND i.status = 'available'
            GROUP BY p.id, p.code ORDER BY p.code
            """
        ).fetchall()
        report["stock"] = {row["code"]: int(row["stock"]) for row in stock_rows}
    report["products_created"] = len(created_product_codes)
    report["products_updated"] = len(updated_product_codes)
    return report


def print_report(report: dict[str, Any]) -> None:
    print(f"Products created: {report['products_created']}")
    print(f"Products updated: {report['products_updated']}")
    print(f"Credentials added: {report['credentials_added']}")
    print(f"Credentials duplicate: {report['credentials_duplicate']}")
    print(f"Rows with errors: {report['row_errors']}")
    print(f"Invalid rows: {report['invalid_rows']}")
    print("Stock by product_code:")
    for code, stock in report["stock"].items():
        print(f"  {code}: {stock}")
    for error in report["errors"]:
        print(f"Error: {error}", file=sys.stderr)


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "store.db"
        shutil.copy2(DEFAULT_DATABASE, database)
        source = root / "inventory.csv"
        rows = [
            REQUIRED_COLUMNS,
            ["GEMINI", "AI", "Gemini", "personal", "30D", "99000", "7", "gemini@example.com|pass", "test", "true"],
            ["CHATGPT", "AI", "ChatGPT", "personal", "30D", "199000", "7", "chatgpt@example.com|pass|2fa", "test", "true"],
            ["GROK", "AI", "Grok", "personal", "30D", "299000", "7", "grok@example.com|pass|2fa|recovery@example.com", "test", "true"],
        ]
        with source.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        report = import_inventory(source, database)
        assert report["products_created"] == 3, report
        assert report["credentials_added"] == 3, report
        assert report["credentials_duplicate"] == 0, report
        assert report["row_errors"] == 0, report
        assert {code: report["stock"][code] for code in ("CHATGPT", "GEMINI", "GROK")} == {
            "CHATGPT": 1,
            "GEMINI": 1,
            "GROK": 1,
        }, report
        print_report(report)
        print("SELF-TEST: PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import account inventory from .xlsx or .csv")
    parser.add_argument("input", nargs="?", type=Path, help="Excel or CSV source file")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to store.db")
    parser.add_argument("--self-test", action="store_true", help="Run isolated Gemini/ChatGPT/Grok import test")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.input:
        parser.error("input is required unless --self-test is used")
    report = import_inventory(args.input, args.database)
    print_report(report)
    return 1 if report["row_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
