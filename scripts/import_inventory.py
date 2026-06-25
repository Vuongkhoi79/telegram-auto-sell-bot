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
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from openpyxl import load_workbook

from repository.store_repository import ACCOUNT_PRODUCT_CODE_ALIASES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "database" / "store.db"
REQUIRED_COLUMNS = (
    "product_code", "category", "product_name", "account_type", "duration",
    "price_vnd", "warranty_days", "credential_text", "note", "active",
)
OPTIONAL_COLUMNS = ("category_key", "menu_order", "show_in_menu", "product_group", "description", "email", "password", "2fa", "recovery_email")
IMPORT_COLUMNS = tuple(dict.fromkeys((*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)))
ALLOWED_PRODUCT_CODES = {
    "CHATGPT",
    "GEMINI",
    "GROK",
    "CAPCUT",
    "CLAUDE",
    "CURSOR",
    "CANVA",
    "ADOBE",
    "ARTLIST",
    "ELEVEN",
    "GAMMA",
    "HEYGEN",
    "HIGGSFIELD",
    "KLING",
    "KREA",
    "OPENART",
    "SUNO",
    "VEO3",
    "VIEWMAX",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def validate_credential(value: Any) -> str:
    credential = text(value)
    parts = credential.split("|")
    if len(parts) not in {2, 3, 4} or any(not part.strip() for part in parts):
        raise ValueError("credential_text must be email|password, email|password|2fa, or email|password|2fa|recovery_email")
    return credential


def credential_from_row(row: dict[str, Any]) -> str:
    credential_text = text(row.get("credential_text"))
    if credential_text:
        return validate_credential(credential_text)

    email = text(row.get("email"))
    password = text(row.get("password"))
    two_factor = text(row.get("2fa"))
    recovery_email = text(row.get("recovery_email"))
    if not email or not password:
        raise ValueError("credential_text or email/password is required")
    parts = [email, password]
    if two_factor or recovery_email:
        parts.append(two_factor)
    if recovery_email:
        parts.append(recovery_email)
    return validate_credential("|".join(parts))


def validate_product_code(product_code: str) -> None:
    if product_code not in ALLOWED_PRODUCT_CODES:
        allowed = ", ".join(sorted(ALLOWED_PRODUCT_CODES))
        raise ValueError(f"Unsupported product_code: {product_code}. Allowed product_code values: {allowed}")


def product_code_candidates(product_code: str) -> list[str]:
    candidates = [product_code]
    for alias in ACCOUNT_PRODUCT_CODE_ALIASES.get(product_code, ()):
        alias = alias.upper()
        if alias not in candidates:
            candidates.append(alias)
    return candidates


def find_existing_product(connection: sqlite3.Connection, product_code: str) -> sqlite3.Row | None:
    for candidate in product_code_candidates(product_code):
        row = connection.execute(
            "SELECT id, code FROM products WHERE code = ? AND active = 1",
            (candidate,),
        ).fetchone()
        if row:
            return row
    return None


def read_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("CSV has no header row")
            fields = {field.strip().lower() for field in reader.fieldnames if field}
            if "product_code" not in fields:
                raise ValueError("Missing required column: product_code")
            if "credential_text" not in fields and not {"email", "password"}.issubset(fields):
                raise ValueError("Missing credential columns: use credential_text or email/password")
            for row_number, row in enumerate(reader, start=2):
                normalized_row = {str(key).strip().lower(): value for key, value in row.items() if key}
                yield row_number, {key: normalized_row.get(key, "") for key in IMPORT_COLUMNS}
        return
    if suffix != ".xlsx":
        raise ValueError("Input must be an .xlsx or .csv file")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        header = [text(value).lower() for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        positions = {name: index for index, name in enumerate(header) if name}
        if "product_code" not in positions:
            raise ValueError("Missing required column: product_code")
        if "credential_text" not in positions and not {"email", "password"}.issubset(positions):
            raise ValueError("Missing credential columns: use credential_text or email/password")
        for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if not any(value is not None and text(value) for value in values):
                continue
            yield row_number, {
                column: values[positions[column]] if column in positions and positions[column] < len(values) else ""
                for column in IMPORT_COLUMNS
            }
    finally:
        workbook.close()


def import_inventory(input_path: Path, database_path: Path = DEFAULT_DATABASE) -> dict[str, Any]:
    if not database_path.is_file():
        raise FileNotFoundError(f"Store database not found: {database_path}")
    report: dict[str, Any] = {
        "products_created": 0, "products_updated": 0, "credentials_added": 0,
        "credentials_duplicate": 0, "row_errors": 0, "invalid_rows": 0,
        "errors": [], "stock": {},
    }
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        for row_index, (row_number, row) in enumerate(read_rows(input_path), start=1):
            savepoint = f"inventory_row_{row_index}"
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                product_code = text(row["product_code"]).upper()
                if not product_code:
                    raise ValueError("product_code is required")
                validate_product_code(product_code)
                credential = credential_from_row(row)
                now = utc_now_iso()
                product = find_existing_product(connection, product_code)
                if not product:
                    raise ValueError(f"Unknown product_code: {product_code}. Product must exist in products; import did not create it.")
                product_id = product["id"]

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
        assert report["products_created"] == 0, report
        assert report["credentials_added"] == 3, report
        assert report["credentials_duplicate"] == 0, report
        assert report["row_errors"] == 0, report
        assert {code: report["stock"][code] for code in ("GPT-PLUS-1M-PRIVATE", "GEM-AIPRO-1M-PRIVATE", "GROK-SUPER-1M-PRIVATE")} == {
            "GPT-PLUS-1M-PRIVATE": 1,
            "GEM-AIPRO-1M-PRIVATE": 1,
            "GROK-SUPER-1M-PRIVATE": 1,
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
