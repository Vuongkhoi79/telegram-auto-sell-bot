from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from payment_service import PaymentConfig, PaymentService
from repository.store_repository import StoreRepository
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory
import sepay_webhook
import telegram_license_bot as bot


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[tuple[str, object | None]] = []
        self.captions: list[tuple[str, object | None]] = []
        self.replies: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.documents: list[tuple[str, object | None]] = []
        self.answered = False
        self.message = SimpleNamespace(
            caption=None,
            chat_id=123456,
            message_id=777,
            reply_text=self._reply_text,
            reply_photo=self._reply_photo,
            reply_document=self._reply_document,
        )

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.captions.append((caption, reply_markup))

    async def _reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))

    async def _reply_document(self, document=None, filename=None) -> None:
        self.documents.append((filename or "", None))


class FakeUpdate:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=user_id, full_name="Test User", username="")
        self.replies: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.documents: list[tuple[str, object | None]] = []
        self.effective_message = SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            reply_text=self._reply_text,
            reply_photo=self._reply_photo,
            reply_document=self._reply_document,
            replies=self.callback_query.replies,
            photos=self.callback_query.photos,
            documents=self.callback_query.documents,
        )

    async def _reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))

    async def _reply_document(self, document=None, filename=None) -> None:
        self.documents.append((filename or "", None))


class FakeLicenseService:
    def touch_user(self, *args, **kwargs) -> None:
        return None


class TelegramCallbackFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "store.db"
        source_db = PROJECT_ROOT / "database" / "store.db"
        with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(self.db_path)) as target:
            source.backup(target)
        workbook_path = Path(self.temp_dir.name) / "inventory.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "GEMINI"
        sheet.append(REQUIRED_COLUMNS)
        for idx in range(8):
            sheet.append(["GEMINI", "account", "Gemini AI Pro", "account", "30D", 70000, 7, f"gem{idx}@example.com|pass", "", 1])
        workbook.save(workbook_path)
        workbook.close()
        import_inventory(workbook_path, self.db_path, mode="replace")
        self.previous_store_db_path = os.environ.get("STORE_DB_PATH")
        self.previous_business_partners_path = bot.BUSINESS_PARTNERS_PATH
        self.previous_sepay_processed_path = sepay_webhook.PROCESSED_TRANSACTIONS_PATH
        self.previous_sepay_unmatched_path = sepay_webhook.UNMATCHED_TRANSACTIONS_PATH
        os.environ["STORE_DB_PATH"] = str(self.db_path)
        bot.BUSINESS_PARTNERS_PATH = Path(self.temp_dir.name) / "business_partners.json"
        sepay_webhook.PROCESSED_TRANSACTIONS_PATH = Path(self.temp_dir.name) / "processed_transactions.json"
        sepay_webhook.UNMATCHED_TRANSACTIONS_PATH = Path(self.temp_dir.name) / "unmatched_transactions.json"
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "store_db_path": self.db_path,
                    "bot_username": "AIDailyBot",
                    "license_service": FakeLicenseService(),
                    "payment_service": PaymentService(
                        PaymentConfig(bank_name="ACB", bank_account="123456789", bank_account_name="AI STORE", qr_url="")
                    ),
                }
            ),
            args=[],
            user_data={},
        )

    def tearDown(self) -> None:
        if self.previous_store_db_path is None:
            os.environ.pop("STORE_DB_PATH", None)
        else:
            os.environ["STORE_DB_PATH"] = self.previous_store_db_path
        bot.BUSINESS_PARTNERS_PATH = self.previous_business_partners_path
        sepay_webhook.PROCESSED_TRANSACTIONS_PATH = self.previous_sepay_processed_path
        sepay_webhook.UNMATCHED_TRANSACTIONS_PATH = self.previous_sepay_unmatched_path
        self.temp_dir.cleanup()

    def test_menu_navigation_callbacks_are_stateless(self) -> None:
        menu_buttons = [button for row in bot._main_menu_keyboard().inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == "menu_history" for button in menu_buttons))

        product_update = FakeUpdate("menu_products")
        asyncio.run(bot._on_menu_impl(product_update, self.context))
        product_text = product_update.callback_query.edits[-1][0]
        self.assertIn("Sản phẩm", product_text)
        self.assertIn("Chọn sản phẩm", product_text)

        main_update = FakeUpdate("menu_main")
        asyncio.run(bot._on_menu_impl(main_update, self.context))
        main_text = main_update.callback_query.edits[-1][0]
        self.assertIn("AI STORE", main_text)

    def test_global_navigation_buttons_work_from_caption_messages(self) -> None:
        product_update = FakeUpdate("menu_products")
        product_update.callback_query.message.caption = "Giao dịch đã hủy."
        asyncio.run(bot._on_menu_impl(product_update, self.context))
        product_reply = product_update.callback_query.replies[-1][0]
        self.assertTrue(product_reply.startswith("🎁"))
        self.assertIn("Ch", product_reply)
        self.assertEqual(product_update.callback_query.edits, [])

        main_update = FakeUpdate("menu_main")
        main_update.callback_query.message.caption = "Giao dịch đã hủy."
        asyncio.run(bot._on_menu_impl(main_update, self.context))
        main_reply = main_update.callback_query.replies[-1][0]
        self.assertIn("AI STORE", main_reply)
        self.assertEqual(main_update.callback_query.edits, [])

    def test_malformed_callbacks_return_safe_menu_message(self) -> None:
        invalid_callbacks = [
            "pkg:GEMINI",
            "qty:GEMINI:GEMINI:not-a-number",
            "pay_acb:",
            "product:",
            "license_product_machine:TOOL_YEAR_365",
            "unknown_callback",
        ]
        for callback_data in invalid_callbacks:
            with self.subTest(callback_data=callback_data):
                update = FakeUpdate(callback_data)
                asyncio.run(bot._on_menu_impl(update, self.context))
                self.assertTrue(update.callback_query.answered)
                self.assertEqual(
                    update.callback_query.edits[-1][0],
                    "Menu không hợp lệ. Vui lòng quay lại menu chính.",
                )

    def test_purchase_history_button_shows_empty_and_user_filtered_orders(self) -> None:
        empty_update = FakeUpdate("menu_history")
        asyncio.run(bot._on_menu_impl(empty_update, self.context))
        empty_text = empty_update.callback_query.edits[-1][0]
        self.assertIn("Lịch sử mua hàng", empty_text)
        self.assertIn("Bạn chưa có đơn hàng nào.", empty_text)

        missing_history_context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "store_db_path": Path(self.temp_dir.name) / "missing-store.db",
                }
            )
        )
        missing_update = FakeUpdate("menu_history")
        asyncio.run(bot._send_purchase_history(missing_update, missing_history_context, edit=True))
        missing_text = missing_update.callback_query.edits[-1][0]
        self.assertIn("Không đọc được lịch sử đơn hàng, vui lòng liên hệ hỗ trợ.", missing_text)
        self.assertNotIn("Bạn chưa có đơn hàng nào.", missing_text)

        now = "2026-06-26T00:00:00+00:00"
        repo = StoreRepository(self.db_path)
        repo.upsert_order(
            {
                "order_id": "ORD-HIST-001",
                "telegram_user_id": 42,
                "username": "Test User",
                "product_code": "GEMINI",
                "product_name": "GEMINI AI",
                "package_name": "Gemini AI Pro",
                "quantity": 1,
                "unit_price_vnd": 70000,
                "total_vnd": 70000,
                "delivery_type": "account",
                "payment_status": "paid",
                "order_status": "delivered",
                "created_at": now,
                "paid_at": now,
                "delivered_at": now,
            }
        )
        repo.upsert_order(
            {
                "order_id": "ORD-HIST-NO-USER",
                "telegram_user_id": 42,
                "username": "",
                "product_code": "GEMINI",
                "product_name": "GEMINI AI",
                "package_name": "Gemini AI Pro",
                "quantity": 1,
                "unit_price_vnd": 70000,
                "total_vnd": 70000,
                "delivery_type": "account",
                "payment_status": "paid",
                "order_status": "delivered",
                "created_at": now,
                "paid_at": now,
                "delivered_at": now,
            }
        )
        repo.upsert_order(
            {
                "order_id": "ORD-HIST-OTHER",
                "telegram_user_id": 7,
                "username": "Other User",
                "product_code": "CAPCUT",
                "product_name": "CAPCUT PRO",
                "package_name": "CAPCUT PRO",
                "quantity": 2,
                "unit_price_vnd": 400000,
                "total_vnd": 800000,
                "delivery_type": "account",
                "payment_status": "paid",
                "order_status": "delivered",
                "created_at": now,
                "paid_at": now,
                "delivered_at": now,
            }
        )
        before_stock = self._gemini_stock_counts()
        history_update = FakeUpdate("menu_history")
        asyncio.run(bot._on_menu_impl(history_update, self.context))
        history_text = history_update.callback_query.edits[-1][0]
        self.assertIn("ORD-HIST-001", history_text)
        self.assertIn("Người mua: Test User | ID: 42", history_text)
        self.assertIn("Người mua: Chưa lưu thông tin | ID: 42", history_text)
        self.assertIn("GEMINI AI", history_text)
        self.assertNotIn("ORD-HIST-OTHER", history_text)
        self.assertEqual(before_stock, self._gemini_stock_counts())

    def _gemini_stock_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            stock = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'available'
                """
            ).fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'reserved'
                """
            ).fetchone()[0]
        return int(stock), int(reserved)

    def test_quantity_flow_uses_canonical_callback_data_and_reserves_one(self) -> None:
        menu_buttons = [button for row in bot._product_menu_keyboard().inline_keyboard for button in row]
        gemini_button = next(button for button in menu_buttons if "GEMINI AI" in button.text)
        self.assertEqual(gemini_button.callback_data, "product:GEMINI")

        update = FakeUpdate("product:GEMINI")
        asyncio.run(bot._on_menu_impl(update, self.context))
        package_markup = update.callback_query.edits[-1][1]
        package_buttons = [button for row in package_markup.inline_keyboard for button in row]
        gemini_package_button = next(button for button in package_buttons if "Gemini AI Pro" in button.text)
        self.assertEqual(gemini_package_button.callback_data, "pkg:GEMINI:GEMINI")

        update = FakeUpdate("pkg:GEMINI:GEMINI")
        asyncio.run(bot._on_menu_impl(update, self.context))
        quantity_markup = update.callback_query.edits[-1][1]
        quantity_buttons = [button for row in quantity_markup.inline_keyboard for button in row]
        one_button = next(button for button in quantity_buttons if button.text == "1")
        self.assertEqual(one_button.callback_data, "qty:GEMINI:GEMINI:1")

        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        ref_prompt = update.callback_query.edits[-1][0]
        self.assertIn("Affiliate", ref_prompt)
        self.assertEqual(self._gemini_stock_counts(), (8, 0))

        update = FakeUpdate("dtkd_order_ref_skip")
        asyncio.run(bot._on_menu_impl(update, self.context))
        final_text = update.callback_query.edits[-1][0]
        self.assertIn("thanh toán", final_text.lower())
        self.assertNotIn("hết hàng", final_text.lower())
        with closing(sqlite3.connect(self.db_path)) as connection:
            stock = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'available'
                """
            ).fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'reserved'
                """
            ).fetchone()[0]
        self.assertEqual((stock, reserved), (7, 1))

    def test_quantity_flow_rejects_when_quantity_exceeds_stock(self) -> None:
        update = FakeUpdate("qty:GEMINI:GEMINI:9")
        asyncio.run(bot._on_menu_impl(update, self.context))
        final_text = update.callback_query.edits[-1][0]
        self.assertIn("hết hàng", final_text.lower())
        with closing(sqlite3.connect(self.db_path)) as connection:
            stock = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'available'
                """
            ).fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'reserved'
                """
            ).fetchone()[0]
        self.assertEqual((stock, reserved), (8, 0))

    def test_quantity_flow_reaches_qr_payment_branch(self) -> None:
        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        ref_prompt = update.callback_query.edits[-1][0]
        self.assertIn("Affiliate", ref_prompt)

        update = FakeUpdate("dtkd_order_ref_skip")
        asyncio.run(bot._on_menu_impl(update, self.context))
        final_text = update.callback_query.edits[-1][0]
        self.assertIn("thanh toán", final_text.lower())
        self.assertNotIn("hết hàng", final_text.lower())

        with closing(sqlite3.connect(self.db_path)) as connection:
            order_id = connection.execute(
                "SELECT order_id FROM orders WHERE product_code = 'GEMINI' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
            total_vnd = connection.execute(
                "SELECT total_vnd FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(total_vnd, 70000)

        qr_update = FakeUpdate(f"pay_acb:{order_id}")
        asyncio.run(bot._send_acb_qr(qr_update, self.context, order_id))
        self.assertTrue(qr_update.effective_message.photos)
        self.assertFalse(any("lỗi tạo thanh toán" in text.lower() for text, _ in qr_update.replies))

    def test_dtkd_referral_code_can_be_entered_before_order_creation(self) -> None:
        bot.BUSINESS_PARTNERS_PATH.write_text(
            """
{
  "partners": [
    {
      "telegram_user_id": 111,
      "partner_id": "PTR0000000111",
      "partner_code": "DTKD000111",
      "referral_code": "AIDAILY111",
      "short_code": "AIDAILY111",
      "status": "approved",
      "approved_at": "2026-07-01T00:00:00+00:00",
      "commission_policy_id": "DTKD_TRIAL_3M_REVENUE_TIERS"
    }
  ],
  "referrals": [],
  "order_refs": [],
  "commissions": [],
  "withdrawals": []
}
""".strip(),
            encoding="utf-8",
        )

        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        self.assertEqual(self._gemini_stock_counts(), (8, 0))

        invalid_update = FakeUpdate("unused")
        invalid_update.effective_message.text = "SAI-MA"
        asyncio.run(bot.on_text_machine_id(invalid_update, self.context))
        self.assertTrue(invalid_update.replies)
        self.assertEqual(self._gemini_stock_counts(), (8, 0))

        valid_update = FakeUpdate("unused")
        valid_update.effective_message.text = "AIDAILY111"
        asyncio.run(bot.on_text_machine_id(valid_update, self.context))
        final_text = valid_update.replies[-1][0]
        self.assertIn("Gemini AI Pro", final_text)

        with closing(sqlite3.connect(self.db_path)) as connection:
            order_id = connection.execute(
                "SELECT order_id FROM orders WHERE product_code = 'GEMINI' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        data = bot._load_business_partners()
        order_ref = next(item for item in data["order_refs"] if item["order_id"] == order_id)
        self.assertEqual(order_ref["partner_code"], "DTKD000111")
        self.assertEqual(order_ref["gross_amount"], 70000)
        self.assertEqual(order_ref["total_amount"], 70000)
        self.assertEqual(order_ref["commission_amount"], 7000)
        self.assertEqual(order_ref["payable_amount"], 70000)
        self.assertEqual(order_ref["net_revenue"], 70000)
        self.assertEqual(order_ref["commission_status"], "pending_reconcile")

        order = StoreRepository(self.db_path).find_order(order_id)
        assert order is not None
        self.assertEqual(order["total"], 70000)
        qr_url = bot._build_vietqr_url(
            bot._dtkd_enrich_order_accounting(dict(order)) or dict(order),
            self.context.application.bot_data["payment_service"],
        )
        self.assertIn("amount=70000", qr_url)

        async def fulfill_stub(matched_order_id: str):
            return {"ok": True, "order_id": matched_order_id}

        webhook_result = asyncio.run(
            sepay_webhook.process_sepay_payload(
                {"id": "TX-DTKD-70000", "amount": 70000, "content": order_id},
                fulfill_stub,
            )
        )
        self.assertTrue(webhook_result["ok"])
        self.assertEqual(webhook_result["order_id"], order_id)
        with closing(sqlite3.connect(self.db_path)) as connection:
            tx_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM payment_transactions AS tx
                JOIN orders AS o ON o.id = tx.order_id
                WHERE o.order_id = ?
                """,
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(tx_count, 1)

        duplicate_result = asyncio.run(
            sepay_webhook.process_sepay_payload(
                {"id": "TX-DTKD-56000-REPLAY", "amount": 70000, "content": order_id},
                fulfill_stub,
            )
        )
        self.assertFalse(duplicate_result["ok"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            tx_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM payment_transactions AS tx
                JOIN orders AS o ON o.id = tx.order_id
                WHERE o.order_id = ?
                """,
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(tx_count, 1)

        order = StoreRepository(self.db_path).find_order(order_id)
        assert order is not None
        self.assertEqual(order["payment_status"], "paid")
        order["payment_status"] = "paid"
        order["order_status"] = "delivered"
        order["paid_at"] = "2000-01-01T00:00:00+00:00"
        order["delivered_at"] = "2000-01-01T00:00:00+00:00"
        bot._record_partner_commission(order)
        data = bot._load_business_partners()
        commission = next(item for item in data["commissions"] if item["order_id"] == order_id)
        self.assertEqual(commission["partner_code"], "DTKD000111")
        self.assertEqual(commission["status"], "withdrawable")
        self.assertEqual(commission["commission_amount"], 7000)
        self.assertEqual(commission["net_revenue"], 70000)
        self.assertEqual(commission["amount"], 7000)
        bot._record_partner_commission(order)
        data = bot._load_business_partners()
        self.assertEqual(len([item for item in data["commissions"] if item["order_id"] == order_id]), 1)

    def test_affiliate_deep_link_saves_referral_and_auto_attaches_order(self) -> None:
        bot.BUSINESS_PARTNERS_PATH.write_text(
            """
{
  "partners": [
    {
      "telegram_user_id": 111,
      "partner_id": "PTR0000000111",
      "partner_code": "DTKD000111",
      "referral_code": "AIDAILY111",
      "short_code": "AIDAILY111",
      "full_name": "Affiliate One",
      "status": "approved",
      "approved_at": "2026-07-01T00:00:00+00:00",
      "commission_policy_id": "DTKD_AFFILIATE_10PCT"
    }
  ],
  "referrals": [],
  "order_refs": [],
  "commissions": [],
  "withdrawals": []
}
""".strip(),
            encoding="utf-8",
        )
        partner = bot._load_business_partners()["partners"][0]
        self.assertEqual(
            bot._partner_referral_link(self.context, str(partner["referral_code"])),
            "https://t.me/AIDailyBot?start=AIDAILY111",
        )

        self.context.args = ["AIDAILY111"]
        start_update = FakeUpdate("unused", user_id=222)
        asyncio.run(bot.cmd_start(start_update, self.context))
        self.assertIn("Bạn được giới thiệu bởi Affiliate: Affiliate One", start_update.replies[-1][0])
        data = bot._load_business_partners()
        referral = next(item for item in data["referrals"] if item["customer_telegram_user_id"] == 222)
        self.assertEqual(referral["partner_code"], "DTKD000111")

        qty_update = FakeUpdate("qty:GEMINI:GEMINI:1", user_id=222)
        asyncio.run(bot._on_menu_impl(qty_update, self.context))
        bill_text = qty_update.callback_query.edits[-1][0]
        self.assertNotIn("Affiliate/referral code", bill_text)
        self.assertIn("70.000", bill_text)

        order_id = self.context.user_data["pending_order_id"]
        order = bot._dtkd_enrich_order_accounting(StoreRepository(self.db_path).find_order(order_id))
        assert order is not None
        self.assertEqual(order["total"], 70000)
        self.assertEqual(order["partner_code"], "DTKD000111")
        self.assertEqual(order["commission_amount"], 7000)
        self.assertIn(
            "amount=70000",
            bot._build_vietqr_url(order, self.context.application.bot_data["payment_service"]),
        )

        order["payment_status"] = "paid"
        order["order_status"] = "delivered"
        order["paid_at"] = "2000-01-01T00:00:00+00:00"
        order["delivered_at"] = "2000-01-01T00:00:00+00:00"
        bot._record_partner_commission(order)
        data = bot._load_business_partners()
        commission = next(item for item in data["commissions"] if item["order_id"] == order_id)
        self.assertEqual(commission["commission_amount"], 7000)
        self.assertEqual(commission["amount"], 7000)
        self.assertEqual(commission["status"], "withdrawable")

    def test_approved_dtkd_self_referral_does_not_discount_or_create_commission(self) -> None:
        bot.BUSINESS_PARTNERS_PATH.write_text(
            """
{
  "partners": [
    {
      "telegram_user_id": 42,
      "partner_id": "PTR0000000042",
      "partner_code": "DTKD000042",
      "referral_code": "AIDAILY42",
      "short_code": "AIDAILY42",
      "status": "approved",
      "approved_at": "2026-07-01T00:00:00+00:00",
      "commission_policy_id": "DTKD_AFFILIATE_10PCT"
    }
  ],
  "referrals": [],
  "order_refs": [],
  "commissions": [],
  "withdrawals": []
}
""".strip(),
            encoding="utf-8",
        )

        self.context.args = ["AIDAILY42"]
        start_update = FakeUpdate("unused", user_id=42)
        asyncio.run(bot.cmd_start(start_update, self.context))
        self.assertIn("Không tính hoa hồng cho đơn tự giới thiệu.", start_update.replies[-1][0])
        self.assertEqual(bot._load_business_partners()["referrals"], [])
        self.context.args = []

        qty_update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(qty_update, self.context))
        prompt_text = qty_update.callback_query.edits[-1][0]
        self.assertIn("Affiliate", prompt_text)
        self.assertNotIn("pending_order_id", self.context.user_data)

        code_update = FakeUpdate("unused")
        code_update.effective_message.text = "AIDAILY42"
        asyncio.run(bot.on_text_machine_id(code_update, self.context))
        bill_text = code_update.replies[-1][0]
        self.assertNotIn("Chi", bill_text)
        self.assertIn("70.000", bill_text)

        order_id = self.context.user_data["pending_order_id"]
        order = bot._dtkd_enrich_order_accounting(StoreRepository(self.db_path).find_order(order_id))
        assert order is not None
        self.assertEqual(order["total"], 70000)
        self.assertEqual(order["gross_amount"], 70000)
        self.assertEqual(order["commission_amount"], 7000)
        self.assertEqual(order["payable_amount"], 70000)
        self.assertIn(
            "amount=70000",
            bot._build_vietqr_url(order, self.context.application.bot_data["payment_service"]),
        )

        async def fulfill_stub(matched_order_id: str):
            return {"ok": True, "order_id": matched_order_id}

        webhook_result = asyncio.run(
            sepay_webhook.process_sepay_payload(
                {"id": "TX-DTKD-SELF-70000", "amount": 70000, "content": order_id},
                fulfill_stub,
            )
        )
        self.assertTrue(webhook_result["ok"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            tx_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM payment_transactions AS tx
                JOIN orders AS o ON o.id = tx.order_id
                WHERE o.order_id = ?
                """,
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(tx_count, 1)

        order["payment_status"] = "paid"
        order["order_status"] = "delivered"
        order["paid_at"] = "2000-01-01T00:00:00+00:00"
        order["delivered_at"] = "2000-01-01T00:00:00+00:00"
        bot._record_partner_commission(order)
        data = bot._load_business_partners()
        order_ref = next(item for item in data["order_refs"] if item["order_id"] == order_id)
        self.assertEqual(order_ref["partner_code"], "DTKD000042")
        self.assertTrue(order_ref["self_referral"])
        self.assertEqual(order_ref["commission_status"], "rejected_self_referral")
        self.assertFalse([item for item in data["commissions"] if item["order_id"] == order_id])

    def test_locked_dtkd_code_is_not_auto_applied(self) -> None:
        bot.BUSINESS_PARTNERS_PATH.write_text(
            """
{
  "partners": [
    {
      "telegram_user_id": 42,
      "partner_id": "PTR0000000042",
      "partner_code": "DTKD000042",
      "referral_code": "AIDAILY42",
      "short_code": "AIDAILY42",
      "status": "locked",
      "approved_at": "2026-07-01T00:00:00+00:00",
      "commission_policy_id": "DTKD_AFFILIATE_10PCT"
    }
  ],
  "referrals": [],
  "order_refs": [],
  "commissions": [],
  "withdrawals": []
}
""".strip(),
            encoding="utf-8",
        )
        qty_update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(qty_update, self.context))
        prompt_text = qty_update.callback_query.edits[-1][0]
        self.assertIn("Affiliate", prompt_text)
        self.assertNotIn("pending_order_id", self.context.user_data)
        self.assertEqual(self._gemini_stock_counts(), (8, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
