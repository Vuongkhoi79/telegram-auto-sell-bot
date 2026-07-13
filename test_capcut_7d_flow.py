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

from openpyxl import Workbook

import sepay_webhook
import telegram_license_bot as bot
from payment_service import PaymentConfig, PaymentService
from repository.store_repository import StoreRepository
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory


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
        self.edits.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))


class FakeUpdate:
    def __init__(self, data: str, user_id: int = 7007) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=user_id, full_name="CapCut 7D Test", username="capcut_7d")
        self.effective_message = SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            reply_text=self.callback_query._reply_text,
            reply_photo=self.callback_query._reply_photo,
        )


class CapCut7DFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store_db_path = os.environ.get("STORE_DB_PATH")
        self.old_paths = (
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "store.db"
        shutil.copy2(PROJECT_ROOT / "database" / "store_CAPCUT_TEST_30D_FINAL.db", self.db_path)
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
        self.import_path = self.root / "capcut_7d.xlsx"
        self._write_capcut_7d_workbook(self.import_path)
        self.import_report = import_inventory(self.import_path, self.db_path, mode="append")

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

    def _write_capcut_7d_workbook(self, path: Path) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "CAPCUT_7D"
        sheet.append(list(REQUIRED_COLUMNS))
        sheet.append([
            "CAPCUT",
            "AI",
            "CAPCUT",
            "personal",
            "7D",
            8000,
            7,
            "capcut7d-one@example.com|pass-one",
            "",
            1,
        ])
        sheet.append([
            "CAPCUT_7D",
            "AI",
            "CAPCUT",
            "personal",
            "7D",
            8000,
            7,
            "capcut7d-two@example.com|pass-two",
            "",
            1,
        ])
        workbook.save(path)

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

    def test_import_adds_capcut_7d_without_touching_other_capcut_skus(self) -> None:
        self.assertEqual(self.import_report["credentials_added"], 2)
        self.assertEqual(self.import_report["row_errors"], 0)
        self.assertEqual(self.import_report["stock"]["CAPCUT_7D"], 2)
        counts = self._capcut_counts()
        self.assertEqual(counts["CAPCUT_7D"]["available"], 2)
        self.assertEqual(counts["CAPCUT_30D"]["available"], 10)
        self.assertEqual(counts["CAPCUT"]["available"], 3)

        repository = StoreRepository(self.db_path)
        repository.set_product_active("CAPCUT_7D", False)
        self.assertFalse(repository.get_product_details("CAPCUT_7D")["active"])
        repository.set_product_active("CAPCUT_7D", True)
        self.assertTrue(repository.get_product_details("CAPCUT_7D")["active"])
        self.assertEqual(repository.get_stock_count("CAPCUT_7D"), 2)

    def test_menu_order_price_payment_and_delivery_use_capcut_7d_sku(self) -> None:
        package_buttons = [button for row in bot._package_keyboard("CAPCUT").inline_keyboard for button in row]
        callbacks = [button.callback_data for button in package_buttons]
        self.assertLess(callbacks.index("pkg:CAPCUT:CAPCUT_7D"), callbacks.index("pkg:CAPCUT:CAPCUT_30D"))
        by_callback = {button.callback_data: button.text for button in package_buttons}
        self.assertIn("8.000", by_callback["pkg:CAPCUT:CAPCUT_7D"])

        before = self._capcut_counts()
        self.assertEqual(before["CAPCUT_7D"]["available"], 2)
        self.assertEqual(before["CAPCUT_30D"]["available"], 10)

        update = FakeUpdate("qty:CAPCUT:CAPCUT_7D:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        self.assertIn("Affiliate", update.callback_query.edits[-1][0])
        after_prompt = self._capcut_counts()
        self.assertEqual(after_prompt["CAPCUT_7D"]["available"], 2)
        self.assertNotIn("reserved", after_prompt["CAPCUT_7D"])

        skip_update = FakeUpdate("dtkd_order_ref_skip")
        asyncio.run(bot._on_menu_impl(skip_update, self.context))
        payment_text = skip_update.callback_query.edits[-1][0]
        self.assertIn("8.000", payment_text)

        with closing(sqlite3.connect(self.db_path)) as connection:
            order = connection.execute(
                """
                SELECT order_id, product_code, quantity, unit_price_vnd, total_vnd, payment_status, order_status
                FROM orders
                WHERE product_code = 'CAPCUT_7D'
                """
            ).fetchone()
        self.assertIsNotNone(order)
        order_id = str(order[0])
        self.assertEqual(order[1:5], ("CAPCUT_7D", 1, 8000, 8000))
        self.assertEqual(order[5:7], ("pending", "pending"))
        after_reserve = self._capcut_counts()
        self.assertEqual(after_reserve["CAPCUT_7D"]["available"], 1)
        self.assertEqual(after_reserve["CAPCUT_7D"]["reserved"], 1)
        self.assertEqual(after_reserve["CAPCUT_30D"]["available"], 10)

        qr_update = FakeUpdate(f"pay_acb:{order_id}")
        asyncio.run(bot._send_acb_qr(qr_update, self.context, order_id))
        self.assertIn(order_id, qr_update.callback_query.photos[-1][0])

        async def fulfill(order_id_to_fulfill: str) -> dict[str, object]:
            return await bot.fulfill_order(self.context, order_id_to_fulfill)

        payload = {"transaction_id": "SEPAY-CAPCUT-7D-001", "transferAmount": 8000, "addInfo": f"PAY {order_id}"}
        result = asyncio.run(sepay_webhook.process_sepay_payload(payload, fulfill))

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], order_id)
        with closing(sqlite3.connect(self.db_path)) as connection:
            final_order = connection.execute(
                """
                SELECT product_code, unit_price_vnd, total_vnd, payment_method,
                       payment_status, order_status, delivery_ref
                FROM orders
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
            delivered_secret = connection.execute(
                """
                SELECT i.secret_value
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE p.code = 'CAPCUT_7D' AND i.status = 'delivered' AND i.delivered_order_id = ?
                """,
                (order_id,),
            ).fetchone()
        self.assertEqual(final_order[0:6], ("CAPCUT_7D", 8000, 8000, "SEPAY", "paid", "delivered"))
        self.assertIsNotNone(delivered_secret)
        self.assertIn("capcut7d-", str(delivered_secret[0]))
        self.assertIn(str(final_order[6]), self.telegram_bot.messages[0]["text"])
        after_delivery = self._capcut_counts()
        self.assertEqual(after_delivery["CAPCUT_7D"]["available"], 1)
        self.assertEqual(after_delivery["CAPCUT_7D"]["delivered"], 1)
        self.assertEqual(after_delivery["CAPCUT_30D"]["available"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
