from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import telegram_license_bot as botmod
from license_service import LIFETIME_PLAN, YEAR_365_PLAN, LicenseService
from payment_service import PaymentConfig, PaymentService
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
        self.message = None

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})


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
    def test_free_success_button_creates_lifetime_order_but_no_paid_license(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        old_orders_path = botmod.ORDERS_DB_PATH

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                botmod.ORDERS_DB_PATH = root / "orders_db.json"
                os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
                license_service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )
                machine_id = "F461FE60-342ADFEF-C1AE4E7B-A5FC9744"
                license_service.touch_user(123456, "Test User", machine_id=machine_id, source="test", reminder_state="active")
                payment_service = PaymentService(
                    PaymentConfig(
                        bank_name="ACB",
                        bank_account="123456789",
                        bank_account_name="TEST ACCOUNT",
                    )
                )
                app = SimpleNamespace(bot_data={"license_service": license_service, "payment_service": payment_service, "admin_ids": set()})
                user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
                message = FakeMessage()
                update = SimpleNamespace(effective_user=user, effective_message=message, callback_query=None)
                context = SimpleNamespace(application=app)

                asyncio.run(_handle_free_license_click(update, context))

                self.assertEqual(len(license_service.db.data["licenses"]), 1)
                callback_data = callback_data_at(message.texts[0]["reply_markup"], 1)
                self.assertEqual(callback_data, "license_product:TOOL_LIFETIME")

                upgrade_query = FakeQuery()
                upgrade_query.data = callback_data
                upgrade_update = SimpleNamespace(
                    effective_user=user,
                    effective_message=FakeMessage(),
                    callback_query=upgrade_query,
                )
                asyncio.run(_on_menu_impl(upgrade_update, context))

                self.assertEqual(botmod._load_orders(), [])
                self.assertIn("Machine ID", upgrade_query.edits[0]["text"])

                machine_message = FakeMessage()
                machine_message.text = machine_id
                machine_update = SimpleNamespace(effective_user=user, effective_message=machine_message, callback_query=None)
                asyncio.run(botmod.on_text_machine_id(machine_update, context))

                orders = botmod._load_orders()
                self.assertEqual(len(orders), 1)
                order = orders[0]
                self.assertEqual(order["product_id"], "TOOL_LIFETIME")
                self.assertEqual(order["delivery_type"], "license")
                self.assertEqual(order["machine_id"], machine_id)
                self.assertEqual(order["plan"], LIFETIME_PLAN)
                self.assertEqual(order["total"], 990000)
                self.assertEqual(order["payment_status"], "pending")
                self.assertEqual(len(machine_message.photos), 1)
                self.assertEqual(len(license_service.db.data["licenses"]), 1)
        finally:
            botmod.ORDERS_DB_PATH = old_orders_path
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key

    def test_long_machine_id_year_button_uses_recent_machine_id_and_common_order(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        old_orders_path = botmod.ORDERS_DB_PATH

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                botmod.ORDERS_DB_PATH = root / "orders_db.json"
                os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
                license_service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )
                machine_id = "BFEB" + "A" * 124
                license_service.touch_user(123456, "Test User", machine_id=machine_id, source="test", reminder_state="active")
                payment_service = PaymentService(PaymentConfig(bank_name="ACB", bank_account="123456789", bank_account_name="TEST ACCOUNT"))
                app = SimpleNamespace(bot_data={"license_service": license_service, "payment_service": payment_service, "admin_ids": set()})
                user = SimpleNamespace(id=123456, username="testuser", full_name="Test User")
                context = SimpleNamespace(application=app)

                callback_data = callback_data_at(_upgrade_permanent_keyboard_for_machine(machine_id), 0)
                self.assertEqual(callback_data, "license_product:TOOL_YEAR_365")
                self.assertLessEqual(len(callback_data.encode("utf-8")), 64)

                upgrade_query = FakeQuery()
                upgrade_query.data = callback_data
                upgrade_update = SimpleNamespace(effective_user=user, effective_message=FakeMessage(), callback_query=upgrade_query)
                asyncio.run(_on_menu_impl(upgrade_update, context))

                self.assertEqual(botmod._load_orders(), [])
                self.assertIn("Machine ID", upgrade_query.edits[0]["text"])

                machine_message = FakeMessage()
                machine_message.text = machine_id
                machine_update = SimpleNamespace(effective_user=user, effective_message=machine_message, callback_query=None)
                asyncio.run(botmod.on_text_machine_id(machine_update, context))

                orders = botmod._load_orders()
                self.assertEqual(len(orders), 1)
                order = orders[0]
                self.assertEqual(order["product_id"], "TOOL_YEAR_365")
                self.assertEqual(order["delivery_type"], "license")
                self.assertEqual(order["machine_id"], machine_id)
                self.assertEqual(order["plan"], YEAR_365_PLAN)
                self.assertEqual(order["total"], 450000)
                self.assertEqual(order["payment_status"], "pending")
                self.assertEqual(len(license_service.db.data["licenses"]), 0)
        finally:
            botmod.ORDERS_DB_PATH = old_orders_path
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
