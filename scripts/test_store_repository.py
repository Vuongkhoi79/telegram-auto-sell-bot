from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from repository.store_repository import StoreRepository
from scripts.import_inventory import OPTIONAL_COLUMNS, REQUIRED_COLUMNS, import_inventory
from scripts.cleanup_demo_inventory import cleanup_demo_inventory


class StoreRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "store.db"
        source_db = PROJECT_ROOT / "database" / "store.db"
        with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(self.db_path)) as target:
            source.backup(target)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO products (id, code, name, active, delivery_type, created_at, updated_at) VALUES (?, ?, ?, 1, 'account', ?, ?)",
                    ("product-test", "TEST_PRODUCT", "Test Product", now, now),
                )
                connection.execute(
                    """
                    INSERT INTO orders
                        (id, order_id, telegram_user_id, product_code, product_name, package_name, quantity,
                         unit_price_vnd, total_vnd, delivery_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("order-test", "ORD-TEST-001", 1, "TEST_PRODUCT", "Test Product", "TEST", 1, 1, 1, "account", now),
                )
        self.store = StoreRepository(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reserve_deliver_and_duplicate_transaction(self) -> None:
        self.assertIn(
            "TEST_PRODUCT", {product["code"] for product in self.store.list_active_products()}
        )
        self.store.add_inventory_item("TEST_PRODUCT", "first@example.com|pass1")
        self.store.add_inventory_item("TEST_PRODUCT", "second@example.com|pass2")
        self.assertEqual(self.store.get_stock_count("TEST_PRODUCT"), 2)

        reserved = self.store.reserve_inventory_items("ORD-TEST-001", "TEST_PRODUCT", 1)
        self.assertEqual(len(reserved), 1)
        reserved_credential = reserved[0]["credential_text"]
        self.assertEqual(self.store.get_stock_count("TEST_PRODUCT"), 1)

        self.assertTrue(self.store.mark_order_paid("ORD-TEST-001", "SEPAY-TEST-001"))
        delivered = self.store.deliver_reserved_items("ORD-TEST-001")
        self.assertEqual(delivered, [reserved_credential])
        self.assertEqual(self.store.get_stock_count("TEST_PRODUCT"), 1)

        with closing(sqlite3.connect(self.db_path)) as connection:
            status = connection.execute(
                "SELECT status FROM inventory_items WHERE secret_value = ?", (reserved_credential,)
            ).fetchone()[0]
        self.assertEqual(status, "delivered")
        self.assertFalse(self.store.mark_order_paid("ORD-TEST-001", "SEPAY-TEST-001"))
        self.assertEqual(self.store.deliver_reserved_items("ORD-TEST-001"), [reserved_credential])

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO orders
                        (id, order_id, telegram_user_id, product_code, product_name, package_name, quantity,
                         unit_price_vnd, total_vnd, delivery_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("order-test-2", "ORD-TEST-002", 1, "TEST_PRODUCT", "Test Product", "TEST", 2, 1, 2, "account", now),
                )
        with self.assertRaisesRegex(ValueError, "Insufficient available inventory"):
            self.store.reserve_inventory_items("ORD-TEST-002", "TEST_PRODUCT", 2)

    def test_canonical_account_product_codes_resolve_legacy_product_rows(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        initial_stock = self.store.get_stock_count("CHATGPT")
        item_id = self.store.add_inventory_item("CHATGPT", "legacy@example.com|pass")
        self.assertTrue(item_id)
        self.assertEqual(self.store.get_stock_count("CHATGPT"), initial_stock + 1)
        reserved = self.store.create_pending_account_order_and_reserve(
            order_id="ORD-LEGACY-CHATGPT",
            telegram_user_id=1,
            username="Legacy Test",
            product_code="CHATGPT",
            product_name="CHATGPT",
            package_name="7D",
            quantity=1,
            unit_price_vnd=1,
            total_vnd=1,
            created_at=now,
            expire_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        self.assertEqual(len(reserved), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT product_code, product_name FROM orders WHERE order_id = ?",
                ("ORD-LEGACY-CHATGPT",),
            ).fetchone()
        self.assertEqual((row[0], row[1]), ("CHATGPT", "CHATGPT"))

    def test_release_expired_reservations_preserves_paid_and_delivered_items(self) -> None:
        for credential in ("expired@example.com|pass", "paid@example.com|pass", "delivered@example.com|pass"):
            self.store.add_inventory_item("TEST_PRODUCT", credential)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expired_at = (now - timedelta(minutes=1)).isoformat()
        created_at = (now - timedelta(minutes=6)).isoformat()

        def reserve(order_id: str) -> None:
            self.store.create_pending_account_order_and_reserve(
                order_id=order_id,
                telegram_user_id=1,
                username="Test User",
                product_code="TEST_PRODUCT",
                product_name="Test Product",
                package_name="TEST",
                quantity=1,
                unit_price_vnd=1,
                total_vnd=1,
                created_at=created_at,
                expire_at=expired_at,
            )

        reserve("ORD-EXPIRED")
        reserve("ORD-PAID")
        reserve("ORD-DELIVERED")
        self.store.mark_account_order_paid_for_fulfillment("ORD-PAID")
        self.store.mark_account_order_paid_for_fulfillment("ORD-DELIVERED")
        self.assertEqual(len(self.store.deliver_reserved_items("ORD-DELIVERED")), 1)

        self.assertEqual(self.store.release_expired_reservations(), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            order_items = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    """
                    SELECT orders.order_id, inventory_items.status,
                           COALESCE(inventory_items.reserved_order_id, ''), order_inventory_items.state
                    FROM order_inventory_items
                    JOIN orders ON orders.id = order_inventory_items.order_id
                    JOIN inventory_items ON inventory_items.id = order_inventory_items.inventory_item_id
                    """
                )
            }
        self.assertEqual(order_items["ORD-EXPIRED"], ("available", "", "released"))
        self.assertEqual(order_items["ORD-PAID"][0], "reserved")
        self.assertEqual(order_items["ORD-DELIVERED"][0], "delivered")
        self.assertEqual(self.store.release_expired_reservations(), 0)

    def test_admin_stock_operations_and_repeat_import(self) -> None:
        item_ids = [
            self.store.add_inventory_item("TEST_PRODUCT", credential)
            for credential in ("one@example.com|pass", "two@example.com|pass", "three@example.com|pass")
        ]
        with self.assertRaisesRegex(ValueError, "Duplicate credential"):
            self.store.add_inventory_item("TEST_PRODUCT", "one@example.com|pass")

        self.store.set_inventory_item_disabled(item_ids[0], True)
        self.store.set_product_active("TEST_PRODUCT", False)
        with self.assertRaisesRegex(ValueError, "product is disabled"):
            self.store.set_inventory_item_disabled(item_ids[0], False)
        self.store.set_product_active("TEST_PRODUCT", True)
        self.store.set_inventory_item_disabled(item_ids[0], False)

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        reserved_ids = self.store.create_pending_account_order_and_reserve(
            order_id="ORD-ADMIN-RESERVED",
            telegram_user_id=1,
            username="Admin Test",
            product_code="TEST_PRODUCT",
            product_name="Test Product",
            package_name="TEST",
            quantity=1,
            unit_price_vnd=1,
            total_vnd=1,
            created_at=now,
            expire_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        delivered_ids = self.store.create_pending_account_order_and_reserve(
            order_id="ORD-ADMIN-DELIVERED",
            telegram_user_id=1,
            username="Admin Test",
            product_code="TEST_PRODUCT",
            product_name="Test Product",
            package_name="TEST",
            quantity=1,
            unit_price_vnd=1,
            total_vnd=1,
            created_at=now,
            expire_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        self.store.mark_account_order_paid_for_fulfillment("ORD-ADMIN-DELIVERED")
        self.assertEqual(len(self.store.deliver_reserved_items("ORD-ADMIN-DELIVERED")), 1)
        remaining_id = (set(item_ids) - set(reserved_ids) - set(delivered_ids)).pop()
        self.store.set_inventory_item_disabled(remaining_id, True)
        with self.assertRaisesRegex(ValueError, "Only available"):
            self.store.set_inventory_item_disabled(delivered_ids[0], True)

        summary = {row["product_code"]: row for row in self.store.get_stock_summary()}["TEST_PRODUCT"]
        self.assertEqual(
            (summary["active"], summary["available"], summary["reserved"], summary["delivered"], summary["disabled"]),
            (1, 0, 1, 1, 1),
        )

        import_path = Path(self.temp_dir.name) / "inventory.csv"
        with import_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REQUIRED_COLUMNS)
            writer.writerow(["GEMINI", "AI", "Gemini", "private", "30D", 100, 7, "import1@example.com|pass", "", 1])
            writer.writerow(["GEMINI", "AI", "Gemini", "private", "30D", 100, 7, "import2@example.com|pass|2fa", "", 1])
        first_import = import_inventory(import_path, self.db_path)
        second_import = import_inventory(import_path, self.db_path)
        self.assertEqual((first_import["credentials_added"], first_import["credentials_duplicate"], first_import["row_errors"]), (2, 0, 0))
        self.assertEqual((second_import["credentials_added"], second_import["credentials_duplicate"], second_import["row_errors"]), (0, 2, 0))
        self.assertEqual(self.store.get_stock_count("GEMINI"), 2)

        row_isolation_path = Path(self.temp_dir.name) / "row-isolation.csv"
        with row_isolation_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REQUIRED_COLUMNS)
            writer.writerow(["CHATGPT", "AI", "ChatGPT", "private", "30D", 100, 7, "success1@example.com|pass", "", 1])
            writer.writerow(["VEO3", "AI", "VEO3", "private", "30D", 100, 7, "missing@example.com|pass", "", 1])
            writer.writerow(["CHATGPT", "AI", "ChatGPT", "private", "30D", 100, 7, "success2@example.com|pass", "", 1])
        row_isolation = import_inventory(row_isolation_path, self.db_path)
        self.assertEqual(
            (row_isolation["products_created"], row_isolation["credentials_added"], row_isolation["invalid_rows"]),
            (0, 2, 1),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM products WHERE code = 'VEO3'").fetchone())
        self.assertEqual(self.store.get_stock_count("CHATGPT"), 2)

    def test_catalog_schema_and_optional_import_columns(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
        self.assertTrue({"menu_order", "show_in_menu", "product_group", "category_key", "description"}.issubset(columns))

        catalog_path = Path(self.temp_dir.name) / "catalog.csv"
        with catalog_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["product_code", "email", "password", "2fa"])
            writer.writerow(["CHATGPT", "a@example.com", "pass", "2fa"])
            writer.writerow(["NOT_ALLOWED", "b@example.com", "pass", "2fa"])
        report = import_inventory(catalog_path, self.db_path)
        self.assertEqual((report["credentials_added"], report["invalid_rows"]), (1, 1))
        self.assertTrue(any("Unsupported product_code: NOT_ALLOWED" in error for error in report["errors"]))

        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                for code, name, category_key in (
                    ("CATEGORY-CHATGPT", "ChatGPT Plus", "CHATGPT"),
                    ("CATEGORY-GEMINI", "Gemini Advanced", "GEMINI"),
                    ("CATEGORY-GROK", "Grok Super", "GROK"),
                ):
                    connection.execute(
                        """
                        INSERT INTO products
                            (id, code, name, category, category_key, active, delivery_type, created_at, updated_at)
                        VALUES (?, ?, ?, 'AI', ?, 1, 'account', ?, ?)
                        """,
                        (f"test-{code}", code, name, category_key, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
                    )
        categories = {row["category_key"] for row in self.store.list_visible_categories()}
        self.assertTrue({"CHATGPT", "GEMINI", "GROK"}.issubset(categories))
        self.assertNotIn("AI", categories)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "INSERT INTO products (id, code, name, category_key, active, delivery_type, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 'account', ?, ?)",
                    ("mixed-case-chatgpt", "MIXED-CHATGPT", "ChatGPT Alternate", "ChatGPT", now, now),
                )
        self.assertEqual(
            [row["category_key"] for row in self.store.list_visible_categories()].count("CHATGPT"),
            1,
        )

    def test_cleanup_demo_inventory_preserves_real_reserved_and_delivered(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                for product_id, code, note in (("demo-product", "DEMO-CODE", "Example only"), ("real-product", "REAL-CODE", "real supplier stock"), ("protected-demo-code", "DEMO-PROTECTED", "real supplier stock")):
                    connection.execute(
                        "INSERT INTO products (id, code, name, note, active, delivery_type, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 'account', ?, ?)",
                        (product_id, code, code, note, now, now),
                    )
                for item_id, product_id, status in (("demo-available", "demo-product", "available"), ("demo-delivered", "demo-product", "delivered"), ("real-available", "real-product", "available"), ("protected-available", "protected-demo-code", "available")):
                    connection.execute(
                        "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, ?, ?)",
                        (item_id, product_id, item_id + "|pass", status, now),
                    )
        self.assertEqual(cleanup_demo_inventory(self.db_path, {"DEMO-CODE", "DEMO-PROTECTED"}), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            states = dict(connection.execute("SELECT id, status FROM inventory_items WHERE id IN ('demo-available', 'demo-delivered', 'real-available', 'protected-available')").fetchall())
        self.assertEqual(states, {"demo-available": "disabled", "demo-delivered": "delivered", "real-available": "available", "protected-available": "available"})
        stock = {row["product_code"]: row for row in self.store.get_stock_summary()}
        self.assertEqual(stock["DEMO-CODE"]["available"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
