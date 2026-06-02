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
from telegram_license_bot import _send_upgrade


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[dict[str, object]] = []
        self.photos: list[dict[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.texts.append({"text": text, "reply_markup": reply_markup})

    async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append({"photo": photo, "caption": caption, "reply_markup": reply_markup})


class FakeQuery:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})


def make_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class UpgradeFromFreeLicenseTest(unittest.TestCase):
    def test_upgrade_uses_recent_license_machine_id(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
            license_service = LicenseService(
                private_key_path=root / "unused.pem",
                db_path=root / "licenses_db.json",
                output_dir=root / "issued_licenses",
            )
            license_result = license_service.issue_free_license(
                123456,
                "Test User",
                "F461FE60-342ADFEF-C1AE4E7B-A5FC9744",
                customer="Test User",
            )
            self.assertTrue(license_result.ok)
            license_service.db.upsert_user(
                {
                    "telegram_user_id": 123456,
                    "username": "Test User",
                    "machine_id": "",
                    "source": "manual_blank_state",
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
            app = SimpleNamespace(bot_data={"license_service": license_service, "payment_service": payment_service})
            user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
            update = SimpleNamespace(effective_user=user, effective_message=FakeMessage(), callback_query=FakeQuery())
            context = SimpleNamespace(application=app)

            asyncio.run(_send_upgrade(update, context, edit=True))

            self.assertEqual(len(license_service.db.data["orders"]), 1)
            order = license_service.db.data["orders"][0]
            self.assertEqual(order["machine_id"], "F461FE60-342ADFEF-C1AE4E7B-A5FC9744")
            self.assertEqual(order["price"], 450000)
            self.assertEqual(order["payment_status"], "pending")
            self.assertTrue(update.callback_query.edits)
            self.assertEqual(len(update.effective_message.photos), 1)

        if previous_private_key is None:
            os.environ.pop("PRIVATE_KEY_PEM", None)
        else:
            os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
