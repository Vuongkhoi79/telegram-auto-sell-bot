from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_CODE = "CAPCUT_30D"
SOLD_EMAIL = "fdfdr03@capcut.team"
RETURNED_EMAIL = "mzzdb72@capcut.team"
DEFAULT_DATABASE = Path(os.environ.get("STORE_DB_PATH", "/var/data/store.db"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-off production inventory repair for two CAPCUT_30D accounts."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to production store.db")
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Omit for dry-run rollback.")
    return parser.parse_args()


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def capcut_30d_available_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE p.code = ? AND i.status = 'available'
        """,
        (PRODUCT_CODE,),
    ).fetchone()
    return int(row["count"] or 0)


def fetch_exact_account(connection: sqlite3.Connection, email: str) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT
            i.id AS inventory_item_id,
            i.product_id,
            p.code AS product_code,
            p.name AS product_name,
            i.secret_value,
            substr(i.secret_value, 1, instr(i.secret_value || '|', '|') - 1) AS email,
            i.status,
            i.reserved_order_id,
            i.delivered_order_id,
            i.created_at,
            i.reserved_at,
            i.delivered_at,
            i.disabled_at
        FROM inventory_items AS i
        JOIN products AS p ON p.id = i.product_id
        WHERE p.code = ?
          AND substr(i.secret_value, 1, instr(i.secret_value || '|', '|') - 1) = ?
        """,
        (PRODUCT_CODE, email),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"ABORT: expected exactly 1 {PRODUCT_CODE} inventory row for {email}, found {len(rows)}"
        )
    return rows[0]


def fetch_links(connection: sqlite3.Connection, inventory_item_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            oi.order_id AS order_row_id,
            oi.inventory_item_id,
            oi.state,
            oi.created_at,
            oi.delivered_at,
            oi.released_at,
            o.order_id AS external_order_id,
            o.payment_status,
            o.order_status
        FROM order_inventory_items AS oi
        LEFT JOIN orders AS o ON o.id = oi.order_id
        WHERE oi.inventory_item_id = ?
        ORDER BY oi.created_at, oi.order_id
        """,
        (inventory_item_id,),
    ).fetchall()


def print_account_state(connection: sqlite3.Connection, label: str, row: sqlite3.Row) -> None:
    links = fetch_links(connection, str(row["inventory_item_id"]))
    print(f"{label}:")
    print(f"  product_code: {row['product_code']}")
    print(f"  inventory_item_id: {row['inventory_item_id']}")
    print(f"  email: {row['email']}")
    print(f"  secret_value: {row['secret_value']}")
    print(f"  status: {row['status']}")
    print(f"  reserved_order_id: {row['reserved_order_id']}")
    print(f"  delivered_order_id: {row['delivered_order_id']}")
    print(f"  reserved_at: {row['reserved_at']}")
    print(f"  delivered_at: {row['delivered_at']}")
    print(f"  disabled_at: {row['disabled_at']}")
    print(f"  order_inventory_links: {len(links)}")
    for link in links:
        print(
            "    "
            f"order_row_id={link['order_row_id']} "
            f"external_order_id={link['external_order_id']} "
            f"state={link['state']} "
            f"payment_status={link['payment_status']} "
            f"order_status={link['order_status']}"
        )


def insert_movement(
    connection: sqlite3.Connection,
    inventory_item_id: str,
    action: str,
    source: str,
    now: str,
    order_row_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_movements
            (id, inventory_item_id, action, order_id, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), inventory_item_id, action, order_row_id, source, now),
    )


def mark_sold_as_delivered(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
    item_id = str(row["inventory_item_id"])
    links = fetch_links(connection, item_id)
    delivered_order_id = str(row["delivered_order_id"] or row["reserved_order_id"] or "manual_sold_before_import")
    connection.execute(
        """
        UPDATE inventory_items
        SET status = 'delivered',
            reserved_order_id = NULL,
            reserved_at = NULL,
            delivered_order_id = ?,
            delivered_at = COALESCE(delivered_at, ?),
            disabled_at = NULL
        WHERE id = ? AND product_id = ?
        """,
        (delivered_order_id, now, item_id, row["product_id"]),
    )
    if links:
        connection.execute(
            """
            UPDATE order_inventory_items
            SET state = 'delivered',
                delivered_at = COALESCE(delivered_at, ?),
                released_at = NULL
            WHERE inventory_item_id = ?
            """,
            (now, item_id),
        )
    insert_movement(
        connection,
        item_id,
        "deliver",
        "manual_capcut_30d_sold_before_import",
        now,
        str(links[0]["order_row_id"]) if links else None,
    )


def return_to_available(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
    item_id = str(row["inventory_item_id"])
    links = fetch_links(connection, item_id)
    connection.execute(
        """
        UPDATE inventory_items
        SET status = 'available',
            reserved_order_id = NULL,
            reserved_at = NULL,
            delivered_order_id = NULL,
            delivered_at = NULL,
            disabled_at = NULL
        WHERE id = ? AND product_id = ?
        """,
        (item_id, row["product_id"]),
    )
    connection.execute(
        "DELETE FROM order_inventory_items WHERE inventory_item_id = ?",
        (item_id,),
    )
    insert_movement(
        connection,
        item_id,
        "release",
        "manual_capcut_30d_return_to_stock",
        now,
        str(links[0]["order_row_id"]) if links else None,
    )


def repair(database: Path, apply: bool) -> int:
    if not database.is_file():
        raise FileNotFoundError(f"store.db not found: {database}")
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            before_stock = capcut_30d_available_count(connection)
            sold_before = fetch_exact_account(connection, SOLD_EMAIL)
            returned_before = fetch_exact_account(connection, RETURNED_EMAIL)

            print(f"Database: {database}")
            print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
            print(f"{PRODUCT_CODE} available stock before: {before_stock}")
            print_account_state(connection, "BEFORE sold-before-import account", sold_before)
            print_account_state(connection, "BEFORE returned-to-stock account", returned_before)

            now = utc_now_iso()
            mark_sold_as_delivered(connection, sold_before, now)
            return_to_available(connection, returned_before, now)

            sold_after = fetch_exact_account(connection, SOLD_EMAIL)
            returned_after = fetch_exact_account(connection, RETURNED_EMAIL)
            after_stock = capcut_30d_available_count(connection)

            if sold_after["status"] in {"available", "reserved"}:
                raise RuntimeError(f"ABORT: {SOLD_EMAIL} is still sellable: {sold_after['status']}")
            if returned_after["status"] != "available":
                raise RuntimeError(f"ABORT: {RETURNED_EMAIL} is not available: {returned_after['status']}")
            if fetch_links(connection, str(returned_after["inventory_item_id"])):
                raise RuntimeError(f"ABORT: {RETURNED_EMAIL} still has order_inventory_items links")

            print(f"{PRODUCT_CODE} available stock after: {after_stock}")
            print_account_state(connection, "AFTER sold-before-import account", sold_after)
            print_account_state(connection, "AFTER returned-to-stock account", returned_after)

            if apply:
                connection.commit()
                print("COMMITTED")
            else:
                connection.rollback()
                print("DRY-RUN ONLY: rolled back. Re-run with --apply to write production.")
        except Exception:
            connection.rollback()
            raise
    return 0


def main() -> int:
    args = parse_args()
    return repair(args.database, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
