"""Safely add the 2026-08-10 CAPCUT_30D NEW8 lot as a new inventory batch.

This migration embeds only SHA-256 fingerprints of the verified source
credentials. It does not store emails, passwords, or full secret values.

Default mode is dry-run. In apply mode, the script copies secret_value from
historical CAPCUT_30D rows with matching fingerprints into new inventory rows,
so old orders, payments, and movements remain intact.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_inventory import credential_from_row, normalize_import_product_code, read_rows, text


DEFAULT_DATABASE = Path("/var/data/store.db")
TARGET_PRODUCT_CODE = "CAPCUT_30D"
BATCH_CODE = "CAPCUT_30D_20260810_NEW8"
EXPECTED_CREDENTIAL_COUNT = 8
TARGET_NAME = "CAPCUT PRO 30 ngày"
TARGET_CATEGORY = "ACCOUNT"
TARGET_ACCOUNT_TYPE = "personal"
TARGET_DURATION = "30D"
TARGET_PRICE_VND = 45000
TARGET_WARRANTY_DAYS = 30

# SHA-256 of the full credentials verified from the local CAPCUT_30D 45k file.
# Do not add email, password, credential_text, or secret_value here.
EMBEDDED_CREDENTIAL_FINGERPRINTS = (
    "530f8076809e6a7d29a610b072ada61563b796be258e0b15021f2c69bd0d1e3d",
    "f4c8cd70b21aafd6e4935319fa7fdfd12fc91794b4a53bf21102bf285cafb57e",
    "c27cc3ff6eea1d329c35002943d5eb77aec5eab4efe66b5d341e77c43dd7388f",
    "c780b5e99acc795af6945323169ddc20648127cd9a433dd7ae48f6dd61014c27",
    "44ec8614e09a392de9f4cbd3c63b14b8eb5b18596323025295f890ddb62b8b17",
    "f58d60f4b7c3afa6aa061be9f2eac536d795fd3b5444167a72d56b79647998d6",
    "1175ecf83e4d5c9c2d1e8873f0652255cc972fc2f327ea27a74040a67461ad0d",
    "427b27d1bb95c17aa693ed9f877c31041619686104be36bc2c3a087b17adf282",
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_fingerprint(value: str) -> str:
    return f"{value[:12]}...{value[-8:]}"


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(connection, table_name):
        return set()
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table_name})")}


def read_excel_fingerprints(path: Path) -> tuple[str, ...]:
    rows = list(read_rows(path))
    fingerprints: list[str] = []
    for _row_number, row in rows:
        product_code = normalize_import_product_code(text(row.get("product_code")), row)
        row["product_code"] = product_code
        if product_code != TARGET_PRODUCT_CODE:
            raise SystemExit(f"Unexpected product_code after normalization: {product_code}")
        fingerprints.append(fingerprint(credential_from_row(row)))
    return tuple(fingerprints)


def verify_excel(path: Path) -> None:
    excel_fingerprints = read_excel_fingerprints(path)
    embedded_set = set(EMBEDDED_CREDENTIAL_FINGERPRINTS)
    excel_set = set(excel_fingerprints)
    print(f"excel_verify_path={path}")
    print(f"excel_credentials={len(excel_fingerprints)}")
    print(f"excel_unique_credentials={len(excel_set)}")
    if len(excel_fingerprints) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit(f"Expected {EXPECTED_CREDENTIAL_COUNT} Excel credentials, got {len(excel_fingerprints)}")
    if len(excel_set) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Excel contains duplicate credentials")
    if excel_set != embedded_set:
        missing = sorted(embedded_set - excel_set)
        extra = sorted(excel_set - embedded_set)
        print("excel_verify=failed")
        print(f"missing_embedded={','.join(short_fingerprint(item) for item in missing) or '-'}")
        print(f"extra_excel={','.join(short_fingerprint(item) for item in extra) or '-'}")
        raise SystemExit("Excel fingerprints do not match embedded fingerprints")
    print("excel_verify=ok")


def secret_duplicate_allowed(connection: sqlite3.Connection) -> bool:
    for row in connection.execute("PRAGMA index_list(inventory_items)").fetchall():
        if not int(row["unique"] or 0):
            continue
        index_name = str(row["name"] or "")
        indexed_columns = [
            str(col["name"] or "")
            for col in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        ]
        if "secret_value" in indexed_columns:
            return False
    return True


def stock_summary(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT i.status, COUNT(*) AS qty
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE p.code = ?
        GROUP BY i.status
        """,
        (TARGET_PRODUCT_CODE,),
    ).fetchall()
    return {str(row["status"]): int(row["qty"] or 0) for row in rows}


def product_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM products WHERE code = ?", (TARGET_PRODUCT_CODE,)).fetchone()


def select_inventory_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    inventory_cols = table_columns(connection, "inventory_items")

    def col(name: str, alias: str) -> str:
        return f"i.{name} AS {alias}" if name in inventory_cols else f"'' AS {alias}"

    return connection.execute(
        f"""
        SELECT
            i.id AS inventory_item_id,
            i.secret_value AS secret_value,
            p.id AS product_id,
            p.code AS product_code,
            {col("status", "status")},
            {col("created_at", "created_at")},
            {col("reserved_order_id", "reserved_order_id")},
            {col("delivered_order_id", "delivered_order_id")},
            {col("reserved_at", "reserved_at")},
            {col("delivered_at", "delivered_at")}
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        ORDER BY p.code, i.created_at, i.id
        """
    ).fetchall()


def batch_item_ids(connection: sqlite3.Connection) -> set[str]:
    if not table_exists(connection, "inventory_movements"):
        return set()
    movement_cols = table_columns(connection, "inventory_movements")
    if not {"inventory_item_id", "source"}.issubset(movement_cols):
        return set()
    return {
        str(row["inventory_item_id"])
        for row in connection.execute(
            "SELECT inventory_item_id FROM inventory_movements WHERE source = ?",
            (BATCH_CODE,),
        ).fetchall()
    }


def choose_source_rows(rows_by_fp: dict[str, list[sqlite3.Row]]) -> tuple[dict[str, sqlite3.Row], list[str]]:
    chosen: dict[str, sqlite3.Row] = {}
    errors: list[str] = []
    for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
        rows = rows_by_fp.get(embedded_fp, [])
        target_rows = [row for row in rows if str(row["product_code"] or "").upper() == TARGET_PRODUCT_CODE]
        if not target_rows:
            errors.append(f"{short_fingerprint(embedded_fp)}: no CAPCUT_30D source row found")
            continue
        delivered = [row for row in target_rows if str(row["status"] or "").lower() == "delivered"]
        chosen[embedded_fp] = (delivered or target_rows)[0]
    return chosen, errors


def ensure_product(connection: sqlite3.Connection, now: str) -> str:
    products_cols = table_columns(connection, "products")
    row = product_row(connection)
    if row:
        assignments: list[str] = []
        values: list[Any] = []
        updates = {
            "name": TARGET_NAME,
            "active": 1,
            "category": TARGET_CATEGORY,
            "account_type": TARGET_ACCOUNT_TYPE,
            "duration": TARGET_DURATION,
            "price_vnd": TARGET_PRICE_VND,
            "warranty_days": TARGET_WARRANTY_DAYS,
            "updated_at": now,
            "delivery_type": "account",
            "show_in_menu": 1,
        }
        for column, value in updates.items():
            if column in products_cols:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(row["id"])
        if assignments:
            connection.execute(f"UPDATE products SET {', '.join(assignments)} WHERE id = ?", values)
        return str(row["id"])

    product_id = str(uuid.uuid4())
    columns = ["id", "code", "name", "active", "delivery_type", "created_at", "updated_at"]
    values = [product_id, TARGET_PRODUCT_CODE, TARGET_NAME, 1, "account", now, now]
    optional_values = {
        "category": TARGET_CATEGORY,
        "account_type": TARGET_ACCOUNT_TYPE,
        "duration": TARGET_DURATION,
        "price_vnd": TARGET_PRICE_VND,
        "warranty_days": TARGET_WARRANTY_DAYS,
        "show_in_menu": 1,
    }
    for column, value in optional_values.items():
        if column in products_cols:
            columns.append(column)
            values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO products ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return product_id


def print_plan(
    connection: sqlite3.Connection,
    rows_by_fp: dict[str, list[sqlite3.Row]],
    chosen: dict[str, sqlite3.Row],
    existing_batch_ids: set[str],
    stock_before: dict[str, int],
    duplicate_allowed: bool,
    errors: list[str],
) -> None:
    print("mode=dry_run")
    print(f"target_product_code={TARGET_PRODUCT_CODE}")
    print(f"batch_code={BATCH_CODE}")
    print(f"embedded_fingerprints_count={len(EMBEDDED_CREDENTIAL_FINGERPRINTS)}")
    print(f"schema_duplicate_secret_allowed={str(duplicate_allowed).lower()}")
    print(
        "stock_before="
        f"available={stock_before.get('available', 0)} "
        f"reserved={stock_before.get('reserved', 0)} "
        f"delivered={stock_before.get('delivered', 0)} "
        f"disabled={stock_before.get('disabled', 0)}"
    )
    print(f"existing_batch_items={len(existing_batch_ids)}")
    status_counts: Counter[str] = Counter()
    for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
        rows = rows_by_fp.get(embedded_fp, [])
        target_rows = [row for row in rows if str(row["product_code"] or "").upper() == TARGET_PRODUCT_CODE]
        statuses = Counter(str(row["status"] or "-") for row in target_rows)
        status_counts.update(statuses)
        selected = chosen.get(embedded_fp)
        print(f"credential fingerprint={short_fingerprint(embedded_fp)}")
        print(f"  matched_total_rows={len(rows)}")
        print(f"  matched_capcut_30d_rows={len(target_rows)}")
        print(f"  current_statuses={dict(sorted(statuses.items()))}")
        print(f"  selected_source_item_id={selected['inventory_item_id'] if selected else '-'}")
        print(f"  selected_source_status={selected['status'] if selected else '-'}")
        batch_matches = [
            row for row in target_rows
            if str(row["inventory_item_id"]) in existing_batch_ids
        ]
        print(f"  existing_new_batch_rows={len(batch_matches)}")
        print("  planned_action=create_new_available_inventory_item" if selected else "  planned_action=blocked")
    print(f"matched_capcut_30d_status_counts={dict(sorted(status_counts.items()))}")
    print(f"planned_new_items={len(chosen)}")
    print(f"stock_after_expected_available={stock_before.get('available', 0) + len(chosen)}")
    if errors:
        print("blocking_errors:")
        for error in errors:
            print(f"  {error}")
    print("changes_written=false")


def backup_database(database_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = database_path.with_name(f"{database_path.name}.bak_before_{BATCH_CODE}_{stamp}")
    shutil.copy2(database_path, backup_path)
    return backup_path


def apply_import(connection: sqlite3.Connection, chosen: dict[str, sqlite3.Row]) -> list[tuple[str, str]]:
    now = utc_now_iso()
    product_id = ensure_product(connection, now)
    created: list[tuple[str, str]] = []
    for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
        source = chosen[embedded_fp]
        item_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
            (item_id, product_id, str(source["secret_value"] or ""), now),
        )
        connection.execute(
            """
            INSERT INTO inventory_movements
                (id, inventory_item_id, action, source, created_at)
            VALUES (?, ?, 'import', ?, ?)
            """,
            (str(uuid.uuid4()), item_id, BATCH_CODE, now),
        )
        created.append((embedded_fp, item_id))
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Create CAPCUT_30D 20260810 NEW8 inventory as a new batch.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to store.db")
    parser.add_argument("--excel", type=Path, default=None, help="Optional local Excel path to verify embedded fingerprints")
    parser.add_argument("--yes", action="store_true", help="Apply the migration. Default is dry-run.")
    args = parser.parse_args()

    if len(EMBEDDED_CREDENTIAL_FINGERPRINTS) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprint count is wrong")
    if len(set(EMBEDDED_CREDENTIAL_FINGERPRINTS)) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprints contain duplicates")
    if args.excel:
        if not args.excel.is_file():
            raise SystemExit(f"Excel file not found: {args.excel}")
        verify_excel(args.excel)
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")

    uri = f"file:{args.database}?mode={'rwc' if args.yes else 'ro'}"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        rows_by_fp: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in select_inventory_rows(connection):
            row_fp = fingerprint(str(row["secret_value"] or ""))
            if row_fp in set(EMBEDDED_CREDENTIAL_FINGERPRINTS):
                rows_by_fp[row_fp].append(row)
        duplicate_allowed = secret_duplicate_allowed(connection)
        stock_before = stock_summary(connection)
        existing_batch_ids = batch_item_ids(connection)
        chosen, errors = choose_source_rows(rows_by_fp)
        if not duplicate_allowed:
            errors.append("inventory_items has a unique secret_value constraint; duplicate batch insert is not safe")
        if existing_batch_ids:
            errors.append(f"batch {BATCH_CODE} already exists with {len(existing_batch_ids)} item(s)")
        if len(chosen) != EXPECTED_CREDENTIAL_COUNT:
            errors.append(f"expected {EXPECTED_CREDENTIAL_COUNT} source credentials, selected {len(chosen)}")

        print_plan(connection, rows_by_fp, chosen, existing_batch_ids, stock_before, duplicate_allowed, errors)
        if not args.yes:
            return 0
        if errors:
            raise SystemExit("Refusing --yes because dry-run validation has blocking errors")

        backup_path = backup_database(args.database)
        print(f"backup_path={backup_path}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            created = apply_import(connection, chosen)
            stock_after = stock_summary(connection)
            if stock_after.get("available", 0) != stock_before.get("available", 0) + EXPECTED_CREDENTIAL_COUNT:
                raise RuntimeError("available stock did not increase by exactly 8")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    print("mode=applied")
    print("created_items:")
    for embedded_fp, item_id in created:
        print(f"  fingerprint={short_fingerprint(embedded_fp)} inventory_item_id={item_id}")
    print("changes_written=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
