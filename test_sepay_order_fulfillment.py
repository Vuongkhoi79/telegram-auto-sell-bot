from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import bank_checker
import sepay_webhook
import telegram_license_bot as botmod
from license_service import LIFETIME_PLAN, YEAR_365_PLAN, LicenseService
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


def pending_order(**overrides):
    now = datetime.now(timezone.utc)
    order = {
        "order_id": "ORD-TEST-001",
        "telegram_user_id": 123456,
        "username": "Test User",
        "product_id": "CAPCUT PRO",
        "product_name": "CAPCUT PRO",
        "package_name": "7D",
        "quantity": 1,
        "unit_price": 99000,
        "total": 99000,
        "amount": 99000,
        "delivery_type": "account",
        "payment_method": "",
        "payment_status": "pending",
        "order_status": "pending",
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(minutes=5)).isoformat(),
        "paid_at": "",
        "delivered_at": "",
        "delivery": "",
    }
    order.update(overrides)
    return order


class SePayOrderFulfillmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        self.previous_store_db_path = os.environ.get("STORE_DB_PATH")
        self.old_paths = (
            botmod.ORDERS_DB_PATH,
            botmod.INVENTORY_PATH,
            bank_checker.ORDERS_DB_PATH,
            sepay_webhook.ORDERS_DB_PATH,
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        orders_path = self.root / "orders_db.json"
        inventory_path = self.root / "inventory.json"
        self.store_db_path = self.root / "store.db"
        botmod.ORDERS_DB_PATH = orders_path
        botmod.INVENTORY_PATH = inventory_path
        bank_checker.ORDERS_DB_PATH = orders_path
        sepay_webhook.ORDERS_DB_PATH = orders_path
        sepay_webhook.PROCESSED_TRANSACTIONS_PATH = self.root / "processed_transactions.json"
        sepay_webhook.UNMATCHED_TRANSACTIONS_PATH = self.root / "unmatched_transactions.json"
        botmod._initialize_store_db(self.store_db_path)
        os.environ["STORE_DB_PATH"] = str(self.store_db_path)
        os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
        self.license_service = LicenseService(
            private_key_path=self.root / "unused_private_key.pem",
            db_path=self.root / "licenses_db.json",
            output_dir=self.root / "issued_licenses",
        )
        self.bot = FakeTelegramBot()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"license_service": self.license_service}),
            bot=self.bot,
        )

    def tearDown(self) -> None:
        (
            botmod.ORDERS_DB_PATH,
            botmod.INVENTORY_PATH,
            bank_checker.ORDERS_DB_PATH,
            sepay_webhook.ORDERS_DB_PATH,
            sepay_webhook.PROCESSED_TRANSACTIONS_PATH,
            sepay_webhook.UNMATCHED_TRANSACTIONS_PATH,
        ) = self.old_paths
        if self.previous_private_key is None:
            os.environ.pop("PRIVATE_KEY_PEM", None)
        else:
            os.environ["PRIVATE_KEY_PEM"] = self.previous_private_key
        if self.previous_store_db_path is None:
            os.environ.pop("STORE_DB_PATH", None)
        else:
            os.environ["STORE_DB_PATH"] = self.previous_store_db_path
        self.temp_dir.cleanup()

    def _reserve_sqlite_chatgpt(self, order: dict[str, object], credential: str) -> None:
        now = str(order["created_at"])
        with closing(sqlite3.connect(self.store_db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO products
                        (id, code, name, active, delivery_type, created_at, updated_at)
                    VALUES (?, ?, ?, 1, 'account', ?, ?)
                    """,
                    ("chatgpt-product", "GPT-PLUS-1M-PRIVATE", "CHATGPT", now, now),
                )
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                    ("chatgpt-item", "chatgpt-product", credential, now),
                )
        botmod.StoreRepository(self.store_db_path).create_pending_account_order_and_reserve(
            order_id=str(order["order_id"]),
            telegram_user_id=int(order["telegram_user_id"]),
            username=str(order["username"]),
            product_code="GPT-PLUS-1M-PRIVATE",
            product_name=str(order["product_name"]),
            package_name=str(order["package_name"]),
            quantity=int(order["quantity"]),
            unit_price_vnd=int(order["unit_price"]),
            total_vnd=int(order["total"]),
            created_at=str(order["created_at"]),
            expire_at=str(order["expire_at"]),
        )

    async def _fulfill(self, order_id: str) -> dict[str, object]:
        return await fulfill_order(self.context, order_id)

    def test_sepay_fulfills_capcut_account_preserving_email_password_format(self) -> None:
        credential = "capcut@example.com|pass123"
        botmod.INVENTORY_PATH.write_text(
            json.dumps({"CAPCUT PRO": {"stock": 1, "active": True, "deliverables": [credential]}}, ensure_ascii=False),
            encoding="utf-8",
        )
        order = pending_order(order_id="ORD-CAPCUT-001", total=99000, amount=99000)
        botmod.ORDERS_DB_PATH.write_text(json.dumps([order], ensure_ascii=False), encoding="utf-8")

        payload = {"transaction_id": "SEPAY-CAPCUT-001", "transferAmount": 99000, "addInfo": f"PAY {order['order_id']}"}
        result = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))

        self.assertTrue(result["ok"])
        self.assertEqual(result["fulfillment"]["type"], "sales_order")
        self.assertIn(credential, self.bot.messages[0]["text"])
        updated_order = botmod._find_order(order["order_id"])
        self.assertEqual(updated_order["delivery"], credential)

    def test_sepay_fulfills_account_preserving_email_2fa_password_format(self) -> None:
        credential = "ai@example.com|JBSWY3DPEHPK3PXP|pass123"
        order = pending_order(
            order_id="ORD-CHATGPT-001",
            product_id="CHATGPT",
            product_name="CHATGPT",
            total=99000,
            amount=99000,
        )
        botmod.ORDERS_DB_PATH.write_text(json.dumps([order], ensure_ascii=False), encoding="utf-8")
        self._reserve_sqlite_chatgpt(order, credential)

        payload = {"transaction_id": "SEPAY-AI-001", "transferAmount": 99000, "addInfo": f"PAY {order['order_id']}"}
        result = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))

        self.assertTrue(result["ok"])
        self.assertEqual(result["fulfillment"]["type"], "sales_order")
        self.assertIn(credential, self.bot.messages[0]["text"])
        updated_order = botmod._find_order(order["order_id"])
        self.assertEqual(updated_order["delivery"], credential)
        duplicate = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))
        self.assertEqual(duplicate["status"], "duplicate")
        with closing(sqlite3.connect(self.store_db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM inventory_items WHERE id = 'chatgpt-item'").fetchone()[0],
                "delivered",
            )

    def test_sepay_fulfills_year_license_from_common_order(self) -> None:
        machine_id = "TEST-MACHINE-YEAR-001"
        order = pending_order(
            order_id="ORD-YEAR-001",
            product_id="TOOL_YEAR_365",
            product_name="💎 Gia hạn 1 năm - 450.000đ",
            package_name=YEAR_365_PLAN,
            unit_price=450000,
            total=450000,
            amount=450000,
            delivery_type="license",
            machine_id=machine_id,
            plan=YEAR_365_PLAN,
            duration_days=365,
            price_vnd=450000,
            lifetime=False,
            license_file="",
        )
        botmod.ORDERS_DB_PATH.write_text(json.dumps([order], ensure_ascii=False), encoding="utf-8")

        self.assertEqual(len(self.license_service.db.data["licenses"]), 0)
        payload = {"transaction_id": "SEPAY-YEAR-001", "transferAmount": 450000, "addInfo": f"PAY {order['order_id']}"}
        result = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))

        self.assertTrue(result["ok"])
        self.assertEqual(result["fulfillment"]["type"], "license")
        record = self.license_service.db.latest_license_by_machine(machine_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["plan"], YEAR_365_PLAN)
        self.assertEqual(record["duration_days"], 365)
        self.assertFalse(record["lifetime"])
        self.assertEqual(record["price"], 450000)
        self.assertEqual(len(self.bot.documents), 1)

    def test_sepay_fulfills_lifetime_license_from_common_order_once(self) -> None:
        machine_id = "TEST-MACHINE-LIFETIME-001"
        order = pending_order(
            order_id="ORD-LIFETIME-001",
            product_id="TOOL_LIFETIME",
            product_name="🚀 Vĩnh viễn - 990.000đ",
            package_name=LIFETIME_PLAN,
            unit_price=990000,
            total=990000,
            amount=990000,
            delivery_type="license",
            machine_id=machine_id,
            plan=LIFETIME_PLAN,
            expire_date="2099-12-31",
            lifetime=True,
            price_vnd=990000,
            license_file="",
        )
        botmod.ORDERS_DB_PATH.write_text(json.dumps([order], ensure_ascii=False), encoding="utf-8")

        payload = {"transaction_id": "SEPAY-LIFETIME-001", "transferAmount": 990000, "addInfo": f"PAY {order['order_id']}"}
        first = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))
        second = asyncio.run(sepay_webhook.process_sepay_payload(payload, self._fulfill))

        self.assertTrue(first["ok"])
        self.assertEqual(first["fulfillment"]["type"], "license")
        self.assertEqual(second["status"], "duplicate")
        licenses = [item for item in self.license_service.db.data["licenses"] if item.get("order_id") == order["order_id"]]
        self.assertEqual(len(licenses), 1)
        payload_data = json.loads(Path(licenses[0]["license_file"]).read_text(encoding="utf-8"))["payload"]
        self.assertEqual(payload_data["plan"], LIFETIME_PLAN)
        self.assertTrue(payload_data["lifetime"])
        self.assertEqual(payload_data["expire_date"], "2099-12-31")
        self.assertEqual(len(self.bot.documents), 1)


if __name__ == "__main__":
    unittest.main()
