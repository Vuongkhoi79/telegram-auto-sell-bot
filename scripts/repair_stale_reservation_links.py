from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair stale reservation links in SQLite.")
    parser.add_argument("--database", required=True, help="Path to store.db")
    return parser.parse_args()


def print_schema(connection: sqlite3.Connection) -> None:
    for table in ("products", "inventory_items", "orders", "order_inventory_items"):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        print(f"{table} columns: {columns}")


def stale_link_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM order_inventory_items AS oi
        JOIN inventory_items AS i ON i.id = oi.inventory_item_id
        LEFT JOIN orders AS o ON o.id = oi.order_id
        WHERE i.status = 'available'
          AND (
                o.id IS NULL
             OR COALESCE(o.payment_status, 'pending') <> 'paid'
             OR COALESCE(o.order_status, 'pending') NOT IN ('paid', 'delivered', 'manual_delivery')
          )
        """
    ).fetchone()
    return int(row[0] if row else 0)


def repair(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        print_schema(connection)
        before = stale_link_count(connection)
        print(f"stale links before: {before}")
        try:
            with connection:
                connection.execute(
                    """
                    DELETE FROM order_inventory_items
                    WHERE inventory_item_id IN (
                        SELECT oi.inventory_item_id
                        FROM order_inventory_items AS oi
                        JOIN inventory_items AS i ON i.id = oi.inventory_item_id
                        LEFT JOIN orders AS o ON o.id = oi.order_id
                        WHERE i.status = 'available'
                          AND (
                                o.id IS NULL
                             OR COALESCE(o.payment_status, 'pending') <> 'paid'
                             OR COALESCE(o.order_status, 'pending') NOT IN ('paid', 'delivered', 'manual_delivery')
                          )
                    )
                    """
                )
        except sqlite3.OperationalError as exc:
            print(f"repair failed: {exc}")
            return 1
        after = stale_link_count(connection)
        print(f"stale links after: {after}")
        return 0 if after == 0 else 1


def main() -> int:
    args = parse_args()
    return repair(Path(args.database))


if __name__ == "__main__":
    raise SystemExit(main())
