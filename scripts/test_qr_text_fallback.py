from __future__ import annotations

import asyncio
import base64
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import telegram_license_bot as bot
from scripts.test_telegram_callback_flow import FakeUpdate, TelegramCallbackFlowTest


class PhotoFailureUpdate(FakeUpdate):
    def __init__(self, data: str, user_id: int = 42) -> None:
        super().__init__(data, user_id=user_id)
        self.callback_query.message.reply_photo = self._fail_photo
        self.effective_message.reply_photo = self._fail_photo

    async def _fail_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        raise RuntimeError("simulated Telegram photo fetch failure")


class QrTextFallbackTest(TelegramCallbackFlowTest):
    def test_extracts_largest_valid_embedded_image_from_vietqr_svg(self) -> None:
        small_jpeg = b"\xff\xd8\xffsmall"
        qr_png = b"\x89PNG\r\n\x1a\n" + (b"qr" * 50)
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<image href="data:image/jpeg;base64,' + base64.b64encode(small_jpeg) + b'"/>'
            b'<image href="data:image/png;base64,' + base64.b64encode(qr_png) + b'"/>'
            b"</svg>"
        )
        self.assertEqual(bot._extract_largest_embedded_vietqr_image(svg), qr_png)

    def test_qr_is_uploaded_as_validated_server_bytes(self) -> None:
        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        asyncio.run(bot._on_menu_impl(FakeUpdate("dtkd_order_ref_skip"), self.context))
        with closing(sqlite3.connect(self.db_path)) as connection:
            order_id = connection.execute(
                "SELECT order_id FROM orders WHERE product_code = 'GEMINI' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]

        qr_update = FakeUpdate(f"pay_acb:{order_id}")
        png = b"\x89PNG\r\n\x1a\n" + b"test-png"
        with patch.object(bot, "_download_vietqr_png", new=AsyncMock(return_value=png)):
            asyncio.run(bot._send_acb_qr(qr_update, self.context, order_id))

        self.assertEqual(len(qr_update.callback_query.photos), 1)
        self.assertFalse(qr_update.callback_query.replies)

    def test_qr_photo_failure_falls_back_without_duplicate_or_release(self) -> None:
        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        asyncio.run(bot._on_menu_impl(FakeUpdate("dtkd_order_ref_skip"), self.context))

        with closing(sqlite3.connect(self.db_path)) as connection:
            order_id = connection.execute(
                "SELECT order_id FROM orders WHERE product_code = 'GEMINI' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
            order_count_before = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            reserved_before = connection.execute(
                "SELECT COUNT(*) FROM inventory_items WHERE reserved_order_id = ?", (order_id,)
            ).fetchone()[0]

        qr_update = PhotoFailureUpdate(f"pay_acb:{order_id}")
        png = b"\x89PNG\r\n\x1a\n" + b"test-png"
        with patch.object(bot, "_download_vietqr_png", new=AsyncMock(return_value=png)):
            asyncio.run(bot._send_acb_qr(qr_update, self.context, order_id))

        self.assertEqual(len(qr_update.callback_query.replies), 1)
        payment_text = qr_update.callback_query.replies[0][0]
        self.assertIn(order_id, payment_text)
        self.assertIn("123456789", payment_text)
        self.assertIn("70.000", payment_text)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], order_count_before)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM inventory_items WHERE reserved_order_id = ?", (order_id,)
                ).fetchone()[0],
                reserved_before,
            )
