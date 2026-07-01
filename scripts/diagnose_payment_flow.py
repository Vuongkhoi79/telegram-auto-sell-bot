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
    parser = argparse.ArgumentParser(description="Diagnose Telegram purchase to payment flow.")
    parser.add_argument("--database", required=True, help="Path to store.db")
    parser.add_argument("--product", required=True, help="Product code or display name")
    parser.add_argument("--package", required=True, help="Canonical package code or label")
    parser.add_argument("--quantity", required=True, type=int, help="Requested quantity")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.database)
    product_code = canonical_product_code(args.product)
    quantity = int(args.quantity)

    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_db = Path(tmp_dir) / "store.db"
        shutil.copy2(source_db, temp_db)
        previous_store_db_path = os.environ.get("STORE_DB_PATH")
        os.environ["STORE_DB_PATH"] = str(temp_db)
        previous_make_order_id = bot._make_order_id
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
            package = bot._get_package_info(product_code, args.package)
            if not package:
                print("package resolves: False")
                print("order created: no (package is unavailable or invalid)")
                return 0
            package_code = str(package.get("package_code") or package.get("product_code") or args.package).upper()

            state_before = snapshot_sales_state(
                temp_db,
                product_code,
                callback_data=f"qty:{product_code}:{package_code}:{quantity}",
                package_id=package_code,
                quantity=quantity,
            )
            print(f"package resolves: {bool(package)}")
            print(f"available before: {state_before.available_count}")
            print(f"reserve possible: {state_before.can_reserve}")
            print(f"expected available after reserve: {state_before.expected_available_after}")

            fake_order_update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42, full_name="Test User", username=""),
                callback_query=SimpleNamespace(
                    data=f"qty:{product_code}:{package_code}:{quantity}",
                    message=SimpleNamespace(
                        chat_id=123456,
                        message_id=777,
                        reply_text=_noop,
                        reply_photo=_noop,
                        reply_document=_noop,
                    ),
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
            bot._make_order_id = lambda _product_name: "ORD-DIAG-PAYMENT"
            try:
                order = bot._create_sales_order(fake_order_update, product_code, package_code, quantity)
                print(f"order created: yes ({order.get('order_id', '')})")
                print(f"payment amount_vnd: {order.get('total', 0)}")
                print("payment build: yes")
                payment_update = SimpleNamespace(
                    effective_user=fake_order_update.effective_user,
                    callback_query=SimpleNamespace(
                        data=f"pay_acb:{order['order_id']}",
                        message=SimpleNamespace(
                            chat_id=123456,
                            message_id=778,
                            reply_text=_noop,
                            reply_photo=_noop,
                            reply_document=_noop,
                        ),
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
                try:
                    asyncio.run(bot._send_payment_choice(payment_update, order, edit=True))
                    print("final branch after payment choice: PAYMENT")
                    asyncio.run(bot._send_acb_qr(payment_update, context, order["order_id"]))
                    print("final branch after QR: QR")
                except Exception as exc:
                    print(f"final branch after QR: FAIL ({type(exc).__name__}: {exc})")
                state_after = snapshot_sales_state(
                    temp_db,
                    product_code,
                    order_id=order["order_id"],
                    callback_data=f"qty:{product_code}:{package_code}:{quantity}",
                    package_id=package_code,
                    quantity=quantity,
                )
                print(f"available after reserve: {state_after.available_count}")
                print(f"reserved after reserve: {state_after.reserved_count}")
            except Exception as exc:
                print(f"order created: no ({type(exc).__name__}: {exc})")
        finally:
            bot._make_order_id = previous_make_order_id
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
