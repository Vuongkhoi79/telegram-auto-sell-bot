from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from repository.store_repository import StoreRepository
from scripts.sales_flow_state import canonical_product_code, snapshot_sales_state
import telegram_license_bot as bot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnose Telegram sales flow state.")
    parser.add_argument("--database", required=True, help="Path to store.db")
    parser.add_argument("--product", required=True, help="Canonical product code, e.g. GEMINI")
    parser.add_argument("--quantity", type=int, required=True, help="Requested quantity")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_path = Path(args.database)
    product_code = canonical_product_code(args.product)
    quantity = int(args.quantity)

    repo = StoreRepository(database_path)
    product = repo.get_product_details(product_code) if product_code else None
    package = bot._get_package_info(product_code, product_code) if product_code else None
    state = snapshot_sales_state(database_path, product_code, callback_data=f"diagnose:{product_code}:{quantity}", package_id=product_code, quantity=quantity)

    print(f"product exists? {'yes' if product else 'no'}")
    print(f"package resolves to canonical product? {'yes' if package and canonical_product_code(str(package.get('product_code', ''))) == product_code else 'no'}")
    print(f"available before: {state.available_count}")
    print(f"can reserve? {'yes' if state.can_reserve else 'no'}")
    print(f"expected available after: {state.expected_available_after}")
    if not state.can_reserve:
        print(f"reason: {state.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
