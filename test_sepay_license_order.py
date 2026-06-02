from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import sepay_webhook
from license_service import LicenseService
from telegram_license_bot import fulfill_order


class FakeTelegramBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})

    async def send_document(self, chat_id: int, document) -> None:
        self.documents.append({"chat_id": chat_id, "filename": document.filename})


def make_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class SePayLicenseOrderTest(unittest.TestCase):
    def test_sepay_fulfills_license_upgrade_order_once(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        old_paths = (
            sepay_webhook.ORDERS_DB_PATH,
            sepay_webhook.LICENSE_DB_PATH,
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
            sepay_webhook.ORDERS_DB_PATH = root / "orders_db.json"
            sepay_webhook.LICENSE_DB_PATH = root / "licenses_db.json"
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH = root / "processed_transactions.json"
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH = root / "unmatched_transactions.json"

            license_service = LicenseService(
                private_key_path=root / "unused_private_key.pem",
                db_path=sepay_webhook.LICENSE_DB_PATH,
                output_dir=root / "issued_licenses",
            )
            order = license_service.create_pending_order(
                123456,
                "Test User @testuser",
                "TEST-MACHINE-001",
                customer="Test User",
            )
            bot = FakeTelegramBot()
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"license_service": license_service}),
                bot=bot,
            )

            async def fulfill(order_id: str) -> dict[str, object]:
                return await fulfill_order(context, order_id)

            payload = {
                "transaction_id": "SEPAY-TX-001",
                "transferAmount": 450000,
                "addInfo": f"AI_DAILY {order['order_id']} TESTMACH",
            }

            first = asyncio.run(sepay_webhook.process_sepay_payload(payload, fulfill))
            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "paid")
            self.assertEqual(first["order_type"], "license")
            self.assertEqual(first["order_id"], order["order_id"])

            license_service.db.load()
            paid_order = license_service.db.find_order(order["order_id"])
            self.assertIsNotNone(paid_order)
            self.assertEqual(paid_order["payment_status"], "paid")
            self.assertEqual(paid_order["transaction_id"], "SEPAY-TX-001")
            self.assertEqual(paid_order["payment_method"], "SEPAY")
            self.assertTrue(paid_order["paid_at"])

            permanent = [
                item
                for item in license_service.db.data["licenses"]
                if item.get("order_id") == order["order_id"] and item.get("license_type") == "permanent"
            ]
            self.assertEqual(len(permanent), 1)
            self.assertEqual(len(bot.messages), 1)
            self.assertEqual(len(bot.documents), 1)

            second = asyncio.run(sepay_webhook.process_sepay_payload(payload, fulfill))
            self.assertTrue(second["ok"])
            self.assertEqual(second["status"], "duplicate")

            license_service.db.load()
            permanent_after_duplicate = [
                item
                for item in license_service.db.data["licenses"]
                if item.get("order_id") == order["order_id"] and item.get("license_type") == "permanent"
            ]
            self.assertEqual(len(permanent_after_duplicate), 1)
            self.assertEqual(len(bot.messages), 1)
            self.assertEqual(len(bot.documents), 1)

        (
            sepay_webhook.ORDERS_DB_PATH,
            sepay_webhook.LICENSE_DB_PATH,
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        ) = old_paths
        if previous_private_key is None:
            os.environ.pop("PRIVATE_KEY_PEM", None)
        else:
            os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
