from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import bank_checker
import sepay_webhook
import telegram_license_bot as bot
from payment_service import PaymentConfig, PaymentService
from repository.store_repository import StoreRepository


def main() -> None:
    previous_orders_path = bot.ORDERS_DB_PATH
    previous_bank_orders_path = bank_checker.ORDERS_DB_PATH
    previous_sepay_orders_path = sepay_webhook.ORDERS_DB_PATH
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    try:
        with tempfile.TemporaryDirectory() as directory:
            store_db_path = Path(directory) / "store.db"
            bot._initialize_store_db(store_db_path)
            os.environ["STORE_DB_PATH"] = str(store_db_path)

            user = SimpleNamespace(id=123, full_name="Order Test", username="order_test")
            update = SimpleNamespace(effective_user=user)

            first = bot._create_sales_order(update, "VEO3 ULTRA", "7D", 1)
            second = bot._create_sales_order(update, "VEO3 ULTRA", "7D", 1)
            assert first["order_id"] != second["order_id"]

            payment_text = bot._order_payment_text(first)
            assert first["order_id"] in payment_text
            payment_service = PaymentService(
                PaymentConfig(bank_name="ACB", bank_account="123456", bank_account_name="TEST")
            )
            qr_url = bot._build_vietqr_url(first, payment_service)
            assert parse_qs(urlparse(qr_url).query)["addInfo"] == [first["order_id"]]
            assert f"Nội dung CK: {first['order_id']}" in bot._qr_caption(first, payment_service)

            persisted = StoreRepository(store_db_path).list_orders()
            assert len(persisted) == 2
            matched = sepay_webhook.find_pending_order(str(first["order_id"]), int(first["total"]))
            assert matched and matched["order_id"] == first["order_id"]
            assert sepay_webhook.find_pending_order("WRONG-CODE", int(first["total"])) is None
    finally:
        bot.ORDERS_DB_PATH = previous_orders_path
        bank_checker.ORDERS_DB_PATH = previous_bank_orders_path
        sepay_webhook.ORDERS_DB_PATH = previous_sepay_orders_path
        if previous_store_db_path is None:
            os.environ.pop("STORE_DB_PATH", None)
        else:
            os.environ["STORE_DB_PATH"] = previous_store_db_path
    print("ORDER_PAYMENT_CODE_TEST=PASS")


if __name__ == "__main__":
    main()
