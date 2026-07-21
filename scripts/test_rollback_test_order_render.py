from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "rollback_test_order_render.py"


class RollbackTestOrderRenderScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "store.db"
        source_db = PROJECT_ROOT / "database" / "store.db"
        with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(self.db_path)) as target:
            source.backup(target)
        self.order_id = "ORD-20260721093007-CHATGPT-246F7BE7"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO products
                        (id, code, name, active, delivery_type, created_at, updated_at, category, account_type, duration, price_vnd, warranty_days, note, category_key, product_group, menu_order, show_in_menu)
                    VALUES (?, 'CHATGPT_SHARED', 'ChatGPT Plus dùng chung', 1, 'account', ?, ?, 'account', 'shared', '30D', 45000, 7, '', 'CHATGPT_SHARED', 'account', 100, 1)
                    """,
                    ("chatgpt-shared", now, now),
                )
                connection.execute("DELETE FROM inventory_items WHERE product_id = ?", ("chatgpt-shared",))
                connection.execute("DELETE FROM order_inventory_items WHERE order_id IN (SELECT id FROM orders WHERE order_id = ?)", (self.order_id,))
                connection.execute("DELETE FROM orders WHERE order_id = ?", (self.order_id,))
                connection.execute("DELETE FROM payment_transactions WHERE provider_transaction_id = 'TEST-TX-ROLLBACK-1'")
                connection.execute(
                    """
                    INSERT INTO inventory_items
                        (id, product_id, secret_value, status, reserved_order_id, delivered_order_id, created_at, reserved_at, delivered_at, disabled_at)
                    VALUES (?, ?, ?, 'delivered', ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        "chatgpt-shared-item-1",
                        "chatgpt-shared",
                        "shared@example.com|password|SLOT-01",
                        self.order_id,
                        self.order_id,
                        now,
                        now,
                        now,
                    ),
                )
                order_row_id = "order-row-chatgpt-shared-1"
                connection.execute(
                    """
                    INSERT INTO orders
                        (id, order_id, telegram_user_id, username, product_id, product_code, product_name,
                         package_name, quantity, unit_price_vnd, total_vnd, delivery_type, machine_id,
                         plan, payment_method, payment_status, order_status, transaction_id, created_at,
                         paid_at, delivered_at, delivery_ref)
                    VALUES (?, ?, 1, 'tester', ?, 'CHATGPT_SHARED', 'ChatGPT Plus dùng chung', '30D', 1, 45000, 45000,
                            'account', '', '', 'SEPAY', 'paid', 'delivered', 'TEST-TX-ROLLBACK-1', ?, ?, ?, 'shared@example.com|password')
                    """,
                    (order_row_id, self.order_id, "chatgpt-shared", now, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO order_inventory_items (order_id, inventory_item_id, state, created_at, delivered_at, released_at)
                    VALUES (?, ?, 'delivered', ?, ?, NULL)
                    """,
                    (order_row_id, "chatgpt-shared-item-1", now, now),
                )
                connection.execute(
                    """
                    INSERT INTO payment_transactions
                        (id, provider, provider_transaction_id, order_id, amount_vnd, description, raw_payload_json, status, received_at, processed_at)
                    VALUES (?, 'SEPAY', 'TEST-TX-ROLLBACK-1', ?, 45000, 'test payment', '{}', 'processed', ?, ?)
                    """,
                    ("payment-tx-1", order_row_id, now, now),
                )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--database", str(self.db_path), "--order-id", self.order_id, *args],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_and_apply(self) -> None:
        dry = self.run_script()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("DRY_RUN_OK", dry.stdout)
        with closing(sqlite3.connect(self.db_path)) as connection:
            before = connection.execute(
                "SELECT status FROM inventory_items WHERE id = 'chatgpt-shared-item-1'"
            ).fetchone()[0]
        self.assertEqual(before, "delivered")

        apply = self.run_script("--yes")
        self.assertEqual(apply.returncode, 0, apply.stdout + apply.stderr)
        self.assertIn("ROLLBACK_OK", apply.stdout)
        with closing(sqlite3.connect(self.db_path)) as connection:
            item = connection.execute(
                """
                SELECT status, reserved_order_id, delivered_order_id, reserved_at, delivered_at
                FROM inventory_items WHERE id = 'chatgpt-shared-item-1'
                """
            ).fetchone()
            order = connection.execute(
                "SELECT payment_status, order_status FROM orders WHERE order_id = ?",
                (self.order_id,),
            ).fetchone()
            counts = dict(
                connection.execute(
                    """
                    SELECT i.status, COUNT(*) FROM inventory_items i
                    JOIN products p ON p.id = i.product_id
                    WHERE p.code = 'CHATGPT_SHARED'
                    GROUP BY i.status
                    """
                ).fetchall()
            )
        available_count = int(counts.get("available") or 0)
        delivered_count = int(counts.get("delivered") or 0)
        self.assertEqual(item[0], "available")
        self.assertIsNone(item[1])
        self.assertIsNone(item[2])
        self.assertIsNone(item[3])
        self.assertIsNone(item[4])
        self.assertEqual(order, ("refunded", "cancelled"))
        self.assertEqual(available_count, 1)
        self.assertEqual(delivered_count, 0)


if __name__ == "__main__":
    unittest.main()
