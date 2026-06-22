"""Minimal SQLite access layer for store.db.

This module is intentionally not wired into the Telegram bot yet.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REQUIRED_TABLES = {
    "products",
    "inventory_items",
    "orders",
    "order_inventory_items",
    "payment_transactions",
    "inventory_movements",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


class StoreRepository:
    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self._verify_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_schema(self) -> None:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Store database not found: {self.database_path}")
        with self._session() as connection:
            found = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        missing = REQUIRED_TABLES - found
        if missing:
            raise RuntimeError(f"Store database schema is missing tables: {', '.join(sorted(missing))}")

    def list_active_products(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, code, name, delivery_type FROM products WHERE active = 1 ORDER BY code"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_stock_count(self, product_code: str) -> int:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS stock
                FROM inventory_items AS item
                JOIN products AS product ON product.id = item.product_id
                WHERE product.code = ? AND item.status = 'available'
                """,
                (product_code.upper(),),
            ).fetchone()
        return int(row["stock"] if row else 0)

    def add_inventory_item(self, product_code: str, credential_text: str) -> str:
        credential_text = credential_text.strip()
        if not credential_text:
            raise ValueError("credential_text must not be empty")
        now = _utc_now_iso()
        item_id = str(uuid.uuid4())
        with self._session() as connection:
            product = connection.execute(
                "SELECT id FROM products WHERE code = ?", (product_code.upper(),)
            ).fetchone()
            if not product:
                raise ValueError(f"Unknown product code: {product_code}")
            connection.execute(
                "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                (item_id, product["id"], credential_text, now),
            )
            connection.execute(
                """
                INSERT INTO inventory_movements
                    (id, inventory_item_id, action, source, created_at)
                VALUES (?, ?, 'import', 'store_repository', ?)
                """,
                (str(uuid.uuid4()), item_id, now),
            )
        return item_id

    def reserve_inventory_items(
        self, order_id: str, product_code: str, quantity: int, expire_minutes: int = 5
    ) -> list[dict[str, str]]:
        if quantity <= 0 or expire_minutes <= 0:
            raise ValueError("quantity and expire_minutes must be positive")
        now = _utc_now()
        now_iso = now.isoformat()
        expire_at = (now + timedelta(minutes=expire_minutes)).isoformat()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT id, payment_status, order_status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise ValueError(f"Unknown order ID: {order_id}")
            if order["payment_status"] != "pending" or order["order_status"] not in {"pending", "reserved"}:
                raise ValueError(f"Order cannot reserve inventory: {order_id}")
            items = connection.execute(
                """
                SELECT item.id, item.secret_value
                FROM inventory_items AS item
                JOIN products AS product ON product.id = item.product_id
                WHERE product.code = ? AND item.status = 'available'
                ORDER BY item.created_at, item.id
                LIMIT ?
                """,
                (product_code.upper(), quantity),
            ).fetchall()
            if len(items) != quantity:
                raise ValueError(f"Insufficient available inventory for {product_code}")
            connection.execute(
                "UPDATE orders SET order_status = 'reserved', expire_at = ? WHERE id = ?",
                (expire_at, order["id"]),
            )
            for item in items:
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET status = 'reserved', reserved_order_id = ?, reserved_at = ?
                    WHERE id = ? AND status = 'available'
                    """,
                    (order_id, now_iso, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO order_inventory_items
                        (order_id, inventory_item_id, state, created_at)
                    VALUES (?, ?, 'reserved', ?)
                    """,
                    (order["id"], item["id"], now_iso),
                )
                connection.execute(
                    """
                    INSERT INTO inventory_movements
                        (id, inventory_item_id, action, order_id, source, created_at)
                    VALUES (?, ?, 'reserve', ?, 'store_repository', ?)
                    """,
                    (str(uuid.uuid4()), item["id"], order["id"], now_iso),
                )
        return [{"id": item["id"], "credential_text": item["secret_value"]} for item in items]

    def release_expired_reservations(self) -> int:
        now = _utc_now_iso()
        released = 0
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT oi.order_id, oi.inventory_item_id
                FROM order_inventory_items AS oi
                JOIN orders AS o ON o.id = oi.order_id
                WHERE oi.state = 'reserved' AND o.payment_status = 'pending'
                  AND o.expire_at IS NOT NULL AND o.expire_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET status = 'available', reserved_order_id = NULL, reserved_at = NULL
                    WHERE id = ? AND status = 'reserved'
                    """,
                    (row["inventory_item_id"],),
                )
                connection.execute(
                    "UPDATE order_inventory_items SET state = 'released', released_at = ? WHERE order_id = ? AND inventory_item_id = ?",
                    (now, row["order_id"], row["inventory_item_id"]),
                )
                connection.execute(
                    "INSERT INTO inventory_movements (id, inventory_item_id, action, order_id, source, created_at) VALUES (?, ?, 'release', ?, 'store_repository', ?)",
                    (str(uuid.uuid4()), row["inventory_item_id"], row["order_id"], now),
                )
                released += 1
            if rows:
                connection.execute(
                    "UPDATE orders SET order_status = 'expired', payment_status = 'expired' WHERE payment_status = 'pending' AND expire_at IS NOT NULL AND expire_at <= ?",
                    (now,),
                )
        return released

    def mark_order_paid(self, order_id: str, transaction_id: str) -> bool:
        transaction_id = transaction_id.strip()
        if not transaction_id:
            raise ValueError("transaction_id must not be empty")
        now = _utc_now_iso()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT id, total_vnd, payment_status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order or order["payment_status"] != "pending":
                return False
            try:
                connection.execute(
                    """
                    INSERT INTO payment_transactions
                        (id, provider, provider_transaction_id, order_id, amount_vnd, status, received_at, processed_at)
                    VALUES (?, 'SEPAY', ?, ?, ?, 'processed', ?, ?)
                    """,
                    (str(uuid.uuid4()), transaction_id, order["id"], order["total_vnd"], now, now),
                )
            except sqlite3.IntegrityError:
                return False
            connection.execute(
                """
                UPDATE orders
                SET payment_status = 'paid', order_status = 'paid', transaction_id = ?, payment_method = 'SEPAY', paid_at = ?
                WHERE id = ?
                """,
                (transaction_id, now, order["id"]),
            )
        return True

    def deliver_reserved_items(self, order_id: str) -> list[str]:
        now = _utc_now_iso()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT id, payment_status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise ValueError(f"Unknown order ID: {order_id}")
            if order["payment_status"] != "paid":
                raise ValueError(f"Order is not paid: {order_id}")
            items = connection.execute(
                """
                SELECT item.id, item.secret_value
                FROM order_inventory_items AS oi
                JOIN inventory_items AS item ON item.id = oi.inventory_item_id
                WHERE oi.order_id = ? AND oi.state = 'reserved'
                ORDER BY oi.created_at, item.id
                """,
                (order["id"],),
            ).fetchall()
            if not items:
                return []
            for item in items:
                connection.execute(
                    "UPDATE inventory_items SET status = 'delivered', delivered_order_id = ?, delivered_at = ? WHERE id = ?",
                    (order_id, now, item["id"]),
                )
                connection.execute(
                    "UPDATE order_inventory_items SET state = 'delivered', delivered_at = ? WHERE order_id = ? AND inventory_item_id = ?",
                    (now, order["id"], item["id"]),
                )
                connection.execute(
                    "INSERT INTO inventory_movements (id, inventory_item_id, action, order_id, source, created_at) VALUES (?, ?, 'deliver', ?, 'store_repository', ?)",
                    (str(uuid.uuid4()), item["id"], order["id"], now),
                )
            connection.execute(
                "UPDATE orders SET order_status = 'delivered', delivered_at = ? WHERE id = ?",
                (now, order["id"]),
            )
        return [str(item["secret_value"]) for item in items]
