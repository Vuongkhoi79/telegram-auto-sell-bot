from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from license_service import LicenseService
from telegram_license_bot import _send_free_help


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[dict[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.texts.append({"text": text, "reply_markup": reply_markup})


class LicenseHelpUiTest(unittest.TestCase):
    def test_free_help_shows_machine_id_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            license_service = LicenseService(
                private_key_path=root / "unused.pem",
                db_path=root / "licenses_db.json",
                output_dir=root / "issued_licenses",
            )
            machine_id = "F461FE60-342ADFEF-C1AE4E7B-A5FC9744"
            license_service.db.upsert_user(
                {
                    "telegram_user_id": 123456,
                    "username": "Test User",
                    "machine_id": machine_id,
                    "source": "help_state",
                    "reminder_state": "active",
                    "last_seen_at": "2026-06-02T00:00:00+00:00",
                    "last_command_at": "2026-06-02T00:00:00+00:00",
                    "last_license_type": "",
                    "last_license_expire_date": "",
                    "last_license_path": "",
                    "next_reminder_at": "",
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:01+00:00",
                }
            )
            app = SimpleNamespace(bot_data={"license_service": license_service})
            user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
            update = SimpleNamespace(effective_user=user, effective_message=FakeMessage(), callback_query=None)
            context = SimpleNamespace(application=app)

            asyncio.run(_send_free_help(update, context))

            self.assertEqual(len(update.effective_message.texts), 1)
            payload = update.effective_message.texts[0]
            text = payload["text"]
            keyboard = payload["reply_markup"]
            buttons = keyboard.inline_keyboard

            self.assertIn("Máy của bạn chưa có license.", text)
            self.assertIn("Nhận license tự động qua Telegram bot.", text)
            self.assertIn(machine_id, text)
            self.assertEqual(buttons[0][0].text, "📋 Sao chép Machine ID")
            self.assertEqual(buttons[0][0].callback_data, f"copy_machine_id:{machine_id}")
            self.assertEqual(buttons[1][0].text, "🎁 Nhận License Free 90 Ngày")
            self.assertTrue(buttons[1][0].url.startswith("https://t.me/Aidaily79_bot?start="))
            self.assertEqual(buttons[2][0].text, "📂 Nhập file license")
            self.assertEqual(buttons[2][0].callback_data, f"import_license:{machine_id}")


if __name__ == "__main__":
    unittest.main()
