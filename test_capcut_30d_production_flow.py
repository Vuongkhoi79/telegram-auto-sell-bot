from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import bank_checker
import sepay_webhook
import telegram_license_bot as bot
from payment_service import PaymentConfig, PaymentService


PROJECT_ROOT = Path(__file__).resolve().parent


class FakeTelegramBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False
        self.edits: list[tuple[str, object | None]] = []
        self.replies: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.message = SimpleNamespace(
            caption=None,
            chat_id=123456,
            message_id=777,
            reply_text=self._reply_text,
            reply_photo=self._reply_photo,
        )

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.edits.append((caption, reply_markup))

    async def _reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))


class FakeUpdate:
    def __init__(self, data: str, user_id: int = 4242) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=user_id, full_name="CapCut Test", username="capcut_test")
        self.effective_message = SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            reply_text=self.callback_query._reply_text,
            reply_photo=self.callback_query._reply_photo,
        )


class CapCut30DProductionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store_db_path = os.environ.get("STORE_DB_PATH")
        self.old_paths = (
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "store.db"
        source_db = PROJECT_ROOT / "database" / "store_CAPCUT_TEST_30D_FINAL.db"
        shutil.copy2(source_db, self.db_path)
        os.environ["STORE_DB_PATH"] = str(self.db_path)
        sepay_webhook.PROCESSED_TRANSACTIONS_PATH = self.root / "processed_transactions.json"
        sepay_webhook.UNMATCHED_TRANSACTIONS_PATH = self.root / "unmatched_transactions.json"
        self.payment_service = PaymentService(
            PaymentConfig(bank_name="ACB", bank_account="123456789", bank_account_name="AI STORE", qr_url="")
        )
        self.telegram_bot = FakeTelegramBot()
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"store_db_path": self.db_path, "payment_service": self.payment_service}
            ),
            bot=self.telegram_bot,
            user_data={},
        )

    def tearDown(self) -> None:
        (
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        ) = self.old_paths
        if self.previous_store_db_path is None:
            os.environ.pop("STORE_DB_PATH", None)
        else:
            os.environ["STORE_DB_PATH"] = self.previous_store_db_path
        self.temp_dir.cleanup()

    def _capcut_counts(self) -> dict[str, dict[str, int]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT p.code, i.status, COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) LIKE 'CAPCUT%'
                GROUP BY p.code, i.status
                """
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for code, status, count in rows:
            counts.setdefault(str(code), {})[str(status)] = int(count)
        return counts

    def test_menu_and_submenu_show_single_grouped_capcut_entry(self) -> None:
        product_buttons = [button for row in bot._product_menu_keyboard().inline_keyboard for button in row]
        capcut_buttons = [button for button in product_buttons if "CAPCUT" in button.text.upper()]
        self.assertEqual(len(capcut_buttons), 1)
        self.assertEqual(capcut_buttons[0].callback_data, "product:CAPCUT")

        package_buttons = [button for row in bot._package_keyboard("CAPCUT").inline_keyboard for button in row]
        by_callback = {button.callback_data: button.text for button in package_buttons}
        self.assertIn("pkg:CAPCUT:CAPCUT_30D", by_callback)
        self.assertIn("[10]", by_callback["pkg:CAPCUT:CAPCUT_30D"])
        self.assertIn("pkg:CAPCUT:CAPCUT_60D", by_callback)
        self.assertIn("Hết", by_callback["pkg:CAPCUT:CAPCUT_60D"])
        self.assertIn("pkg:CAPCUT:CAPCUT_365D", by_callback)
        self.assertIn("[3]", by_callback["pkg:CAPCUT:CAPCUT_365D"])

    def test_capcut_60d_callback_is_blocked_without_creating_order(self) -> None:
        update = FakeUpdate("pkg:CAPCUT:CAPCUT_60D")
        with closing(sqlite3.connect(self.db_path)) as connection:
            before_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

        asyncio.run(bot._on_menu_impl(update, self.context))

        self.assertEqual(update.callback_query.edits[-1][0], "Gói này hiện đã hết hàng. Vui lòng chọn gói khác.")
        with closing(sqlite3.connect(self.db_path)) as connection:
            after_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            capcut_60d_count = connection.execute("SELECT COUNT(*) FROM orders WHERE product_code = 'CAPCUT_60D'").fetchone()[0]
        self.assertEqual(after_count, before_count)
        self.assertEqual(capcut_60d_count, 0)

    def test_capcut_30d_order_payment_match_and_delivery_on_db_copy(self) -> None:
        before = self._capcut_counts()
        self.assertEqual(before["CAPCUT_30D"]["available"], 10)

        update = FakeUpdate("qty:CAPCUT:CAPCUT_30D:1")
        asyncio.run(bot._on_menu_impl(update, self.context))

        with closing(sqlite3.connect(self.db_path)) as connection:
            order = connection.execute(
                """
                SELECT order_id, product_code, quantity, unit_price_vnd, total_vnd,
                       payment_method, payment_status, order_status, delivered_at, delivery_ref
                FROM orders
                WHERE product_code = 'CAPCUT_30D'
                """
            ).fetchone()
        self.assertIsNotNone(order)
        order_id = str(order[0])
        self.assertEqual(order[1:5], ("CAPCUT_30D", 1, 45000, 45000))
        self.assertEqual(order[5:8], ("", "pending", "pending"))
        after_reserve = self._capcut_counts()
        self.assertEqual(after_reserve["CAPCUT_30D"]["available"], 9)
        self.assertEqual(after_reserve["CAPCUT_30D"]["reserved"], 1)

        qr_update = FakeUpdate(f"pay_acb:{order_id}")
        asyncio.run(bot._send_acb_qr(qr_update, self.context, order_id))
        qr_caption = qr_update.callback_query.photos[-1][0]
        self.assertIn(f"Nội dung: {order_id}", qr_caption)
        with closing(sqlite3.connect(self.db_path)) as connection:
            payment_method = connection.execute(
                "SELECT payment_method FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(payment_method, "ACB")

        async def fulfill(order_id_to_fulfill: str) -> dict[str, object]:
            return await bot.fulfill_order(self.context, order_id_to_fulfill)

        payload = {"transaction_id": "SEPAY-CAPCUT-30D-001", "transferAmount": 45000, "addInfo": f"PAY {order_id}"}
        result = asyncio.run(sepay_webhook.process_sepay_payload(payload, fulfill))

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], order_id)
        self.assertEqual(result["fulfillment"]["type"], "sales_order")
        with closing(sqlite3.connect(self.db_path)) as connection:
            final_order = connection.execute(
                """
                SELECT product_code, unit_price_vnd, total_vnd, payment_method,
                       payment_status, order_status, delivered_at, delivery_ref
                FROM orders
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
            delivered_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE p.code = 'CAPCUT_30D' AND i.status = 'delivered' AND i.delivered_order_id = ?
                """,
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(final_order[0:6], ("CAPCUT_30D", 45000, 45000, "SEPAY", "paid", "paid"))
        self.assertTrue(final_order[6])
        self.assertTrue(final_order[7])
        self.assertEqual(delivered_count, 1)
        after_delivery = self._capcut_counts()
        self.assertEqual(after_delivery["CAPCUT_30D"]["available"], 9)
        self.assertEqual(after_delivery["CAPCUT_30D"]["delivered"], 1)
        self.assertIn(str(final_order[7]), self.telegram_bot.messages[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
