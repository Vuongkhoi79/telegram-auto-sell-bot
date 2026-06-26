from __future__ import annotations

import argparse
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

import telegram_license_bot as bot
from scripts.sales_flow_state import canonical_product_code, snapshot_sales_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose Telegram callback flow for a sales purchase.")
    parser.add_argument("--database", required=True, help="Path to store.db")
    parser.add_argument("--product", required=True, help="Product code or display name")
    parser.add_argument("--package", required=True, help="Package name or package code")
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
        try:
            packages = bot._packages_for_product(product_code)
            package = bot._get_package_info(product_code, args.package)
            if not package and packages:
                wanted = args.package.strip().upper()
                for candidate in packages:
                    candidate_keys = {
                        str(candidate.get("product_code", "")).strip().upper(),
                        str(candidate.get("display_name", "")).strip().upper(),
                        str(candidate.get("name", "")).strip().upper(),
                    }
                    if wanted in candidate_keys:
                        package = candidate
                        break
                if not package:
                    package = packages[0]
            package_code = str(package.get("product_code", "")) if package else ""
            menu_state = snapshot_sales_state(temp_db, product_code, callback_data=f"product:{product_code}", package_id=package_code, quantity=quantity)
            product_callback = f"product:{product_code}"
            package_callback = f"pkg:{product_code}:{package_code or args.package.upper()}"
            quantity_callback = f"qty:{product_code}:{package_code or args.package.upper()}:{quantity}"
            print(f"product menu callback: {product_callback}")
            print(f"package callback: {package_callback}")
            print(f"quantity callback: {quantity_callback}")
            print(f"parsed product_code: {product_code}")
            print(f"parsed package_code: {package_code or args.package.upper()}")
            print(f"reserve product_code: {package_code or product_code}")
            print(f"available before: {menu_state.available_count}")
            print(f"expected branch: {'QR' if menu_state.can_reserve else 'OUT_OF_STOCK'}")
            print(f"expected available after: {menu_state.expected_available_after}")

            fake_update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42, full_name="Test User", username=""),
                callback_query=SimpleNamespace(
                    data=quantity_callback,
                    message=SimpleNamespace(caption=None),
                    answer=lambda: None,
                    edit_message_text=lambda *a, **k: None,
                    edit_message_caption=lambda *a, **k: None,
                ),
                effective_message=SimpleNamespace(
                    reply_text=lambda *a, **k: None,
                    reply_photo=lambda *a, **k: None,
                    reply_document=lambda *a, **k: None,
                ),
            )
            # Simulate the actual reserve path with the production helper.
            previous_make_order_id = bot._make_order_id
            bot._make_order_id = lambda _product_name: "ORD-DIAG-TELEGRAM"
            try:
                order = bot._create_sales_order(fake_update, product_code, package_code or args.package, quantity)
                print("actual branch: QR")
                print(f"actual order_id: {order.get('order_id', '')}")
                after = snapshot_sales_state(temp_db, product_code, order_id=str(order.get("order_id", "")), callback_data=quantity_callback, package_id=package_code, quantity=quantity)
                print(f"available after actual reserve: {after.available_count}")
                print(f"reserved after actual reserve: {after.reserved_count}")
            except Exception as exc:
                print("actual branch: OUT_OF_STOCK")
                print(f"failure reason: {exc}")
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
