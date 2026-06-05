from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from license_service import LicenseService
from payment_service import PaymentConfig, PaymentService
from license_service import LIFETIME_PLAN, YEAR_365_PLAN
from telegram_license_bot import _handle_free_license_click, _on_menu_impl, _upgrade_permanent_keyboard_for_machine


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[dict[str, object]] = []
        self.photos: list[dict[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.texts.append({"text": text, "reply_markup": reply_markup})

    async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append({"photo": photo, "caption": caption, "reply_markup": reply_markup})

    async def reply_document(self, document=None, **kwargs):
        self.texts.append({"text": "DOCUMENT", "reply_markup": None})


class FakeQuery:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.answered = False
        self.data = ""

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})


def first_callback_data(reply_markup) -> str:
    button = reply_markup.inline_keyboard[0][0]
    return button.callback_data


def callback_data_at(reply_markup, row: int, col: int = 0) -> str:
    return reply_markup.inline_keyboard[row][col].callback_data


def make_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class UpgradeFromFreeLicenseTest(unittest.TestCase):
    def test_free_success_button_contains_machine_id_and_upgrades_immediately(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
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
                    "source": "free_ready",
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

            payment_service = PaymentService(
                PaymentConfig(
                    bank_name="ACB",
                    bank_account="123456789",
                    bank_account_name="TEST ACCOUNT",
                    qr_url="https://example.com/qr.png",
                )
            )
            app = SimpleNamespace(bot_data={"license_service": license_service, "payment_service": payment_service, "admin_ids": set()})
            user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
            message = FakeMessage()
            update = SimpleNamespace(effective_user=user, effective_message=message, callback_query=None)
            context = SimpleNamespace(application=app)

            asyncio.run(_handle_free_license_click(update, context))

            self.assertGreaterEqual(len(message.texts), 1)
            button_markup = message.texts[0]["reply_markup"]
            callback_data = callback_data_at(button_markup, 1)
            self.assertEqual(callback_data, f"upgrade_machine:{machine_id}")
            self.assertEqual(len(license_service.db.data["licenses"]), 1)

            upgrade_query = FakeQuery()
            upgrade_query.data = callback_data
            upgrade_update = SimpleNamespace(
                effective_user=user,
                effective_message=FakeMessage(),
                callback_query=upgrade_query,
            )
            asyncio.run(_on_menu_impl(upgrade_update, context))

            self.assertTrue(upgrade_query.answered)
            self.assertEqual(len(license_service.db.data["orders"]), 1)
            order = license_service.db.data["orders"][0]
            self.assertEqual(order["machine_id"], machine_id)
            self.assertEqual(order["plan"], LIFETIME_PLAN)
            self.assertEqual(order["price"], 990000)
            self.assertEqual(order["payment_status"], "pending")
            self.assertEqual(len(upgrade_update.effective_message.photos), 1)
            self.assertNotIn("Chưa có Machine ID", "".join(item["text"] for item in upgrade_query.edits))

        if previous_private_key is None:
            os.environ.pop("PRIVATE_KEY_PEM", None)
        else:
            os.environ["PRIVATE_KEY_PEM"] = previous_private_key

    def test_long_machine_id_upgrade_button_uses_recent_machine_id(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
            license_service = LicenseService(
                private_key_path=root / "unused.pem",
                db_path=root / "licenses_db.json",
                output_dir=root / "issued_licenses",
            )
            machine_id = "BFEB" + "A" * 124
            license_service.db.upsert_user(
                {
                    "telegram_user_id": 123456,
                    "username": "Test User",
                    "machine_id": machine_id,
                    "source": "free_ready",
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

            payment_service = PaymentService(
                PaymentConfig(
                    bank_name="ACB",
                    bank_account="123456789",
                    bank_account_name="TEST ACCOUNT",
                    qr_url="https://example.com/qr.png",
                )
            )
            app = SimpleNamespace(bot_data={"license_service": license_service, "payment_service": payment_service, "admin_ids": set()})
            user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
            context = SimpleNamespace(application=app)

            callback_data = first_callback_data(_upgrade_permanent_keyboard_for_machine(machine_id))
            self.assertEqual(callback_data, f"license_plan:{YEAR_365_PLAN}")
            self.assertLessEqual(len(callback_data.encode("utf-8")), 64)

            upgrade_query = FakeQuery()
            upgrade_query.data = callback_data
            upgrade_update = SimpleNamespace(
                effective_user=user,
                effective_message=FakeMessage(),
                callback_query=upgrade_query,
            )
            asyncio.run(_on_menu_impl(upgrade_update, context))

            self.assertTrue(upgrade_query.answered)
            self.assertEqual(len(license_service.db.data["orders"]), 1)
            order = license_service.db.data["orders"][0]
            self.assertEqual(order["machine_id"], machine_id)
            self.assertEqual(order["plan"], YEAR_365_PLAN)
            self.assertEqual(order["price"], 450000)
            self.assertEqual(order["payment_status"], "pending")

        if previous_private_key is None:
            os.environ.pop("PRIVATE_KEY_PEM", None)
        else:
            os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
