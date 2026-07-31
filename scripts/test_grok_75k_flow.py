from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot
from repository.store_repository import StoreRepository
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory, normalize_import_product_code


def _insert_old_grok_stock(db_path: Path, count: int = 2) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, code, name, active, delivery_type, created_at, updated_at,
                     category, account_type, duration, price_vnd, warranty_days,
                     category_key, product_group)
                VALUES (?, 'GROK', 'Old GROK 10K', 1, 'account', ?, ?,
                        'account', 'personal', '7D', 10000, 7, 'GROK', 'account')
                """,
                ("old-grok", now, now),
            )
            for index in range(count):
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, 'old-grok', ?, 'available', ?)",
                    (f"old-grok-item-{index}", f"old-grok-{index}@example.com|oldpass", now),
                )


def _write_grok_75k_workbook(path: Path, count: int = 10) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "GROK"
    sheet.append(REQUIRED_COLUMNS)
    for index in range(count):
        sheet.append(
            [
                "GROK",
                "AI",
                "SuperGrok AI",
                "personal",
                "30D",
                75000,
                7,
                f"grok75-{index}@example.com|newpass",
                "",
                1,
            ]
        )
    workbook.save(path)
    workbook.close()


def test_grok_75k_menu_order_reserve_and_delivery_do_not_use_old_grok() -> None:
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    previous_make_order_id = bot._make_order_id
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        workbook_path = root / "grok-75k.xlsx"
        bot._initialize_store_db(db_path)
        _insert_old_grok_stock(db_path, count=2)
        _write_grok_75k_workbook(workbook_path, count=10)

        report = import_inventory(workbook_path, db_path, mode="replace")
        assert report["stock"] == {"GROK_75K": 10}
        assert normalize_import_product_code(
            "GROK", {"duration": "30D", "price_vnd": 75000, "warranty_days": 7}
        ) == "GROK_75K"

        try:
            os.environ["STORE_DB_PATH"] = str(db_path)
            repo = StoreRepository(db_path)
            assert repo.get_stock_count("GROK_75K") == 10

            menu_labels = [button.text for row in bot._product_menu_keyboard().inline_keyboard for button in row]
            assert any("SUPERGROK AI (10)" in label for label in menu_labels)
            assert bot._menu_available_count("GROK") == 10
            assert bot._menu_available_count("GROK SUPER") == 10
            assert bot._menu_available_count("SUPERGROK AI") == 10

            package = bot._get_package_info("GROK SUPER", "GROK_75K")
            assert package is not None
            assert package["product_code"] == "GROK_75K"
            assert package["price_vnd"] == 75000
            assert package["available_count"] == 10

            quantity_text = bot._quantity_text("GROK SUPER", "GROK_75K")
            assert "SUPERGROK AI" in quantity_text
            assert "75.000" in quantity_text
            assert "Kho: 10" in quantity_text
            assert "Bảo hành: 7 ngày" in quantity_text

            fake_update = type(
                "FakeOrderUpdate",
                (),
                {"effective_user": type("FakeUser", (), {"id": 42, "full_name": "Test User", "username": ""})()},
            )()
            bot._make_order_id = lambda _product_name: "ORD-GROK-75K-1"
            order_one = bot._create_sales_order(fake_update, "GROK SUPER", "GROK_75K", 1)
            assert order_one["product_code"] == "GROK_75K"
            assert order_one["unit_price"] == 75000
            assert order_one["total"] == 75000

            bot._make_order_id = lambda _product_name: "ORD-GROK-75K-2"
            order_two = bot._create_sales_order(fake_update, "GROK SUPER", "GROK_75K", 2)
            assert order_two["product_code"] == "GROK_75K"
            assert order_two["unit_price"] == 75000
            assert order_two["total"] == 150000

            persisted = repo.find_order("ORD-GROK-75K-2")
            assert persisted is not None
            assert persisted["product_code"] == "GROK_75K"
            assert persisted["unit_price_vnd"] == 75000
            assert persisted["total_vnd"] == 150000
            assert persisted["quantity"] == 2

            assert repo.mark_order_paid("ORD-GROK-75K-2", "TX-GROK-75K-2")
            delivered = repo.deliver_reserved_items("ORD-GROK-75K-2")
            assert len(delivered) == 2
            assert all(item.startswith("grok75-") for item in delivered)
            assert not any(item.startswith("old-grok-") for item in delivered)

            with closing(sqlite3.connect(db_path)) as connection:
                old_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM inventory_items AS i
                    JOIN products AS p ON p.id = i.product_id
                    WHERE p.code = 'GROK' AND i.status = 'available'
                    """
                ).fetchone()[0]
            assert old_count == 2
        finally:
            bot._make_order_id = previous_make_order_id
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
