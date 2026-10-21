from __future__ import annotations

import asyncio
import base64
import io
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import cv2
from PIL import Image

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
    def test_rendered_svg_qr_decodes_and_matches_payment_fields(self) -> None:
        payload = (
            "00020101021238530010A0000007270123000697041601091234567890208QRIBFTTA"
            "53037045405700005802VN62150811ORDTEST00016304ABCD"
        )
        matrix = cv2.QRCodeEncoder_create().encode(payload)
        modules = matrix.shape[0]
        path_parts: list[str] = []
        for y, row in enumerate(matrix):
            x = 0
            while x < modules:
                if row[x] != 0:
                    x += 1
                    continue
                start = x
                while x < modules and row[x] == 0:
                    x += 1
                width = x - start
                path_parts.append(f"M{start} {y}h{width}v1h-{width}z")
        background = Image.new("RGB", (540, 640), "white")
        background_bytes = io.BytesIO()
        background.save(background_bytes, format="PNG")
        background_data = base64.b64encode(background_bytes.getvalue()).decode("ascii")
        svg = (
            f'<svg width="540" height="640" viewBox="0 0 540 640" xmlns="http://www.w3.org/2000/svg">'
            f'<image x="0" y="0" width="540" height="640" href="data:image/png;base64,{background_data}"/>'
            f'<g transform="translate(92 94) scale({357 / modules})">'
            f'<rect width="{modules}" height="{modules}" fill="#fff"/>'
            f'<path fill="#111" d="{"".join(path_parts)}"/></g></svg>'
        ).encode("utf-8")
        png = bot._render_vietqr_svg_png(svg)
        order = {"order_id": "ORD-TEST-0001", "total": 70000}
        decoded = bot._validate_vietqr_png(png, order, self.context.application.bot_data["payment_service"])
        self.assertEqual(decoded, payload)

    def test_blank_or_logo_only_image_is_rejected(self) -> None:
        blank = Image.new("RGB", (540, 640), "white")
        output = io.BytesIO()
        blank.save(output, format="PNG")
        with self.assertRaisesRegex(ValueError, "decodable QR code"):
            bot._validate_vietqr_png(
                output.getvalue(),
                {"order_id": "ORD-TEST-0001", "total": 70000},
                self.context.application.bot_data["payment_service"],
            )

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
        with (
            patch.object(bot, "_download_vietqr_png", new=AsyncMock(return_value=png)),
            patch.object(bot, "_validate_vietqr_png"),
        ):
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
        with (
            patch.object(bot, "_download_vietqr_png", new=AsyncMock(return_value=png)),
            patch.object(bot, "_validate_vietqr_png"),
        ):
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
