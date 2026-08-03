from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot
from repository.store_repository import StoreRepository
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory


OFFICE_CODE = "OFFICE_2024_LIFETIME"
OFFICE_NAME = "Microsoft Office LTSC 2024 Professional Plus"


def _write_office_workbook(path: Path, count: int = 15) -> list[str]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "OFFICE"
    sheet.append(REQUIRED_COLUMNS)
    credentials: list[str] = []
    for index in range(count):
        key = f"AAAAA-BBBBB-CCCCC-DDDDD-{index:05d}"
        credential = f"{key}|Office-2024-LTSC"
        credentials.append(credential)
        sheet.append(
            [
                OFFICE_CODE,
                "SOFTWARE",
                "Microsoft Office LTSC 2024 Professional Plus  Trọn đời",
                "license_key",
                "LIFETIME",
                198000,
                365,
                credential,
                "",
                1,
            ]
        )
    workbook.save(path)
    workbook.close()
    return credentials


def test_office_2024_lifetime_import_menu_order_and_delivery() -> None:
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    previous_make_order_id = bot._make_order_id
    previous_orders_path = bot.ORDERS_DB_PATH
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        workbook_path = root / "office.xlsx"
        orders_path = root / "orders.json"
        bot._initialize_store_db(db_path)
        credentials = _write_office_workbook(workbook_path, count=15)

        report = import_inventory(workbook_path, db_path, mode="replace")
        assert report["row_errors"] == 0
        assert report["credentials_added"] == 15
        assert report["stock"] == {OFFICE_CODE: 15}

        try:
            os.environ["STORE_DB_PATH"] = str(db_path)
            bot.ORDERS_DB_PATH = orders_path
            repo = StoreRepository(db_path)

            product = repo.get_product_details(OFFICE_CODE)
            assert product is not None
            assert product["code"] == OFFICE_CODE
            assert product["price_vnd"] == 198000
            assert product["duration"] == "LIFETIME"
            assert product["warranty_days"] == 365
            assert repo.get_stock_count(OFFICE_CODE) == 15

            menu_labels = [button.text for row in bot._product_menu_keyboard().inline_keyboard for button in row]
            assert any(f"{OFFICE_NAME} (15)" in label for label in menu_labels)

            quantity_text = bot._quantity_text(OFFICE_NAME, OFFICE_CODE)
            assert OFFICE_NAME in quantity_text
            assert "198.000" in quantity_text
            assert "Kho: 15" in quantity_text
            assert "Thời hạn: Trọn đời" in quantity_text
            assert "Bảo hành: 12 tháng" in quantity_text

            fake_update = type(
                "FakeOrderUpdate",
                (),
                {"effective_user": type("FakeUser", (), {"id": 42, "full_name": "Office User", "username": ""})()},
            )()
            bot._make_order_id = lambda _product_name: "ORD-OFFICE-1"
            order_one = bot._create_sales_order(fake_update, OFFICE_NAME, OFFICE_CODE, 1)
            assert order_one["product_code"] == OFFICE_CODE
            assert order_one["unit_price"] == 198000
            assert order_one["total"] == 198000

            assert repo.mark_order_paid("ORD-OFFICE-1", "TX-OFFICE-1")
            delivered_one = repo.deliver_reserved_items("ORD-OFFICE-1")
            assert len(delivered_one) == 1
            assert "🔑 Product Key:" in delivered_one[0]
            assert "📦 Phiên bản:" in delivered_one[0]
            assert "Office-2024-LTSC" in delivered_one[0]
            assert "🛡 Bảo hành:" in delivered_one[0]
            assert "12 tháng" in delivered_one[0]
            assert "|Office-2024-LTSC" not in delivered_one[0]
            assert "password" not in delivered_one[0].lower()
            assert repo.get_stock_count(OFFICE_CODE) == 14

            with closing(sqlite3.connect(db_path)) as connection:
                delivered_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM inventory_items AS i
                    JOIN products AS p ON p.id = i.product_id
                    WHERE p.code = ? AND i.status = 'delivered'
                    """,
                    (OFFICE_CODE,),
                ).fetchone()[0]
                available_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM inventory_items AS i
                    JOIN products AS p ON p.id = i.product_id
                    WHERE p.code = ? AND i.status = 'available'
                    """,
                    (OFFICE_CODE,),
                ).fetchone()[0]
            assert delivered_count == 1
            assert available_count == 14

            bot._make_order_id = lambda _product_name: "ORD-OFFICE-2"
            order_two = bot._create_sales_order(fake_update, OFFICE_NAME, OFFICE_CODE, 2)
            assert order_two["product_code"] == OFFICE_CODE
            assert order_two["unit_price"] == 198000
            assert order_two["total"] == 396000

            with closing(sqlite3.connect(db_path)) as connection:
                old_or_other = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM order_inventory_items AS oi
                    JOIN inventory_items AS i ON i.id = oi.inventory_item_id
                    JOIN products AS p ON p.id = i.product_id
                    WHERE oi.order_id = (SELECT id FROM orders WHERE order_id = ?)
                      AND p.code != ?
                    """,
                    ("ORD-OFFICE-2", OFFICE_CODE),
                ).fetchone()[0]
            assert old_or_other == 0
            assert all("|Office-2024-LTSC" in item for item in credentials)
        finally:
            bot._make_order_id = previous_make_order_id
            bot.ORDERS_DB_PATH = previous_orders_path
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
