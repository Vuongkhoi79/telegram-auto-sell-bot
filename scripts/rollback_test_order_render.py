"""Safely rollback one known test order on Render production SQLite.

Dry-run first:
    python scripts/rollback_test_order_render.py --database /var/data/store.db --order-id ORD-... 

Commit:
    python scripts/rollback_test_order_render.py --database /var/data/store.db --order-id ORD-... --yes

This script:
- inspects the target order, linked inventory item(s), and payment transaction(s)
- only supports the known bad test order ORD-20260721093007-CHATGPT-246F7BE7
- verifies the delivered item is the expected CHATGPT private inventory row
- backs up the database before any write
- restores the delivered inventory item(s) to available
- invalidates any order_inventory_items link by marking it released
- keeps order and payment history intact
- never performs a bank refund
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DATABASE = Path("/var/data/store.db")
TARGET_ORDER_ID = "ORD-20260721093007-CHATGPT-246F7BE7"
TARGET_ORDER_PRODUCT_CODE = "CHATGPT"
TARGET_INVENTORY_ITEM_ID = "565a3e52-4ce2-4062-ab50-4ca49350658d"
TARGET_PAYMENT_AMOUNT_VND = 160000
TARGET_PAYMENT_STATUS = "processed"
AUDIT_SOURCE = "rollback_test"
TARGET_COUNTS_PRODUCT_CODES = ("CHATGPT", "CHATGPT_SHARED")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def masked_secret(secret_value: str) -> str:
    value = str(secret_value or "")
    if "|" in value:
        email, rest = value.split("|", 1)
        email_mask = email[:3] + "***" + email[-4:] if "@" in email else email[:3] + "***"
        return f"{email_mask}|***"
    return value[:3] + "***" + value[-4:] if len(value) > 8 else "***"


def resolve_database_path(raw: str | None) -> Path:
    if not raw:
        return DEFAULT_DATABASE
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def backup_database(database_path: Path) -> Path:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"store_before_test_order_rollback_{timestamp}.db"
    shutil.copy2(database_path, backup_path)
    return backup_path


def schema_order_status_choices(connection: sqlite3.Connection) -> tuple[str, str]:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
    ).fetchone()
    sql = str(row["sql"] if row else "")
    if "cancelled_test" in sql or "refunded_test" in sql:
        payment_status = "refunded_test" if "refunded_test" in sql else "refunded"
        order_status = "cancelled_test" if "cancelled_test" in sql else "cancelled"
        return payment_status, order_status
    return "refunded", "cancelled"


def fetch_order(connection: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, order_id, telegram_user_id, username, product_id, product_code,
               product_name, package_name, quantity, unit_price_vnd, total_vnd,
               delivery_type, machine_id, plan, payment_method, payment_status,
               order_status, transaction_id, created_at, expire_at, paid_at,
               delivered_at, delivery_ref, inventory_source
        FROM orders
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()


def fetch_linked_items(connection: sqlite3.Connection, order_row_id: str, order_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            oi.order_id AS order_row_id,
            oi.inventory_item_id,
            oi.state,
            oi.created_at AS link_created_at,
            oi.delivered_at AS link_delivered_at,
            oi.released_at AS link_released_at,
            i.status,
            i.secret_value,
            i.reserved_order_id,
            i.delivered_order_id,
            i.reserved_at,
            i.delivered_at AS item_delivered_at,
            p.code AS product_code,
            p.name AS product_name
        FROM order_inventory_items AS oi
        JOIN inventory_items AS i ON i.id = oi.inventory_item_id
        JOIN products AS p ON p.id = i.product_id
        WHERE oi.order_id = ?
        ORDER BY oi.created_at, oi.inventory_item_id
        """,
        (order_row_id,),
    ).fetchall()
    if rows:
        return rows
    rows = connection.execute(
        """
        SELECT
            ? AS order_row_id,
            i.id AS inventory_item_id,
            CASE
                WHEN i.status = 'delivered' THEN 'delivered'
                WHEN i.status = 'reserved' THEN 'reserved'
                ELSE i.status
            END AS state,
            i.created_at AS link_created_at,
            i.delivered_at AS link_delivered_at,
            NULL AS link_released_at,
            i.status,
            i.secret_value,
            i.reserved_order_id,
            i.delivered_order_id,
            i.reserved_at,
            i.delivered_at AS item_delivered_at,
            p.code AS product_code,
            p.name AS product_name
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE i.delivered_order_id = ? OR i.reserved_order_id = ?
        ORDER BY i.created_at, i.id
        """,
        (order_row_id, order_id, order_id),
    ).fetchall()
    return rows


def fetch_payment_transactions(connection: sqlite3.Connection, order_row_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, provider, provider_transaction_id, amount_vnd, description,
               status, received_at, processed_at
        FROM payment_transactions
        WHERE order_id = ?
        ORDER BY received_at, id
        """,
        (order_row_id,),
    ).fetchall()


def fetch_order_item_link_count(connection: sqlite3.Connection, inventory_item_id: str, order_row_id: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM order_inventory_items
        WHERE inventory_item_id = ? AND order_id = ?
        """,
        (inventory_item_id, order_row_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def fetch_other_order_item_link_count(connection: sqlite3.Connection, inventory_item_id: str, order_row_id: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM order_inventory_items
        WHERE inventory_item_id = ? AND order_id <> ?
        """,
        (inventory_item_id, order_row_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def product_counts(connection: sqlite3.Connection, product_code: str) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT i.status, COUNT(*) AS count
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE UPPER(p.code) = ?
        GROUP BY i.status
        """,
        (product_code.upper(),),
    ).fetchall()
    return {str(row["status"]): int(row["count"] or 0) for row in rows}


def multi_product_counts(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for code in TARGET_COUNTS_PRODUCT_CODES:
        result[code] = product_counts(connection, code)
    return result


def all_counts(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = connection.execute(
        """
        SELECT UPPER(p.code) AS code, i.status, COUNT(*) AS count
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        GROUP BY UPPER(p.code), i.status
        ORDER BY code, i.status
        """
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        code = str(row["code"])
        counts.setdefault(code, {})[str(row["status"])] = int(row["count"] or 0)
    return counts


def print_order_summary(order: sqlite3.Row | None, items: list[sqlite3.Row], txs: list[sqlite3.Row]) -> None:
    print(f"ORDER_ID: {TARGET_ORDER_ID}")
    if not order:
        print("ORDER: NOT FOUND")
        return
    print(
        "ORDER: "
        f"product_code={order['product_code']} "
        f"product_name={order['product_name']} "
        f"payment_status={order['payment_status']} "
        f"order_status={order['order_status']} "
        f"delivery_type={order['delivery_type']} "
        f"delivery_ref={order['delivery_ref']}"
    )
    print(f"LINKED_ITEMS: {len(items)}")
    for item in items:
        print(
            "  ITEM: "
            f"inventory_item_id={item['inventory_item_id']} "
            f"product_code={item['product_code']} "
            f"status={item['status']} "
            f"reserved_order_id={item['reserved_order_id']} "
            f"delivered_order_id={item['delivered_order_id']} "
            f"secret_hint={masked_secret(str(item['secret_value']))}"
        )
    print(f"PAYMENT_TXS: {len(txs)}")
    for tx in txs:
        provider_tx = str(tx["provider_transaction_id"] or "")
        print(
            "  TX: "
            f"provider={tx['provider']} "
            f"provider_tx={provider_tx[:4]}***{provider_tx[-4:] if len(provider_tx) >= 4 else ''} "
            f"amount_vnd={tx['amount_vnd']} "
            f"status={tx['status']}"
        )


def validate_targets(
    connection: sqlite3.Connection,
    order: sqlite3.Row | None,
    items: list[sqlite3.Row],
    txs: list[sqlite3.Row],
) -> None:
    if not order:
        raise RuntimeError(f"Order not found: {TARGET_ORDER_ID}")
    if str(order["order_id"]) != TARGET_ORDER_ID:
        raise RuntimeError(f"Unexpected order_id: {order['order_id']}")
    if str(order["product_code"]).upper() != TARGET_ORDER_PRODUCT_CODE:
        raise RuntimeError(f"Unexpected order product_code: {order['product_code']}")
    if int(order["total_vnd"] or 0) != TARGET_PAYMENT_AMOUNT_VND:
        raise RuntimeError(f"Unexpected order total_vnd: {order['total_vnd']}")
    if not items:
        raise RuntimeError("No linked inventory items found for this order")
    if len(items) != 1:
        raise RuntimeError(f"Expected exactly 1 linked item, found {len(items)}")
    item = items[0]
    if str(item["inventory_item_id"]) != TARGET_INVENTORY_ITEM_ID:
        raise RuntimeError(f"Unexpected inventory_item_id: {item['inventory_item_id']}")
    if str(item["product_code"]).upper() != TARGET_ORDER_PRODUCT_CODE:
        raise RuntimeError(f"Unexpected linked item product_code: {item['product_code']}")
    if str(item["status"]).lower() != "delivered":
        raise RuntimeError(f"Linked item is not delivered: {item['status']}")
    if str(item["delivered_order_id"] or "") != str(order["id"]):
        raise RuntimeError("Linked item was not delivered by this order")
    if fetch_order_item_link_count(connection, TARGET_INVENTORY_ITEM_ID, str(order["id"])) != 1:
        raise RuntimeError("Expected exactly one order_inventory_items link for this order")
    if fetch_other_order_item_link_count(connection, TARGET_INVENTORY_ITEM_ID, str(order["id"])) != 0:
        raise RuntimeError("Inventory item is linked to another order")
    if not txs:
        raise RuntimeError("No payment transaction linked to this order")
    matching_txs = [tx for tx in txs if int(tx["amount_vnd"] or 0) == TARGET_PAYMENT_AMOUNT_VND]
    if not matching_txs:
        raise RuntimeError(f"No payment transaction for {TARGET_PAYMENT_AMOUNT_VND}")
    if not any(str(tx["status"]).lower() == TARGET_PAYMENT_STATUS for tx in matching_txs):
        raise RuntimeError(f"Payment transaction not in {TARGET_PAYMENT_STATUS} state")


def dry_run(connection: sqlite3.Connection, order_id: str) -> None:
    order = fetch_order(connection, order_id)
    items = fetch_linked_items(connection, str(order["id"]) if order else "", order_id)
    txs = fetch_payment_transactions(connection, str(order["id"]) if order else "")
    print_order_summary(order, items, txs)
    validate_targets(connection, order, items, txs)
    before = multi_product_counts(connection)
    print(f"TARGET_COUNTS_BEFORE: {before}")
    print("DRY_RUN_OK: no changes written")


def apply_rollback(connection: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    order = fetch_order(connection, order_id)
    items = fetch_linked_items(connection, str(order["id"]) if order else "", order_id)
    txs = fetch_payment_transactions(connection, str(order["id"]) if order else "")
    print_order_summary(order, items, txs)
    validate_targets(connection, order, items, txs)
    before_all = all_counts(connection)
    before_target = multi_product_counts(connection)
    now = utc_now_iso()
    payment_status, order_status = schema_order_status_choices(connection)

    linked_item_ids = [str(item["inventory_item_id"]) for item in items]
    order_row_id = str(order["id"])

    connection.execute("BEGIN IMMEDIATE")
    try:
        item = items[0]
        if str(item["product_code"]).upper() != TARGET_ORDER_PRODUCT_CODE:
            raise RuntimeError(f"Unexpected linked product_code: {item['product_code']}")
        if str(item["status"]).lower() != "delivered":
            raise RuntimeError(f"Inventory item is not delivered: {item['status']}")
        connection.execute(
            """
            UPDATE inventory_items
            SET status = 'available',
                reserved_order_id = NULL,
                reserved_at = NULL,
                delivered_order_id = NULL,
                delivered_at = NULL,
                disabled_at = NULL
            WHERE id = ?
            """,
            (item["inventory_item_id"],),
        )
        connection.execute(
            """
            UPDATE order_inventory_items
            SET state = 'released',
                released_at = ?
            WHERE order_id = ? AND inventory_item_id = ?
            """,
            (now, order_row_id, item["inventory_item_id"]),
        )
        connection.execute(
            """
            INSERT INTO inventory_movements
                (id, inventory_item_id, action, order_id, source, created_at)
            VALUES (?, ?, 'release', ?, ?, ?)
            """,
            (str(uuid.uuid4()), item["inventory_item_id"], order_row_id, AUDIT_SOURCE, now),
        )
        connection.execute(
            """
            UPDATE orders
            SET payment_status = ?, order_status = ?
            WHERE id = ?
            """,
            (payment_status, order_status, order_row_id),
        )

        after_all = all_counts(connection)
        after_target = multi_product_counts(connection)
        for code, counts in before_all.items():
            if code in TARGET_COUNTS_PRODUCT_CODES:
                continue
            if after_all.get(code, {}) != counts:
                raise RuntimeError(f"Non-target product changed unexpectedly: {code}")
        if after_target.get("CHATGPT", {}).get("available", 0) != before_target.get("CHATGPT", {}).get("available", 0) + 1:
            raise RuntimeError("CHATGPT available count did not restore as expected")
        if after_target.get("CHATGPT", {}).get("delivered", 0) != 0:
            raise RuntimeError("CHATGPT delivered count still nonzero after rollback")
        if after_target.get("CHATGPT_SHARED", {}) != before_target.get("CHATGPT_SHARED", {}):
            raise RuntimeError("CHATGPT_SHARED counts changed unexpectedly")
        connection.commit()
        return {
            "before_target": before_target,
            "after_target": after_target,
            "before_all": before_all,
            "after_all": after_all,
            "linked_item_ids": linked_item_ids,
            "payment_status": payment_status,
            "order_status": order_status,
            "timestamp": now,
        }
    except Exception:
        connection.rollback()
        raise


def backup_path_label(path: Path) -> Path:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"store_before_test_order_rollback_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely rollback one test order on Render SQLite.")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE), help="Path to store.db")
    parser.add_argument("--order-id", required=True, help="External order_id to rollback")
    parser.add_argument("--yes", action="store_true", help="Write changes after backup. Default is dry-run.")
    args = parser.parse_args()

    database_path = resolve_database_path(args.database)
    order_id = str(args.order_id or "").strip()
    if not order_id:
        raise SystemExit("order-id is required")
    if not database_path.is_file():
        raise SystemExit(f"Database not found: {database_path}")

    print("TEST ORDER ROLLBACK SCRIPT")
    print(f"DATABASE: {database_path}")
    print(f"ORDER_ID: {order_id}")
    print(f"TARGET_PRODUCT: {TARGET_ORDER_PRODUCT_CODE}")
    print("NO FULL CREDENTIALS WILL BE PRINTED")

    if not args.yes:
        with closing(connect(database_path)) as connection:
            dry_run(connection, order_id)
        return 0

    backup_path = backup_database(database_path)
    print(f"BACKUP_CREATED: {backup_path}")
    with closing(connect(database_path)) as connection:
        result = apply_rollback(connection, order_id)
    print(f"ROLLBACK_TIMESTAMP_UTC: {result['timestamp']}")
    print(f"ORDER_STATUS_SET_TO: {result['order_status']}")
    print(f"PAYMENT_STATUS_SET_TO: {result['payment_status']}")
    print(f"LINKED_ITEM_IDS: {', '.join(result['linked_item_ids'])}")
    print(f"TARGET_COUNTS_BEFORE: {result['before_target']}")
    print(f"TARGET_COUNTS_AFTER: {result['after_target']}")
    print("ROLLBACK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
