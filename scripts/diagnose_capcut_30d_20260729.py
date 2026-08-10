"""Read-only diagnostic for the 2026-07-29 CAPCUT_30D import lot.

The production command does not require the Excel file. This script embeds only
SHA-256 fingerprints of the 10 verified credentials and compares them against
hashes of inventory_items.secret_value computed in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_inventory import credential_from_row, normalize_import_product_code, read_rows, text


DEFAULT_DATABASE = Path("/var/data/store.db")
TARGET_PRODUCT_CODE = "CAPCUT_30D"
EXPECTED_CREDENTIAL_COUNT = 10

# SHA-256 of the full credentials from:
# C:\Users\Admin\Desktop\Hàng nhập Bot\7.29.26_import_inventory_CAPCUT_PER_PRODUCT_FIXED.xlsx
# Do not add email, password, credential_text, or secret_value here.
EMBEDDED_CREDENTIAL_FINGERPRINTS = (
    "321206535feced5e105b3cb1bd59cc4538dff79987fdf0edc9f63f1cd335ce7b",
    "5b047d0ca5664f5323139d4478991d996bc921867af62398eccac2d8d9b5d8c7",
    "78e5a6866cab044ac36a0cc752dd111b9609071e713908d293a3d86c8b105959",
    "8c8b00f6acffa56f7c89713728115b7c66310dafa75d04531f935974b60eb9fd",
    "039e658b4a8b153cf294d9d6d3cfba27b8de1ec400c3d6b49731d1edb3a409f5",
    "c321c4e1aabb566595d923aa77b76adab4e90d2449b6f10922756b98727ee9c1",
    "18e0ce3b2c3f4fdcf243f91fe14e2307636b235865f8293d867f0267a0a66184",
    "8b3bdc44acf6f2fa7ebe5517af4195fabdc1e25ae0107d80f4619bbfe58388a6",
    "2471dd635aa6fd23dd1a79e5e8b0acf76543acc9b30bea75a17715f2de8becf0",
    "0d08d35c9bc124b0463e7340d8fc9facb4c60aae886be02fae4742717897db8e",
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
    return connection.execute(
        f"""
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
    ).fetchall()


def movement_rows(connection: sqlite3.Connection, inventory_item_id: str) -> list[sqlite3.Row]:
    if not table_exists(connection, "inventory_movements"):
        return []
    movement_cols = table_columns(connection, "inventory_movements")
    if not {"inventory_item_id", "created_at", "action"}.issubset(movement_cols):
        return []

    def col(name: str, alias: str) -> str:
        return f"{name} AS {alias}" if name in movement_cols else f"'' AS {alias}"

    return connection.execute(
        f"""
        SELECT
            {col("action", "action")},
            {col("created_at", "created_at")},
            {col("source", "source")},
            {col("order_id", "order_id")},
            {col("admin_telegram_id", "admin_telegram_id")}
        FROM inventory_movements
        WHERE inventory_item_id = ?
        ORDER BY created_at, rowid
        """,
        (inventory_item_id,),
    ).fetchall()


def first_movement(connection: sqlite3.Connection, inventory_item_id: str, fallback_created_at: str) -> dict[str, str]:
    rows = movement_rows(connection, inventory_item_id)
    if not rows:
        return {"action": "", "created_at": fallback_created_at, "source": "", "order_id": ""}
    first = rows[0]
    return {
        "action": str(first["action"] or ""),
        "created_at": str(first["created_at"] or ""),
        "source": str(first["source"] or ""),
        "order_id": str(first["order_id"] or ""),
    }


def imported_at(connection: sqlite3.Connection, inventory_item_id: str, created_at: str) -> str:
    imports = [
        str(row["created_at"] or "")
        for row in movement_rows(connection, inventory_item_id)
        if str(row["action"] or "") == "import" and str(row["created_at"] or "")
    ]
    return min(imports) if imports else created_at


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


def linked_order_rows(connection: sqlite3.Connection, inventory_item_id: str) -> list[sqlite3.Row]:
    if not (table_exists(connection, "order_inventory_items") and table_exists(connection, "orders")):
        return []
    oi_cols = table_columns(connection, "order_inventory_items")
    order_cols = table_columns(connection, "orders")
    if not {"order_id", "inventory_item_id", "state"}.issubset(oi_cols):
        return []

    def col(name: str, alias: str) -> str:
        return f"o.{name} AS {alias}" if name in order_cols else f"'' AS {alias}"

    return connection.execute(
        f"""
        SELECT
            oi.state AS link_state,
            {col("id", "order_row_id")},
            {col("order_id", "order_id")},
            {col("product_code", "order_product_code")},
            {col("package_name", "package_name")},
            {col("quantity", "quantity")},
            {col("unit_price_vnd", "unit_price_vnd")},
            {col("total_vnd", "total_vnd")},
            {col("payment_status", "payment_status")},
            {col("order_status", "order_status")},
            {col("transaction_id", "transaction_id")},
            {col("created_at", "order_created_at")},
            {col("paid_at", "paid_at")},
            {col("delivered_at", "order_delivered_at")}
        FROM order_inventory_items AS oi
        JOIN orders AS o ON o.id = oi.order_id
        WHERE oi.inventory_item_id = ?
        ORDER BY o.created_at, o.order_id
        """,
        (inventory_item_id,),
    ).fetchall()


def payment_summary(connection: sqlite3.Connection, order_row_id: str) -> str:
    if not order_row_id or not table_exists(connection, "payment_transactions"):
        return "payment_transactions=unavailable"
    payment_cols = table_columns(connection, "payment_transactions")
    if "order_id" not in payment_cols:
        return "payment_order_id_column=missing"
    status_expr = "GROUP_CONCAT(status, ',')" if "status" in payment_cols else "''"
    amount_expr = "SUM(amount_vnd)" if "amount_vnd" in payment_cols else "0"
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS qty, {status_expr} AS statuses, {amount_expr} AS amount_vnd
        FROM payment_transactions
        WHERE order_id = ?
        """,
        (order_row_id,),
    ).fetchone()
    if not row or int(row["qty"] or 0) == 0:
        return "payment_transactions=0"
    return (
        f"payment_transactions={int(row['qty'] or 0)} "
        f"statuses={row['statuses'] or '-'} amount_vnd={int(row['amount_vnd'] or 0)}"
    )


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


def stock_summary(connection: sqlite3.Connection) -> dict[str, int]:
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
        (TARGET_PRODUCT_CODE,),
    ).fetchall()
    return {str(row["status"]): int(row["qty"] or 0) for row in rows}


def stock_summary_by_product(connection: sqlite3.Connection, product_code: str) -> dict[str, int]:
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


def public_order_id(connection: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str:
    links = order_links(connection, str(row["inventory_item_id"]))
    if kind == "reserved":
        return str(
            links["reserved"]
            or order_id_from_raw(connection, str(row["reserved_order_id_raw"] or ""))
            or ""
        )
    return str(
        links["delivered"]
        or order_id_from_raw(connection, str(row["delivered_order_id_raw"] or ""))
        or ""
    )


def print_account_match(connection: sqlite3.Connection, embedded_fp: str, rows: list[sqlite3.Row]) -> Counter[str]:
    print(f"account fingerprint={short_fingerprint(embedded_fp)}")
    if not rows:
        print("  match=not_found")
        return Counter({"not_found": 1})

    counts: Counter[str] = Counter()
    print(f"  db_match_rows={len(rows)}")
    for index, row in enumerate(rows, start=1):
        inventory_item_id = str(row["inventory_item_id"])
        status = str(row["status"] or "")
        created_at = str(row["created_at"] or "")
        first = first_movement(connection, inventory_item_id, created_at)
        links = order_links(connection, inventory_item_id)
        counts[status] += 1
        print(f"  match_{index}:")
        print(f"    inventory_item_id={inventory_item_id}")
        print(f"    product_code={row['product_code']}")
        print(f"    status={status or '-'}")
        print(f"    created_at={created_at or '-'}")
        print(f"    imported_at={imported_at(connection, inventory_item_id, created_at) or '-'}")
        print(f"    reserved_order_id={public_order_id(connection, row, 'reserved') or '-'}")
        print(f"    delivered_order_id={public_order_id(connection, row, 'delivered') or '-'}")
        print(f"    delivered_at={row['delivered_at'] or '-'}")
        print(f"    first_action={first['action'] or '-'}")
        print(f"    first_action_at={first['created_at'] or '-'}")
        print(f"    first_action_source={first['source'] or '-'}")
        print(f"    in_any_order={'yes' if int(links['linked_order_count'] or 0) else 'no'}")
        linked_orders = linked_order_rows(connection, inventory_item_id)
        if linked_orders:
            print("    linked_orders:")
            for linked in linked_orders:
                order_row_id = str(linked["order_row_id"] or "")
                print(
                    f"      order_id={linked['order_id'] or '-'} "
                    f"link_state={linked['link_state'] or '-'} "
                    f"order_product_code={linked['order_product_code'] or '-'} "
                    f"package_name={linked['package_name'] or '-'} "
                    f"quantity={linked['quantity'] or '-'} "
                    f"unit_price_vnd={linked['unit_price_vnd'] or '-'} "
                    f"total_vnd={linked['total_vnd'] or '-'} "
                    f"payment_status={linked['payment_status'] or '-'} "
                    f"order_status={linked['order_status'] or '-'} "
                    f"paid_at={linked['paid_at'] or '-'} "
                    f"order_delivered_at={linked['order_delivered_at'] or '-'} "
                    f"{payment_summary(connection, order_row_id)}"
                )
        movements = movement_rows(connection, inventory_item_id)
        if movements:
            print("    movements:")
            for movement_index, movement in enumerate(movements, start=1):
                print(
                    f"      {movement_index}. action={movement['action'] or '-'} "
                    f"created_at={movement['created_at'] or '-'} "
                    f"source={movement['source'] or '-'} "
                    f"order_id={movement['order_id'] or '-'}"
                )
    return counts


def print_cross_product_matches(cross_matches_by_fp: dict[str, list[sqlite3.Row]]) -> Counter[str]:
    product_counts: Counter[str] = Counter()
    print("cross_product_matches_not_counted_in_capcut:")
    any_cross = False
    for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
        rows = cross_matches_by_fp.get(embedded_fp, [])
        if not rows:
            continue
        any_cross = True
        print(f"  fingerprint={short_fingerprint(embedded_fp)}")
        for row in rows:
            product_code = str(row["product_code"] or "")
            product_counts[product_code] += 1
            print(
                f"    inventory_item_id={row['inventory_item_id']} "
                f"product_code={product_code} "
                f"status={row['status'] or '-'} "
                f"created_at={row['created_at'] or '-'} "
                f"delivered_order_id={row['delivered_order_id_raw'] or '-'} "
                f"delivered_at={row['delivered_at'] or '-'}"
            )
    if not any_cross:
        print("  none")
    if product_counts:
        print("cross_product_summary:")
        for product_code, count in sorted(product_counts.items()):
            print(f"  {product_code}={count}")
    return product_counts


def available_membership(rows: list[sqlite3.Row], embedded_set: set[str]) -> tuple[list[str], list[tuple[str, str]]]:
    matches: list[str] = []
    available_rows: list[tuple[str, str]] = []
    for row in rows:
        if str(row["product_code"] or "").upper() != TARGET_PRODUCT_CODE:
            continue
        if str(row["status"] or "").lower() != "available":
            continue
        row_fp = fingerprint(str(row["secret_value"] or ""))
        available_rows.append((row_fp, str(row["inventory_item_id"])))
        if row_fp in embedded_set:
            matches.append(row_fp)
    return matches, available_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only CAPCUT_30D 2026-07-29 lot diagnostic.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to store.db")
    parser.add_argument("--excel", type=Path, default=None, help="Optional local Excel path to verify embedded fingerprints")
    args = parser.parse_args()

    if len(EMBEDDED_CREDENTIAL_FINGERPRINTS) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprint count is wrong")
    if len(set(EMBEDDED_CREDENTIAL_FINGERPRINTS)) != EXPECTED_CREDENTIAL_COUNT:
        raise SystemExit("Embedded fingerprints contain duplicates")
    if args.excel:
        if not args.excel.is_file():
            raise SystemExit(f"Excel file not found: {args.excel}")
        verify_excel_fingerprints(args.excel)
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")

    embedded_set = set(EMBEDDED_CREDENTIAL_FINGERPRINTS)
    capcut_matches_by_fp: dict[str, list[sqlite3.Row]] = defaultdict(list)
    cross_matches_by_fp: dict[str, list[sqlite3.Row]] = defaultdict(list)
    with closing(sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        inventory_rows = select_inventory_rows(connection)
        for row in inventory_rows:
            row_fp = fingerprint(str(row["secret_value"] or ""))
            if row_fp in embedded_set:
                if str(row["product_code"] or "").upper() == TARGET_PRODUCT_CODE:
                    capcut_matches_by_fp[row_fp].append(row)
                else:
                    cross_matches_by_fp[row_fp].append(row)

        stock = stock_summary(connection)
        grok_stock = stock_summary_by_product(connection, "GROK_75K")
        available_lot_matches, current_available_rows = available_membership(inventory_rows, embedded_set)
        print("mode=read_only")
        print(f"database={args.database}")
        print(f"target_product_code={TARGET_PRODUCT_CODE}")
        print("source_lot=2026-07-29 CAPCUT_30D")
        print(f"embedded_fingerprints_count={len(EMBEDDED_CREDENTIAL_FINGERPRINTS)}")
        print(
            "target_stock_summary="
            f"available={stock.get('available', 0)} "
            f"reserved={stock.get('reserved', 0)} "
            f"delivered={stock.get('delivered', 0)} "
            f"disabled={stock.get('disabled', 0)}"
        )
        print(
            "grok_75k_stock_summary_for_cross_check="
            f"available={grok_stock.get('available', 0)} "
            f"reserved={grok_stock.get('reserved', 0)} "
            f"delivered={grok_stock.get('delivered', 0)} "
            f"disabled={grok_stock.get('disabled', 0)}"
        )
        print(f"current_capcut_30d_available_rows={len(current_available_rows)}")

        total_counts: Counter[str] = Counter()
        delivered_orders: list[tuple[str, str, str]] = []
        unique_delivered_order_ids: set[str] = set()
        for embedded_fp in EMBEDDED_CREDENTIAL_FINGERPRINTS:
            rows = capcut_matches_by_fp.get(embedded_fp, [])
            total_counts.update(print_account_match(connection, embedded_fp, rows))
            for row in rows:
                if str(row["status"] or "") == "delivered":
                    delivered_order_id = public_order_id(connection, row, "delivered") or "-"
                    if delivered_order_id != "-":
                        unique_delivered_order_ids.add(delivered_order_id)
                    delivered_orders.append(
                        (
                            embedded_fp,
                            delivered_order_id,
                            str(row["delivered_at"] or "-"),
                        )
                    )
        cross_counts = print_cross_product_matches(cross_matches_by_fp)

    matched = sum(total_counts.get(status, 0) for status in ("available", "reserved", "delivered", "disabled"))
    print("available_membership:")
    if available_lot_matches:
        print(f"  file_20260729_accounts_in_current_available={len(available_lot_matches)}")
        for item in available_lot_matches:
            print(f"  fingerprint={short_fingerprint(item)}")
    else:
        print("  file_20260729_accounts_in_current_available=0")
        print("  ZERO MATCH")

    print("delivered_accounts_from_file_20260729:")
    if delivered_orders:
        for embedded_fp, order_id, delivered_at_value in delivered_orders:
            print(f"  fingerprint={short_fingerprint(embedded_fp)} order_id={order_id} delivered_at={delivered_at_value}")
    else:
        print("  none")
    print("unique_capcut_30d_delivered_order_ids:")
    if unique_delivered_order_ids:
        for order_id in sorted(unique_delivered_order_ids):
            print(f"  {order_id}")
    else:
        print("  none")

    print("summary:")
    print(f"  matched={matched}")
    print(f"  available={total_counts.get('available', 0)}")
    print(f"  delivered={total_counts.get('delivered', 0)}")
    print(f"  reserved={total_counts.get('reserved', 0)}")
    print(f"  disabled={total_counts.get('disabled', 0)}")
    print(f"  not_found={total_counts.get('not_found', 0)}")
    print(f"  current_capcut_30d_available={len(current_available_rows)}")
    print(f"  current_available_from_file_20260729={len(available_lot_matches)}")
    print(f"  cross_product_matches_not_counted={sum(cross_counts.values())}")
    print("changes_written=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
