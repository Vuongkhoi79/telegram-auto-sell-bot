"""Read-only diagnostic for the CAPCUT_30D 45k import duplicate report.

Production does not need the Excel file. The script embeds only SHA-256
fingerprints of the 8 verified credentials and compares those against hashes of
inventory_items.secret_value computed in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_inventory import credential_from_row, normalize_import_product_code, read_rows, text


DEFAULT_DATABASE = Path("/var/data/store.db")
TARGET_PRODUCT_CODE = "CAPCUT_30D"
EXPECTED_CREDENTIAL_COUNT = 8

# SHA-256 of the full credentials from:
# C:\Users\Admin\Downloads\8.10.26_import_CAPCUT_30D_45K_READY.xlsx
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
        fingerprints.append(fingerprint(credential_from_row(row)))
    return tuple(fingerprints)


def verify_excel_fingerprints(path: Path) -> None:
    excel_fingerprints = read_excel_fingerprints(path)
    embedded_set = set(EMBEDDED_CREDENTIAL_FINGERPRINTS)
    excel_set = set(excel_fingerprints)
    print(f"excel_verify_path={path}")
    print(f"excel_credentials={len(excel_fingerprints)}")
    print(f"excel_unique_credentials={len(excel_set)}")
    print(f"embedded_fingerprints_count={len(EMBEDDED_CREDENTIAL_FINGERPRINTS)}")
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


def select_inventory_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    inventory_cols = table_columns(connection, "inventory_items")
    product_cols = table_columns(connection, "products")
    if "secret_value" not in inventory_cols:
        raise SystemExit("Schema error: inventory_items.secret_value not found")
    if "product_id" not in inventory_cols:
        raise SystemExit("Schema error: inventory_items.product_id not found")
    if "id" not in product_cols or "code" not in product_cols:
        raise SystemExit("Schema error: products.id/code not found")

    def col(name: str, alias: str) -> str:
        return f"i.{name} AS {alias}" if name in inventory_cols else f"'' AS {alias}"

    order_by = "i.created_at, i.id" if "created_at" in inventory_cols else "i.id"
    query = f"""
        SELECT
            i.id AS inventory_item_id,
            i.secret_value AS secret_value,
            p.code AS product_code,
            {col("status", "status")},
            {col("reserved_order_id", "reserved_order_id_raw")},
            {col("delivered_order_id", "delivered_order_id_raw")},
            {col("created_at", "created_at")},
            {col("reserved_at", "reserved_at")},
            {col("delivered_at", "delivered_at")},
            {col("disabled_at", "disabled_at")}
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        ORDER BY p.code, {order_by}
    """
    return connection.execute(query).fetchall()


def order_links(connection: sqlite3.Connection, inventory_item_id: str) -> dict[str, Any]:
    if not (table_exists(connection, "order_inventory_items") and table_exists(connection, "orders")):
        return {"linked_order_count": 0, "reserved": "", "delivered": ""}
    rows = connection.execute(
        """
        SELECT oi.state, GROUP_CONCAT(o.order_id, ',') AS order_ids, COUNT(*) AS qty
        FROM order_inventory_items AS oi
        JOIN orders AS o ON o.id = oi.order_id
        WHERE oi.inventory_item_id = ?
        GROUP BY oi.state
        """,
        (inventory_item_id,),
    ).fetchall()
    result: dict[str, Any] = {"linked_order_count": 0, "reserved": "", "delivered": ""}
    for row in rows:
        state = str(row["state"] or "")
        result["linked_order_count"] += int(row["qty"] or 0)
        if state in {"reserved", "delivered"}:
            result[state] = str(row["order_ids"] or "")
    return result


def order_id_from_raw(connection: sqlite3.Connection, raw_value: str) -> str:
    raw_value = str(raw_value or "").strip()
    if not raw_value or not table_exists(connection, "orders"):
        return ""
    row = connection.execute(
        """
        SELECT order_id
        FROM orders
        WHERE order_id = ? OR id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (raw_value, raw_value),
    ).fetchone()
    return str(row["order_id"]) if row else raw_value


def stock_summary(connection: sqlite3.Connection, product_code: str) -> dict[str, int]:
    if not (table_exists(connection, "inventory_items") and table_exists(connection, "products")):
        return {}
    rows = connection.execute(
        """
        SELECT i.status, COUNT(*) AS qty
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE UPPER(p.code) = ?
        GROUP BY i.status
        """,
        (product_code.upper(),),
    ).fetchall()
    return {str(row["status"]): int(row["qty"] or 0) for row in rows}


def imported_at(connection: sqlite3.Connection, inventory_item_id: str, created_at: str) -> str:
    if not table_exists(connection, "inventory_movements"):
        return created_at
    movement_cols = table_columns(connection, "inventory_movements")
    if not {"inventory_item_id", "created_at", "action"}.issubset(movement_cols):
        return created_at
    row = connection.execute(
        """
        SELECT MIN(created_at) AS imported_at
        FROM inventory_movements
        WHERE inventory_item_id = ? AND action = 'import'
        """,
        (inventory_item_id,),
    ).fetchone()
    return str((row or {})["imported_at"] or created_at or "")


def movement_rows(connection: sqlite3.Connection, inventory_item_id: str) -> list[sqlite3.Row]:
    if not table_exists(connection, "inventory_movements"):
        return []
    movement_cols = table_columns(connection, "inventory_movements")
    required = {"inventory_item_id", "created_at", "action"}
    if not required.issubset(movement_cols):
        return []

    def col(name: str, alias: str) -> str:
        return f"{name} AS {alias}" if name in movement_cols else f"'' AS {alias}"

    query = f"""
        SELECT
            {col("action", "action")},
            {col("created_at", "created_at")},
            {col("source", "source")},
            {col("order_id", "order_id")},
            {col("admin_telegram_id", "admin_telegram_id")}
        FROM inventory_movements
        WHERE inventory_item_id = ?
        ORDER BY created_at, rowid
    """
    return connection.execute(query, (inventory_item_id,)).fetchall()


def first_movement_summary(connection: sqlite3.Connection, inventory_item_id: str, fallback_created_at: str) -> dict[str, str]:
    rows = movement_rows(connection, inventory_item_id)
    if not rows:
        return {
            "action": "",
            "created_at": fallback_created_at,
            "source": "",
            "order_id": "",
        }
    first = rows[0]
    return {
        "action": str(first["action"] or ""),
        "created_at": str(first["created_at"] or ""),
        "source": str(first["source"] or ""),
        "order_id": str(first["order_id"] or ""),
    }


def print_match(connection: sqlite3.Connection, embedded_fp: str, rows: list[sqlite3.Row], import_date: str) -> Counter[str]:
    print(f"credential fingerprint={short_fingerprint(embedded_fp)}")
    if not rows:
        print("  match=not_found")
        return Counter({"not_found": 1})

    counts: Counter[str] = Counter()
    print(f"  db_match_rows={len(rows)}")
    for index, row in enumerate(rows, start=1):
        status = str(row["status"] or "")
        counts[status] += 1
        links = order_links(connection, str(row["inventory_item_id"]))
        reserved_order_id = (
            links["reserved"]
            or order_id_from_raw(connection, str(row["reserved_order_id_raw"] or ""))
            or "-"
        )
        delivered_order_id = (
            links["delivered"]
            or order_id_from_raw(connection, str(row["delivered_order_id_raw"] or ""))
            or "-"
        )
        created_at = str(row["created_at"] or "")
        inventory_item_id = str(row["inventory_item_id"])
        first_movement = first_movement_summary(connection, inventory_item_id, created_at)
        movements = movement_rows(connection, inventory_item_id)
        import_events_on_date = [
            movement for movement in movements
            if str(movement["action"] or "") == "import" and str(movement["created_at"] or "").startswith(import_date)
        ]
        print(f"  match_{index}:")
        print(f"    inventory_item_id={inventory_item_id}")
        print(f"    product_code={row['product_code']}")
        print(f"    status={status or '-'}")
        print(f"    reserved_order_id={reserved_order_id}")
        print(f"    delivered_order_id={delivered_order_id}")
        print(f"    created_at={created_at or '-'}")
        print(f"    imported_at={imported_at(connection, str(row['inventory_item_id']), created_at) or '-'}")
        print(f"    first_action={first_movement['action'] or '-'}")
        print(f"    first_action_at={first_movement['created_at'] or '-'}")
        print(f"    first_action_source={first_movement['source'] or '-'}")
        print(f"    import_events_on_{import_date}={len(import_events_on_date)}")
        if movements:
            print("    movements:")
            for movement_index, movement in enumerate(movements, start=1):
                print(
                    f"      {movement_index}. action={movement['action'] or '-'} "
                    f"created_at={movement['created_at'] or '-'} "
                    f"source={movement['source'] or '-'} "
                    f"order_id={movement['order_id'] or '-'}"
                )
        else:
            print("    movements=none")
        print(f"    in_any_order={'yes' if int(links['linked_order_count'] or 0) else 'no'}")
    return counts


def available_target_rows(rows: list[sqlite3.Row]) -> list[tuple[str, sqlite3.Row]]:
    result: list[tuple[str, sqlite3.Row]] = []
    for row in rows:
        if str(row["product_code"] or "").upper() != TARGET_PRODUCT_CODE:
            continue
        if str(row["status"] or "").lower() != "available":
            continue
        result.append((fingerprint(str(row["secret_value"] or "")), row))
    return result


def print_available_stock_breakdown(
    connection: sqlite3.Connection,
    rows: list[tuple[str, sqlite3.Row]],
    embedded_set: set[str],
) -> dict[str, int]:
    print("available_stock_breakdown:")
    if not rows:
        print("  none")
        return {"total": 0, "new_lot": 0, "old_lot": 0}

    new_lot_count = 0
    old_lot_count = 0
    for index, (row_fp, row) in enumerate(rows, start=1):
        inventory_item_id = str(row["inventory_item_id"])
        created_at = str(row["created_at"] or "")
        first_movement = first_movement_summary(connection, inventory_item_id, created_at)
        is_new_lot = row_fp in embedded_set
        if is_new_lot:
            new_lot_count += 1
        else:
            old_lot_count += 1
        print(f"  available_{index}:")
        print(f"    fingerprint={short_fingerprint(row_fp)}")
        print(f"    inventory_item_id={inventory_item_id}")
        print(f"    belongs_to_embedded_8_new_lot={'yes' if is_new_lot else 'no'}")
        print(f"    product_code={row['product_code']}")
        print(f"    status={row['status'] or '-'}")
        print(f"    created_at={created_at or '-'}")
        print(f"    imported_at={imported_at(connection, inventory_item_id, created_at) or '-'}")
        print(f"    first_action={first_movement['action'] or '-'}")
        print(f"    first_action_at={first_movement['created_at'] or '-'}")
        print(f"    first_action_source={first_movement['source'] or '-'}")
    print("available_stock_summary:")
    print(f"  total_available={len(rows)}")
    print(f"  belongs_to_embedded_8_new_lot={new_lot_count}")
    print(f"  not_in_embedded_8_old_lot={old_lot_count}")
    return {"total": len(rows), "new_lot": new_lot_count, "old_lot": old_lot_count}


def is_before_cutoff(value: str, cutoff: str) -> bool:
    value = str(value or "").strip()
    return bool(value and value < cutoff)


def capcut_30d_rows_before_cutoff(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    cutoff: str,
) -> list[tuple[str, sqlite3.Row, str]]:
    result: list[tuple[str, sqlite3.Row, str]] = []
    for row in rows:
        if str(row["product_code"] or "").upper() != TARGET_PRODUCT_CODE:
            continue
        inventory_item_id = str(row["inventory_item_id"])
        created_at = str(row["created_at"] or "")
        item_imported_at = imported_at(connection, inventory_item_id, created_at)
        earliest_known_at = min([value for value in (created_at, item_imported_at) if value] or [""])
        if is_before_cutoff(earliest_known_at, cutoff):
            result.append((fingerprint(str(row["secret_value"] or "")), row, item_imported_at))
    return result


def was_available_at_cutoff(connection: sqlite3.Connection, row: sqlite3.Row, cutoff: str) -> bool:
    status = str(row["status"] or "")
    if status == "available":
        return True

    reserved_at = str(row["reserved_at"] or "")
    delivered_at = str(row["delivered_at"] or "")
    disabled_at = str(row["disabled_at"] or "")

    if status == "reserved":
        return is_before_cutoff(str(row["created_at"] or ""), cutoff) and not is_before_cutoff(reserved_at, cutoff)
    if status == "delivered":
        if is_before_cutoff(delivered_at, cutoff):
            return False
        return is_before_cutoff(str(row["created_at"] or ""), cutoff)
    if status == "disabled":
        if is_before_cutoff(disabled_at, cutoff):
            return False
        return is_before_cutoff(str(row["created_at"] or ""), cutoff)

    movements = movement_rows(connection, str(row["inventory_item_id"]))
    for movement in movements:
        action = str(movement["action"] or "")
        movement_at = str(movement["created_at"] or "")
        if action in {"reserve", "deliver", "disable"} and is_before_cutoff(movement_at, cutoff):
            return False
    return is_before_cutoff(str(row["created_at"] or ""), cutoff)


def print_old_inventory_timeline(
    connection: sqlite3.Connection,
    old_rows: list[tuple[str, sqlite3.Row, str]],
    embedded_set: set[str],
    cutoff: str,
) -> dict[str, int]:
    print("old_inventory_before_cutoff:")
    print(f"  cutoff={cutoff}")
    status_counts: Counter[str] = Counter()
    old_lot_remaining_available = 0
    old_lot_sold = 0
    old_lot_disabled = 0
    old_lot_reserved = 0
    available_at_cutoff = 0

    if not old_rows:
        print("  none")
    for index, (row_fp, row, item_imported_at) in enumerate(old_rows, start=1):
        inventory_item_id = str(row["inventory_item_id"])
        status = str(row["status"] or "")
        status_counts[status] += 1
        first_movement = first_movement_summary(connection, inventory_item_id, str(row["created_at"] or ""))
        belongs_to_embedded = row_fp in embedded_set
        item_was_available_at_cutoff = was_available_at_cutoff(connection, row, cutoff)
        if item_was_available_at_cutoff:
            available_at_cutoff += 1
        if not belongs_to_embedded:
            if status == "available":
                old_lot_remaining_available += 1
            elif status == "delivered":
                old_lot_sold += 1
            elif status == "disabled":
                old_lot_disabled += 1
            elif status == "reserved":
                old_lot_reserved += 1
        delivered_order_id = (
            order_links(connection, inventory_item_id)["delivered"]
            or order_id_from_raw(connection, str(row["delivered_order_id_raw"] or ""))
            or "-"
        )
        print(f"  old_item_{index}:")
        print(f"    fingerprint={short_fingerprint(row_fp)}")
        print(f"    inventory_item_id={inventory_item_id}")
        print(f"    belongs_to_embedded_8_new_lot={'yes' if belongs_to_embedded else 'no'}")
        print(f"    created_at={row['created_at'] or '-'}")
        print(f"    imported_at={item_imported_at or '-'}")
        print(f"    first_action={first_movement['action'] or '-'}")
        print(f"    first_action_at={first_movement['created_at'] or '-'}")
        print(f"    first_action_source={first_movement['source'] or '-'}")
        print(f"    status={status or '-'}")
        print(f"    available_at_cutoff={'yes' if item_was_available_at_cutoff else 'no'}")
        print(f"    delivered_order_id={delivered_order_id}")
        print(f"    delivered_at={row['delivered_at'] or '-'}")
        print(f"    disabled_at={row['disabled_at'] or '-'}")

    print("old_inventory_status_summary:")
    print(f"  available={status_counts.get('available', 0)}")
    print(f"  reserved={status_counts.get('reserved', 0)}")
    print(f"  delivered={status_counts.get('delivered', 0)}")
    print(f"  disabled={status_counts.get('disabled', 0)}")
    print(f"  available_at_cutoff={available_at_cutoff}")
    print("old_lot_not_in_embedded_8_summary:")
    print(f"  currently_available={old_lot_remaining_available}")
    print(f"  delivered={old_lot_sold}")
    print(f"  reserved={old_lot_reserved}")
    print(f"  disabled={old_lot_disabled}")
    return {
        "total": len(old_rows),
        "available": status_counts.get("available", 0),
        "reserved": status_counts.get("reserved", 0),
        "delivered": status_counts.get("delivered", 0),
        "disabled": status_counts.get("disabled", 0),
        "available_at_cutoff": available_at_cutoff,
        "old_lot_currently_available": old_lot_remaining_available,
        "old_lot_delivered": old_lot_sold,
        "old_lot_reserved": old_lot_reserved,
        "old_lot_disabled": old_lot_disabled,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only CAPCUT_30D duplicate diagnostic.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to store.db")
    parser.add_argument("--excel", type=Path, default=None, help="Optional local Excel path to verify embedded fingerprints")
    parser.add_argument("--import-date", default=str(date.today()), help="Date prefix used to count import events, e.g. 2026-08-10")
    parser.add_argument("--cutoff", default="2026-08-05T10:43:30+00:00", help="Cutoff for old CAPCUT_30D inventory timeline")
    args = parser.parse_args()

    if len(EMBEDDED_CREDENTIAL_FINGERPRINTS) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprint count is wrong")
    if len(set(EMBEDDED_CREDENTIAL_FINGERPRINTS)) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprints contain duplicates")
    if args.excel:
        if not args.excel.is_file():
            raise SystemExit(f"Excel file not found: {args.excel}")
        verify_excel_fingerprints(args.excel)

    database_path = args.database
    if not database_path.is_file():
        raise SystemExit(f"Database not found: {database_path}")

    embedded_set = set(EMBEDDED_CREDENTIAL_FINGERPRINTS)
    matches_by_fp: dict[str, list[sqlite3.Row]] = defaultdict(list)
    with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        stock = stock_summary(connection, TARGET_PRODUCT_CODE)
        inventory_rows = select_inventory_rows(connection)
        for row in inventory_rows:
            secret_value = str(row["secret_value"] or "")
            row_fp = fingerprint(secret_value)
            if row_fp in embedded_set:
                matches_by_fp[row_fp].append(row)
        available_rows = available_target_rows(inventory_rows)
        old_rows = capcut_30d_rows_before_cutoff(connection, inventory_rows, args.cutoff)

        print("mode=read_only")
        print(f"database={database_path}")
        print(f"target_product_code={TARGET_PRODUCT_CODE}")
        print(f"embedded_fingerprints_count={len(EMBEDDED_CREDENTIAL_FINGERPRINTS)}")
        print(
            "target_stock_summary="
            f"available={stock.get('available', 0)} "
            f"reserved={stock.get('reserved', 0)} "
            f"delivered={stock.get('delivered', 0)} "
            f"disabled={stock.get('disabled', 0)}"
        )

        total_counts: Counter[str] = Counter()
        product_mismatch: Counter[str] = Counter()
        matched_item_ids: list[str] = []
        matched_created_today = 0
        matched_import_events_today = 0
        for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
            rows = matches_by_fp.get(embedded_fp, [])
            total_counts.update(print_match(connection, embedded_fp, rows, args.import_date))
            for row in rows:
                matched_item_ids.append(str(row["inventory_item_id"]))
                if str(row["created_at"] or "").startswith(args.import_date):
                    matched_created_today += 1
                matched_import_events_today += sum(
                    1 for movement in movement_rows(connection, str(row["inventory_item_id"]))
                    if str(movement["action"] or "") == "import"
                    and str(movement["created_at"] or "").startswith(args.import_date)
                )
                product_code = str(row["product_code"] or "")
                if product_code.upper() != TARGET_PRODUCT_CODE:
                    product_mismatch[product_code] += 1
        available_breakdown = print_available_stock_breakdown(connection, available_rows, embedded_set)
        old_timeline = print_old_inventory_timeline(connection, old_rows, embedded_set, args.cutoff)

    matched = sum(total_counts.get(status, 0) for status in ("available", "reserved", "delivered", "disabled"))
    print("summary:")
    print(f"  matched={matched}")
    print(f"  available={total_counts.get('available', 0)}")
    print(f"  reserved={total_counts.get('reserved', 0)}")
    print(f"  delivered={total_counts.get('delivered', 0)}")
    print(f"  disabled={total_counts.get('disabled', 0)}")
    print(f"  not_found={total_counts.get('not_found', 0)}")
    print(f"  matched_inventory_items={len(matched_item_ids)}")
    print(f"  matched_created_on_{args.import_date}={matched_created_today}")
    print(f"  matched_import_events_on_{args.import_date}={matched_import_events_today}")
    print(f"  current_available_total={available_breakdown['total']}")
    print(f"  current_available_from_embedded_8_new_lot={available_breakdown['new_lot']}")
    print(f"  current_available_not_in_embedded_8_old_lot={available_breakdown['old_lot']}")
    print(f"  old_inventory_before_cutoff_total={old_timeline['total']}")
    print(f"  old_inventory_available_at_cutoff={old_timeline['available_at_cutoff']}")
    print(f"  old_lot_not_in_embedded_8_currently_available={old_timeline['old_lot_currently_available']}")
    print(f"  old_lot_not_in_embedded_8_delivered={old_timeline['old_lot_delivered']}")
    print(f"  old_lot_not_in_embedded_8_reserved={old_timeline['old_lot_reserved']}")
    print(f"  old_lot_not_in_embedded_8_disabled={old_timeline['old_lot_disabled']}")
    if product_mismatch:
        print("  product_mismatch=yes")
        for product_code, count in sorted(product_mismatch.items()):
            print(f"    {product_code}: {count}")
    else:
        print("  product_mismatch=no")
    if matched == EXPECTED_CREDENTIAL_COUNT and total_counts.get("available", 0) == 0:
        print(
            "explanation=Importer can report duplicates because these exact credential fingerprints already exist in inventory; "
            "duplicate detection does not return delivered/reserved/disabled rows to available stock."
        )
    print("changes_written=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
