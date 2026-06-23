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

CATALOG_COLUMNS = {
    "menu_order": "INTEGER NOT NULL DEFAULT 100",
    "show_in_menu": "INTEGER NOT NULL DEFAULT 1",
    "product_group": "TEXT NOT NULL DEFAULT 'account'",
    "category_key": "TEXT NOT NULL DEFAULT ''",
    "description": "TEXT NOT NULL DEFAULT ''",
}

ORDER_COLUMNS = {
    "inventory_source": "TEXT NOT NULL DEFAULT 'sqlite'",
    "duration_days": "INTEGER",
    "expire_date": "TEXT NOT NULL DEFAULT ''",
    "lifetime": "INTEGER NOT NULL DEFAULT 0 CHECK (lifetime IN (0, 1))",
}

ACCOUNT_PRODUCT_CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "ADOBE": ("ADOBE-1M-PRIVATE",),
    "ARTLIST": ("ARTLIST-1M-PRIVATE",),
    "CANVA": ("CANVA-PRO-1M-PRIVATE",),
    "CANVA PRO": ("CANVA-PRO-1M-PRIVATE",),
    "CAPCUT": ("CAPCUT-PRO-1M-PRIVATE",),
    "CAPCUT PRO": ("CAPCUT-PRO-1M-PRIVATE",),
    "CHATGPT": ("GPT-PLUS-1M-PRIVATE",),
    "CLAUDE": ("CLAUDE-PRO-1M-PRIVATE",),
    "CLAUDE AI": ("CLAUDE-PRO-1M-PRIVATE",),
    "CURSOR": ("CURSOR-PRO-1M-PRIVATE",),
    "CURSOR AI": ("CURSOR-PRO-1M-PRIVATE",),
    "ELEVEN": ("ELEVENLABS-1M-PRIVATE",),
    "ELEVENLABS": ("ELEVENLABS-1M-PRIVATE",),
    "GAMMA": ("GAMMA-1M-PRIVATE",),
    "GAMMA AI": ("GAMMA-1M-PRIVATE",),
    "GEMINI": ("GEM-AIPRO-1M-PRIVATE",),
    "GEMINI AI": ("GEM-AIPRO-1M-PRIVATE",),
    "GROK": ("GROK-SUPER-1M-PRIVATE",),
    "GROK SUPER": ("GROK-SUPER-1M-PRIVATE",),
    "HEYGEN": ("HEYGEN-1M-PRIVATE",),
    "HEYGEN AI": ("HEYGEN-1M-PRIVATE",),
    "HIGGFIELD": ("HIGGSFIELD-1M-PRIVATE",),
    "HIGGSFIELD": ("HIGGSFIELD-1M-PRIVATE",),
    "KLING": ("KLING-1M-PRIVATE",),
    "KREA": ("KREA-1M-PRIVATE",),
    "KREA AI": ("KREA-1M-PRIVATE",),
    "OPENART": ("OPENART-1M-PRIVATE",),
    "OPENART AI": ("OPENART-1M-PRIVATE",),
    "SUNO": ("SUNO-1M-PRIVATE",),
    "SUNO AI": ("SUNO-1M-PRIVATE",),
    "VEO3": ("VEO3-1M-PRIVATE",),
    "VEO3 ULTRA": ("VEO3-1M-PRIVATE",),
    "VIEWMAX": ("VIEWMAX-1M-PRIVATE",),
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
        with self._session() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
            for name, definition in CATALOG_COLUMNS.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
            order_columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
            for name, definition in ORDER_COLUMNS.items():
                if name not in order_columns:
                    connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")

    def _product_code_candidates(self, product_code: str) -> list[str]:
        normalized = str(product_code or "").strip().upper()
        candidates = [normalized] if normalized else []
        for alias in ACCOUNT_PRODUCT_CODE_ALIASES.get(normalized, ()):
            alias = str(alias).strip().upper()
            if alias and alias not in candidates:
                candidates.append(alias)
        return candidates

    def _resolve_active_product(self, connection: sqlite3.Connection, product_code: str) -> sqlite3.Row | None:
        for candidate in self._product_code_candidates(product_code):
            row = connection.execute(
                "SELECT id, code, name, active FROM products WHERE code = ? AND active = 1",
                (candidate,),
            ).fetchone()
            if row:
                return row
        return None

    def list_active_products(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, code, name, delivery_type FROM products WHERE active = 1 ORDER BY code"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_catalog_products(self) -> list[dict[str, Any]]:
        """List active catalog products in display-name order for the Telegram menu."""
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT id, code, name, active, delivery_type
                FROM products
                WHERE active = 1
                ORDER BY name COLLATE NOCASE, code
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _order_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        delivery = str(data.get("delivery_ref", "") or "")
        data["delivery"] = delivery
        data["license_file"] = delivery
        data["inventory_source"] = str(data.get("inventory_source", "") or "sqlite")
        data["price_vnd"] = int(data.get("unit_price_vnd", 0) or 0)
        data["unit_price"] = int(data.get("unit_price_vnd", 0) or 0)
        data["amount"] = int(data.get("total_vnd", 0) or 0)
        data["total"] = int(data.get("total_vnd", 0) or 0)
        data["status"] = str(data.get("payment_status", "") or "pending")
        data["package_code"] = str(data.get("product_code", "") or "")
        data["expire_date"] = str(data.get("expire_date", "") or "")
        data["lifetime"] = bool(int(data.get("lifetime", 0) or 0))
        return data

    def list_orders(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT id, order_id, telegram_user_id, username, product_id, product_code,
                       product_name, package_name, quantity, unit_price_vnd, total_vnd,
                       delivery_type, machine_id, plan, payment_method, payment_status,
                       order_status, transaction_id, created_at, expire_at, paid_at,
                       delivered_at, delivery_ref, inventory_source, duration_days,
                       expire_date, lifetime
                FROM orders
                ORDER BY created_at, order_id
                """
            ).fetchall()
        return [self._order_row_to_dict(row) for row in rows]

    def find_order(self, order_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT id, order_id, telegram_user_id, username, product_id, product_code,
                       product_name, package_name, quantity, unit_price_vnd, total_vnd,
                       delivery_type, machine_id, plan, payment_method, payment_status,
                       order_status, transaction_id, created_at, expire_at, paid_at,
                       delivered_at, delivery_ref, inventory_source, duration_days,
                       expire_date, lifetime
                FROM orders
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
        return self._order_row_to_dict(row) if row else None

    def list_pending_orders(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT id, order_id, telegram_user_id, username, product_id, product_code,
                       product_name, package_name, quantity, unit_price_vnd, total_vnd,
                       delivery_type, machine_id, plan, payment_method, payment_status,
                       order_status, transaction_id, created_at, expire_at, paid_at,
                       delivered_at, delivery_ref, inventory_source, duration_days,
                       expire_date, lifetime
                FROM orders
                WHERE payment_status = 'pending'
                ORDER BY created_at, order_id
                """
            ).fetchall()
        return [self._order_row_to_dict(row) for row in rows]

    def upsert_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order_id = str(order.get("order_id", "")).strip()
        if not order_id:
            raise ValueError("order_id must not be empty")
        now = _utc_now_iso()
        total_vnd = int(order.get("total_vnd", order.get("total", order.get("amount", 0))) or 0)
        unit_price_vnd = int(order.get("unit_price_vnd", order.get("unit_price", order.get("price_vnd", 0))) or 0)
        delivery_ref = str(order.get("delivery_ref", order.get("delivery", order.get("license_file", ""))) or "")
        payload = (
            str(order.get("id", uuid.uuid4())),
            order_id,
            int(order.get("telegram_user_id", 0) or 0),
            str(order.get("username", "") or ""),
            str(order.get("product_id", "") or ""),
            str(order.get("product_code", order.get("product_id", order.get("package_code", ""))) or ""),
            str(order.get("product_name", "") or ""),
            str(order.get("package_name", "") or ""),
            int(order.get("quantity", 1) or 1),
            unit_price_vnd,
            total_vnd,
            str(order.get("delivery_type", "account") or "account"),
            str(order.get("machine_id", "") or ""),
            str(order.get("plan", "") or ""),
            str(order.get("payment_method", "") or ""),
            str(order.get("payment_status", "pending") or "pending"),
            str(order.get("order_status", order.get("payment_status", "pending")) or "pending"),
            str(order.get("transaction_id", "") or ""),
            str(order.get("created_at", now) or now),
            str(order.get("expire_at", "") or ""),
            str(order.get("paid_at", "") or ""),
            str(order.get("delivered_at", "") or ""),
            delivery_ref,
            str(order.get("inventory_source", "sqlite") or "sqlite"),
            order.get("duration_days"),
            str(order.get("expire_date", "") or ""),
            1 if bool(order.get("lifetime", False)) else 0,
        )
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE orders
                    SET telegram_user_id = ?, username = ?, product_id = ?, product_code = ?,
                        product_name = ?, package_name = ?, quantity = ?, unit_price_vnd = ?,
                        total_vnd = ?, delivery_type = ?, machine_id = ?, plan = ?,
                        payment_method = ?, payment_status = ?, order_status = ?, transaction_id = ?,
                        created_at = ?, expire_at = ?, paid_at = ?, delivered_at = ?, delivery_ref = ?,
                        inventory_source = ?, duration_days = ?, expire_date = ?, lifetime = ?
                    WHERE order_id = ?
                    """,
                    payload[2:] + (order_id,),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO orders
                        (id, order_id, telegram_user_id, username, product_id, product_code,
                         product_name, package_name, quantity, unit_price_vnd, total_vnd,
                         delivery_type, machine_id, plan, payment_method, payment_status,
                         order_status, transaction_id, created_at, expire_at, paid_at,
                         delivered_at, delivery_ref, inventory_source, duration_days,
                         expire_date, lifetime)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
        return self.find_order(order_id) or {}

    def update_order(self, order_id: str, **changes: Any) -> dict[str, Any] | None:
        order = self.find_order(order_id)
        if not order:
            return None
        if "payment_status" in changes and "status" not in changes:
            changes["status"] = changes["payment_status"]
        updates: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "status":
                updates["payment_status"] = value
                updates["order_status"] = value if value else order.get("order_status", "")
            elif key == "delivery":
                updates["delivery_ref"] = value
            elif key == "license_file":
                updates["delivery_ref"] = value
            elif key == "price_vnd":
                updates["unit_price_vnd"] = value
            elif key == "amount":
                updates["total_vnd"] = value
            elif key == "package_code":
                updates["product_code"] = value
            else:
                updates[key] = value
        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = list(updates.values()) + [order_id]
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"UPDATE orders SET {assignments} WHERE order_id = ?", values)
        return self.find_order(order_id)

    def order_exists(self, order_id: str) -> bool:
        with self._session() as connection:
            row = connection.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return bool(row)

    def list_visible_categories(self, product_group: str = "account") -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT UPPER(COALESCE(NULLIF(p.category_key, ''), NULLIF(p.name, ''), p.code)) AS category_key,
                       MIN(p.menu_order) AS menu_order,
                       SUM(CASE WHEN i.status = 'available' THEN 1 ELSE 0 END) AS available_count
                FROM products
                AS p LEFT JOIN inventory_items AS i ON i.product_id = p.id
                WHERE p.active = 1 AND p.show_in_menu = 1 AND p.product_group = ?
                GROUP BY UPPER(COALESCE(NULLIF(p.category_key, ''), NULLIF(p.name, ''), p.code))
                ORDER BY menu_order, category_key COLLATE NOCASE
                """,
                (product_group,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_packages_by_category(self, category_key: str, product_group: str = "account") -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT id, code AS product_code, name AS display_name, category_key,
                       description, price_vnd, active, menu_order, product_group
                FROM products
                WHERE active = 1 AND product_group = ?
                  AND COALESCE(NULLIF(category_key, ''), NULLIF(category, ''), code) = ?
                ORDER BY menu_order, display_name COLLATE NOCASE, product_code
                """,
                (product_group, category_key.upper()),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_product_details(self, product_code: str) -> dict[str, Any] | None:
        """Read one product and its import metadata without changing inventory state."""
        with self._session() as connection:
            row = None
            for candidate in self._product_code_candidates(product_code):
                row = connection.execute(
                    """
                    SELECT id, code, name, active, delivery_type, category, account_type,
                           duration, price_vnd, warranty_days, note
                    FROM products
                    WHERE code = ?
                    """,
                    (candidate,),
                ).fetchone()
                if row:
                    break
        return dict(row) if row else None

    def get_stock_count(self, product_code: str) -> int:
        with self._session() as connection:
            row = None
            for candidate in self._product_code_candidates(product_code):
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS stock
                    FROM inventory_items AS item
                    JOIN products AS product ON product.id = item.product_id
                    WHERE product.code = ? AND item.status = 'available'
                    """,
                    (candidate,),
                ).fetchone()
                if row and int(row["stock"] or 0) > 0:
                    break
        return int(row["stock"] if row else 0)

    def get_stock_summary(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT p.code AS product_code, p.name AS display_name, p.active,
                       SUM(CASE WHEN i.status = 'available' THEN 1 ELSE 0 END) AS available,
                       SUM(CASE WHEN i.status = 'reserved' THEN 1 ELSE 0 END) AS reserved,
                       SUM(CASE WHEN i.status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                       SUM(CASE WHEN i.status = 'disabled' THEN 1 ELSE 0 END) AS disabled
                FROM products AS p
                LEFT JOIN inventory_items AS i ON i.product_id = p.id
                GROUP BY p.id, p.code, p.name, p.active
                ORDER BY p.code
                """
            ).fetchall()
        return [
            {
                **dict(row),
                "available": int(row["available"] or 0),
                "reserved": int(row["reserved"] or 0),
                "delivered": int(row["delivered"] or 0),
                "disabled": int(row["disabled"] or 0),
            }
            for row in rows
        ]

    def add_inventory_item(self, product_code: str, credential_text: str) -> str:
        credential_text = credential_text.strip()
        if not credential_text:
            raise ValueError("credential_text must not be empty")
        now = _utc_now_iso()
        item_id = str(uuid.uuid4())
        with self._session() as connection:
            product = self._resolve_active_product(connection, product_code)
            if not product:
                raise ValueError(f"Unknown product code: {product_code}")
            duplicate = connection.execute(
                "SELECT id FROM inventory_items WHERE product_id = ? AND secret_value = ?",
                (product["id"], credential_text),
            ).fetchone()
            if duplicate:
                raise ValueError(f"Duplicate credential for product code: {product_code}")
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

    def set_product_active(self, product_code: str, active: bool) -> None:
        now = _utc_now_iso()
        with self._session() as connection:
            product = None
            for candidate in self._product_code_candidates(product_code):
                product = connection.execute(
                    "SELECT id, code, name, active FROM products WHERE code = ?",
                    (candidate,),
                ).fetchone()
                if product:
                    break
            if not product:
                raise ValueError(f"Unknown product code: {product_code}")
            result = connection.execute(
                "UPDATE products SET active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, now, product["id"]),
            )
            if result.rowcount != 1:
                raise ValueError(f"Unknown product code: {product_code}")

    def set_inventory_item_disabled(self, item_id: str, disabled: bool) -> None:
        now = _utc_now_iso()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT i.status, p.active
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
            if not item:
                raise ValueError(f"Unknown inventory item: {item_id}")
            if disabled:
                if item["status"] != "available":
                    raise ValueError("Only available inventory items can be disabled")
                connection.execute(
                    "UPDATE inventory_items SET status = 'disabled', disabled_at = ? WHERE id = ?",
                    (now, item_id),
                )
                action, source = "disable", "admin_disable"
            else:
                if item["status"] != "disabled":
                    raise ValueError("Only disabled inventory items can be enabled")
                if not item["active"]:
                    raise ValueError("Cannot enable an item while its product is disabled")
                connection.execute(
                    "UPDATE inventory_items SET status = 'available', disabled_at = NULL WHERE id = ?",
                    (item_id,),
                )
                # Existing schema has no `enable` action; `release` plus source preserves audit history.
                action, source = "release", "admin_enable"
            connection.execute(
                """
                INSERT INTO inventory_movements
                    (id, inventory_item_id, action, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), item_id, action, source, now),
            )

    def create_pending_account_order_and_reserve(
        self,
        *,
        order_id: str,
        telegram_user_id: int,
        username: str,
        product_code: str,
        product_name: str,
        package_name: str,
        quantity: int,
        unit_price_vnd: int,
        total_vnd: int,
        created_at: str,
        expire_at: str,
    ) -> list[str]:
        """Atomically mirror an account order and reserve its available items.

        Repeating the same order_id returns its existing reservation without
        allocating any additional inventory.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if existing:
                rows = connection.execute(
                    """
                    SELECT inventory_item_id FROM order_inventory_items
                    WHERE order_id = ? AND state IN ('reserved', 'delivered')
                    ORDER BY created_at, inventory_item_id
                    """,
                    (existing["id"],),
                ).fetchall()
                return [str(row["inventory_item_id"]) for row in rows]

            product = self._resolve_active_product(connection, product_code)
            if not product:
                raise ValueError(f"Mapped SQLite product is unavailable: {product_code}")
            items = connection.execute(
                """
                SELECT id FROM inventory_items
                WHERE product_id = ? AND status = 'available'
                ORDER BY created_at, id
                LIMIT ?
                """,
                (product["id"], quantity),
            ).fetchall()
            if len(items) != quantity:
                raise ValueError(f"Insufficient available inventory for {product_code}")

            internal_order_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO orders
                    (id, order_id, telegram_user_id, username, product_id, product_code,
                     product_name, package_name, quantity, unit_price_vnd, total_vnd,
                     delivery_type, payment_status, order_status, created_at, expire_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'account', 'pending', 'reserved', ?, ?)
                """,
                (
                    internal_order_id,
                    order_id,
                    telegram_user_id,
                    username,
                    product["id"],
                    product_code.upper(),
                    product_name,
                    package_name,
                    quantity,
                    unit_price_vnd,
                    total_vnd,
                    created_at,
                    expire_at,
                ),
            )
            for item in items:
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET status = 'reserved', reserved_order_id = ?, reserved_at = ?
                    WHERE id = ? AND status = 'available'
                    """,
                    (order_id, created_at, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO order_inventory_items
                        (order_id, inventory_item_id, state, created_at)
                    VALUES (?, ?, 'reserved', ?)
                    """,
                    (internal_order_id, item["id"], created_at),
                )
                connection.execute(
                    """
                    INSERT INTO inventory_movements
                        (id, inventory_item_id, action, order_id, source, created_at)
                    VALUES (?, ?, 'reserve', ?, 'account_order_create', ?)
                    """,
                    (str(uuid.uuid4()), item["id"], internal_order_id, created_at),
                )
        return [str(item["id"]) for item in items]

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
            items = []
            for candidate in self._product_code_candidates(product_code):
                items = connection.execute(
                    """
                    SELECT item.id, item.secret_value
                    FROM inventory_items AS item
                    JOIN products AS product ON product.id = item.product_id
                    WHERE product.code = ? AND item.status = 'available'
                    ORDER BY item.created_at, item.id
                    LIMIT ?
                    """,
                    (candidate, quantity),
                ).fetchall()
                if len(items) == quantity:
                    break
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

    def mark_account_order_paid_for_fulfillment(self, order_id: str) -> bool:
        """Mark a reserved account order paid after the existing payment flow confirms it."""
        now = _utc_now_iso()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT id, payment_status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                return False
            if order["payment_status"] == "pending":
                connection.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'paid', order_status = 'paid', paid_at = ?
                    WHERE id = ?
                    """,
                    (now, order["id"]),
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
                delivered_items = connection.execute(
                    """
                    SELECT item.secret_value
                    FROM order_inventory_items AS oi
                    JOIN inventory_items AS item ON item.id = oi.inventory_item_id
                    WHERE oi.order_id = ? AND oi.state = 'delivered'
                    ORDER BY oi.created_at, item.id
                    """,
                    (order["id"],),
                ).fetchall()
                return [str(item["secret_value"]) for item in delivered_items]
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
