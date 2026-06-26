from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from payment_service import PaymentConfig, PaymentService
import telegram_license_bot as bot
from scripts.sales_flow_state import canonical_product_code, snapshot_sales_state


async def _noop(*args, **kwargs) -> None:
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose runtime product price and payment flow.")
    parser.add_argument("--database", required=True, help="Path to store.db")
    parser.add_argument("--product", required=True, help="Canonical product code or display name")
    parser.add_argument("--package", required=True, help="Canonical package code or display name")
    return parser.parse_args()


def _make_update(callback_data: str) -> SimpleNamespace:
    message = SimpleNamespace(
        chat_id=123456,
        message_id=777,
        reply_text=_noop,
        reply_photo=_noop,
        reply_document=_noop,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42, full_name="Test User", username=""),
        callback_query=SimpleNamespace(
            data=callback_data,
            message=message,
            answer=_noop,
            edit_message_text=_noop,
            edit_message_caption=_noop,
        ),
        effective_message=SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            reply_text=_noop,
            reply_photo=_noop,
            reply_document=_noop,
        ),
    )


def main() -> int:
    args = parse_args()
    product_code = canonical_product_code(args.product)
    package_code = canonical_product_code(args.package)
    callback_chain = {
        "product": f"product:{product_code}",
        "package": f"pkg:{product_code}:{package_code}",
        "quantity": f"qty:{product_code}:{package_code}:1",
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_db = Path(tmp_dir) / "store.db"
        shutil.copy2(Path(args.database), temp_db)
        previous_store_db_path = os.environ.get("STORE_DB_PATH")
        os.environ["STORE_DB_PATH"] = str(temp_db)
        try:
            payment_service = PaymentService(
                PaymentConfig(bank_name="ACB", bank_account="123456789", bank_account_name="AI STORE", qr_url="")
            )
            context = SimpleNamespace(
                application=SimpleNamespace(
                    bot_data={"store_db_path": temp_db, "payment_service": payment_service}
                ),
                user_data={},
            )
            package = bot._get_package_info(product_code, package_code)
            if not package:
                print("product_code:", product_code)
                print("package_code:", package_code)
                print("display name: <not found>")
                print("stock available: 0")
                print("price source: <not found>")
                print("price_vnd: 0")
                print("payment amount: 0")
                print("callback_data chain:", callback_chain)
                print("expected branch after qty 1: out_of_stock")
                print("actual simulated branch: out_of_stock")
                return 1

            state_before = snapshot_sales_state(
                temp_db,
                product_code,
                callback_data=callback_chain["quantity"],
                package_id=package_code,
                quantity=1,
            )
            print(f"product_code: {product_code}")
            print(f"package_code: {package_code}")
            print(f"display name: {package.get('display_name', '')}")
            print(f"stock available: {state_before.available_count}")
            print(f"price source: {package.get('price_source', package.get('source', 'unknown'))}")
            print(f"price_vnd: {int(package.get('price_vnd', 0) or 0)}")

            previous_make_order_id = bot._make_order_id
            bot._make_order_id = lambda _product_name: "ORD-RUNTIME-DIAG"
            try:
                order = bot._create_sales_order(_make_update(callback_chain["quantity"]), product_code, package_code, 1)
                print(f"payment amount: {int(order.get('total', 0) or 0)}")
                print(f"callback_data chain: {callback_chain}")
                print("expected branch after qty 1: payment")

                payment_update = _make_update(f"pay_acb:{order['order_id']}")
                branch = "payment"
                asyncio.run(bot._send_payment_choice(payment_update, order, edit=True))
                try:
                    asyncio.run(bot._send_acb_qr(payment_update, context, order["order_id"]))
                    branch = "qr"
                except Exception as exc:
                    branch = f"error:{type(exc).__name__}"
                    print(f"qr error: {type(exc).__name__}: {exc}")
                print(f"actual simulated branch: {branch}")
                state_after = snapshot_sales_state(
                    temp_db,
                    product_code,
                    order_id=order["order_id"],
                    callback_data=callback_chain["quantity"],
                    package_id=package_code,
                    quantity=1,
                )
                print(f"available after reserve: {state_after.available_count}")
                print(f"reserved after reserve: {state_after.reserved_count}")
            finally:
                bot._make_order_id = previous_make_order_id
        finally:
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
