from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from repository.store_repository import StoreRepository


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
        self.assertEqual(self.store.deliver_reserved_items("ORD-TEST-001"), [])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
