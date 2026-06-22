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
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory


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
            writer.writerow(["IMPORT_PRODUCT", "AI", "Import Product", "private", "30D", 100, 7, "import1@example.com|pass", "", 1])
            writer.writerow(["IMPORT_PRODUCT", "AI", "Import Product", "private", "30D", 100, 7, "import2@example.com|pass|2fa", "", 1])
        first_import = import_inventory(import_path, self.db_path)
        second_import = import_inventory(import_path, self.db_path)
        self.assertEqual((first_import["credentials_added"], first_import["credentials_duplicate"], first_import["row_errors"]), (2, 0, 0))
        self.assertEqual((second_import["credentials_added"], second_import["credentials_duplicate"], second_import["row_errors"]), (0, 2, 0))
        self.assertEqual(self.store.get_stock_count("IMPORT_PRODUCT"), 2)

        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_row_failure
                    BEFORE INSERT ON inventory_items
                    WHEN (SELECT code FROM products WHERE id = NEW.product_id) = 'ROW_FAILURE'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced row failure');
                    END
                    """
                )
        row_isolation_path = Path(self.temp_dir.name) / "row-isolation.csv"
        with row_isolation_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REQUIRED_COLUMNS)
            writer.writerow(["ROW_SUCCESS", "AI", "Row Success", "private", "30D", 100, 7, "success1@example.com|pass", "", 1])
            writer.writerow(["ROW_FAILURE", "AI", "Row Failure", "private", "30D", 100, 7, "failure@example.com|pass", "", 1])
            writer.writerow(["ROW_SUCCESS", "AI", "Row Success", "private", "30D", 100, 7, "success2@example.com|pass", "", 1])
        row_isolation = import_inventory(row_isolation_path, self.db_path)
        self.assertEqual(
            (row_isolation["products_created"], row_isolation["credentials_added"], row_isolation["invalid_rows"]),
            (1, 2, 1),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM products WHERE code = 'ROW_FAILURE'").fetchone())
        self.assertEqual(self.store.get_stock_count("ROW_SUCCESS"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
