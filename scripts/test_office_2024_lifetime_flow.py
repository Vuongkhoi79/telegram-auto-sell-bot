from __future__ import annotations

import os
import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot
from payment_service import PaymentConfig, PaymentService
from repository.store_repository import StoreRepository
from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory


OFFICE_CODE = "OFFICE_2024_LIFETIME"
OFFICE_NAME = "Microsoft Office LTSC 2024 Professional Plus"


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[tuple[str, object | None]] = []
        self.replies: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.answered = False
        self.message = SimpleNamespace(
            caption=None,
            chat_id=123456,
            message_id=777,
            reply_text=self._reply_text,
            reply_photo=self._reply_photo,
        )

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.edits.append((caption, reply_markup))

    async def _reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))


class FakeUpdate:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=user_id, full_name="Office User", username="")
        self.effective_message = SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            text="",
            reply_text=self.callback_query._reply_text,
            reply_photo=self.callback_query._reply_photo,
        )


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
                "LIFETIME",
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
            assert product["warranty_days"] == 0
            assert repo.get_stock_count(OFFICE_CODE) == 15

            menu_labels = [button.text for row in bot._product_menu_keyboard().inline_keyboard for button in row]
            assert any(f"{OFFICE_NAME} (15)" in label for label in menu_labels)

            quantity_text = bot._quantity_text(OFFICE_NAME, OFFICE_CODE)
            assert OFFICE_NAME in quantity_text
            assert "198.000" in quantity_text
            assert "Kho: 15" in quantity_text
            assert "Thời hạn: Trọn đời" in quantity_text
            assert "Bảo hành: Trọn đời" in quantity_text
            assert "12 tháng" not in quantity_text
            assert "365 ngày" not in quantity_text
            assert "12M" not in quantity_text
            assert "Bảo hành: 7 ngày" not in quantity_text

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
            assert "Trọn đời" in delivered_one[0]
            assert "12 tháng" not in delivered_one[0]
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


def test_office_2024_lifetime_callback_payment_and_delivery_with_software_category_key() -> None:
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    previous_make_order_id = bot._make_order_id
    previous_orders_path = bot.ORDERS_DB_PATH
    previous_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        workbook_path = root / "office.xlsx"
        orders_path = root / "orders.json"
        bot._initialize_store_db(db_path)
        _write_office_workbook(workbook_path, count=15)
        report = import_inventory(workbook_path, db_path, mode="replace")
        assert report["row_errors"] == 0
        assert report["credentials_added"] == 15

        # Production imported Office as SOFTWARE before package lookup knew about this
        # product. Menu still used products.code, but package lookup used category_key.
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                """
                UPDATE products
                SET category = 'SOFTWARE', category_key = 'SOFTWARE', account_type = 'license_key'
                WHERE code = ?
                """,
                (OFFICE_CODE,),
            )
            connection.commit()

        try:
            os.environ["STORE_DB_PATH"] = str(db_path)
            bot.ORDERS_DB_PATH = orders_path
            bot.BUSINESS_PARTNERS_PATH = root / "business_partners.json"
            context = SimpleNamespace(
                application=SimpleNamespace(
                    bot_data={
                        "store_db_path": db_path,
                        "bot_username": "AIDailyBot",
                        "payment_service": PaymentService(
                            PaymentConfig(
                                bank_name="ACB",
                                bank_account="123456789",
                                bank_account_name="AI STORE",
                                qr_url="",
                            )
                        ),
                    }
                ),
                args=[],
                user_data={},
            )

            menu_buttons = [button for row in bot._product_menu_keyboard().inline_keyboard for button in row]
            office_button = next(button for button in menu_buttons if OFFICE_NAME in button.text)
            assert office_button.callback_data == f"product:{OFFICE_CODE}"
            assert "(15)" in office_button.text

            product_update = FakeUpdate(f"product:{OFFICE_CODE}")
            asyncio.run(bot._on_menu_impl(product_update, context))
            product_detail_text = product_update.callback_query.edits[-1][0]
            assert "Bảo hành: Trọn đời" in product_detail_text
            assert "12 tháng" not in product_detail_text
            assert "365 ngày" not in product_detail_text
            assert "12M" not in product_detail_text
            assert "Bảo hành: 7 ngày" not in product_detail_text
            package_markup = product_update.callback_query.edits[-1][1]
            package_buttons = [button for row in package_markup.inline_keyboard for button in row]
            package_button = next(button for button in package_buttons if button.callback_data.startswith("pkg:"))
            assert package_button.callback_data == f"pkg:{OFFICE_CODE}:{OFFICE_CODE}"
            assert "198.000" in package_button.text
            assert "15" in package_button.text
            assert "Bảo hành: Trọn đời" in package_button.text
            assert "12 tháng" not in package_button.text
            assert "365 ngày" not in package_button.text
            assert "12M" not in package_button.text
            assert "Bảo hành: 7 ngày" not in package_button.text

            package_update = FakeUpdate(f"pkg:{OFFICE_CODE}:{OFFICE_CODE}")
            asyncio.run(bot._on_menu_impl(package_update, context))
            quantity_text = package_update.callback_query.edits[-1][0]
            assert "Bảo hành: Trọn đời" in quantity_text
            assert "12 tháng" not in quantity_text
            assert "365 ngày" not in quantity_text
            assert "12M" not in quantity_text
            assert "Bảo hành: 7 ngày" not in quantity_text
            quantity_markup = package_update.callback_query.edits[-1][1]
            quantity_buttons = [button for row in quantity_markup.inline_keyboard for button in row]
            assert any(button.callback_data == f"qty:{OFFICE_CODE}:{OFFICE_CODE}:1" for button in quantity_buttons)
            assert any(button.callback_data == f"qty:{OFFICE_CODE}:{OFFICE_CODE}:2" for button in quantity_buttons)

            bot._make_order_id = lambda _product_name: "ORD-OFFICE-CB-1"
            qty_update = FakeUpdate(f"qty:{OFFICE_CODE}:{OFFICE_CODE}:1")
            asyncio.run(bot._on_menu_impl(qty_update, context))
            assert "dtkd_order_ref" in context.user_data
            assert context.user_data["dtkd_order_ref"] == {
                "product_code": OFFICE_CODE,
                "package_code": OFFICE_CODE,
                "quantity": 1,
            }
            assert "pending_order_id" not in context.user_data

            skip_update = FakeUpdate("dtkd_order_ref_skip")
            asyncio.run(bot._on_menu_impl(skip_update, context))
            payment_text, payment_markup = skip_update.callback_query.edits[-1]
            assert "198.000" in payment_text
            assert "Bảo hành\nTrọn đời" in payment_text
            assert "12 tháng" not in payment_text
            assert "365 ngày" not in payment_text
            assert "12M" not in payment_text
            assert "7 ngày" not in payment_text
            assert context.user_data["pending_order_id"] == "ORD-OFFICE-CB-1"
            pay_button = next(
                button
                for row in payment_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("pay_acb:")
            )
            assert pay_button.callback_data == "pay_acb:ORD-OFFICE-CB-1"

            repo = StoreRepository(db_path)
            order_one = repo.find_order("ORD-OFFICE-CB-1")
            assert order_one is not None
            assert order_one["product_code"] == OFFICE_CODE
            assert order_one["package_code"] == OFFICE_CODE
            assert order_one["quantity"] == 1
            assert order_one["unit_price"] == 198000
            assert order_one["total"] == 198000

            qr_update = FakeUpdate("pay_acb:ORD-OFFICE-CB-1")
            asyncio.run(bot._on_menu_impl(qr_update, context))
            assert qr_update.callback_query.photos
            qr_caption = qr_update.callback_query.photos[-1][0]
            assert "198.000" in qr_caption
            assert "ORD-OFFICE-CB-1" in qr_caption
            assert "Bảo hành\nTrọn đời" in qr_caption
            assert "12 tháng" not in qr_caption
            assert "365 ngày" not in qr_caption
            assert "12M" not in qr_caption
            assert "7 ngày" not in qr_caption

            repo.mark_order_paid("ORD-OFFICE-CB-1", "TX-OFFICE-CB-1")
            sent_messages: list[str] = []
            context.bot = SimpleNamespace(send_message=lambda chat_id, text, reply_markup=None: sent_messages.append(text))

            async def send_message(chat_id, text, reply_markup=None):
                sent_messages.append(text)

            context.bot.send_message = send_message
            fulfillment = asyncio.run(bot.fulfill_order(context, "ORD-OFFICE-CB-1"))
            assert fulfillment["ok"] is True
            delivery_text = str(fulfillment["delivery"])
            assert "Product Key:" in delivery_text
            assert "Phiên bản:" in delivery_text
            assert "Office-2024-LTSC" in delivery_text
            assert "Trọn đời" in delivery_text
            assert "12 tháng" not in delivery_text
            assert "|Office-2024-LTSC" not in delivery_text
            assert "password" not in delivery_text.lower()
            assert repo.get_stock_count(OFFICE_CODE) == 14

            bot._make_order_id = lambda _product_name: "ORD-OFFICE-CB-2"
            qty_two_update = FakeUpdate(f"qty:{OFFICE_CODE}:{OFFICE_CODE}:2")
            asyncio.run(bot._on_menu_impl(qty_two_update, context))
            skip_two_update = FakeUpdate("dtkd_order_ref_skip")
            asyncio.run(bot._on_menu_impl(skip_two_update, context))
            order_two = repo.find_order("ORD-OFFICE-CB-2")
            assert order_two is not None
            assert order_two["product_code"] == OFFICE_CODE
            assert order_two["quantity"] == 2
            assert order_two["unit_price"] == 198000
            assert order_two["total"] == 396000
            with closing(sqlite3.connect(db_path)) as connection:
                other_reserved = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM order_inventory_items AS oi
                    JOIN inventory_items AS i ON i.id = oi.inventory_item_id
                    JOIN products AS p ON p.id = i.product_id
                    WHERE oi.order_id = (SELECT id FROM orders WHERE order_id = ?)
                      AND p.code != ?
                    """,
                    ("ORD-OFFICE-CB-2", OFFICE_CODE),
                ).fetchone()[0]
            assert other_reserved == 0
        finally:
            bot._make_order_id = previous_make_order_id
            bot.ORDERS_DB_PATH = previous_orders_path
            bot.BUSINESS_PARTNERS_PATH = previous_business_partners_path
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path


def test_office_import_updates_existing_product_warranty_to_lifetime_without_touching_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        workbook_path = root / "office.xlsx"
        bot._initialize_store_db(db_path)
        now = "2026-08-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, code, name, active, delivery_type, created_at, updated_at,
                     category, account_type, duration, price_vnd, warranty_days, note,
                     category_key, product_group)
                VALUES
                    ('office-product', ?, ?, 1, 'account', ?, ?,
                     'SOFTWARE', 'license_key', 'LIFETIME', 198000, 7, '',
                     'SOFTWARE', 'account')
                """,
                (OFFICE_CODE, OFFICE_NAME, now, now),
            )
            connection.execute(
                """
                INSERT INTO orders
                    (id, order_id, telegram_user_id, username, product_id, product_code,
                     product_name, package_name, quantity, unit_price_vnd, total_vnd,
                     delivery_type, payment_status, order_status, created_at)
                VALUES
                    ('existing-order', 'ORD-OLD-OFFICE', 42, 'old', 'office-product', ?,
                     ?, ?, 1, 198000, 198000, 'account', 'paid', 'delivered', ?)
                """,
                (OFFICE_CODE, OFFICE_NAME, OFFICE_NAME, now),
            )
            connection.commit()

        _write_office_workbook(workbook_path, count=15)
        report = import_inventory(workbook_path, db_path, mode="append")
        assert report["row_errors"] == 0
        assert report["products_updated"] == 1
        assert report["credentials_added"] == 15

        repo = StoreRepository(db_path)
        product = repo.get_product_details(OFFICE_CODE)
        assert product is not None
        assert product["warranty_days"] == 0
        assert product["duration"] == "LIFETIME"
        assert product["price_vnd"] == 198000
        assert repo.get_stock_count(OFFICE_CODE) == 15
        with closing(sqlite3.connect(db_path)) as connection:
            order_count = connection.execute("SELECT COUNT(*) FROM orders WHERE order_id = 'ORD-OLD-OFFICE'").fetchone()[0]
            payment_count = connection.execute("SELECT COUNT(*) FROM payment_transactions").fetchone()[0]
        assert order_count == 1
        assert payment_count == 0


def test_seven_day_products_still_display_7_day_warranty() -> None:
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        bot._initialize_store_db(db_path)
        now = "2026-08-04T00:00:00+00:00"
        products = [
            ("GROK_75K", "SUPERGROK AI", "account", "account", "30D", 75000, 7, "GROK_75K"),
            ("CAPCUT_7D", "CAPCUT PRO 7 ngày", "account", "account", "7D", 8000, 7, "CAPCUT"),
            ("GPT-PLUS-1M-PRIVATE", "ChatGPT Plus", "account", "account", "30D", 160000, 7, "CHATGPT"),
        ]
        with closing(sqlite3.connect(db_path)) as connection:
            for code, name, category, account_type, duration, price, warranty, category_key in products:
                product_id = f"product-{code}"
                connection.execute(
                    """
                    INSERT INTO products
                        (id, code, name, active, delivery_type, created_at, updated_at,
                         category, account_type, duration, price_vnd, warranty_days, note,
                         category_key, product_group)
                    VALUES (?, ?, ?, 1, 'account', ?, ?, ?, ?, ?, ?, ?, '', ?, 'account')
                    """,
                    (product_id, code, name, now, now, category, account_type, duration, price, warranty, category_key),
                )
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                    (f"item-{code}", product_id, f"{code.lower()}@example.com|pass", now),
                )
            connection.commit()

        try:
            os.environ["STORE_DB_PATH"] = str(db_path)
            assert "Bảo hành: 7 ngày" in bot._quantity_text("GROK_75K", "GROK_75K")
            assert "Bảo hành: 7 ngày" in bot._quantity_text("CAPCUT", "CAPCUT_7D")
            assert "Bảo hành: 7 ngày" in bot._quantity_text("CHATGPT", "GPT-PLUS-1M-PRIVATE")
            assert "12 tháng" not in bot._quantity_text("GROK_75K", "GROK_75K")
            assert "12 tháng" not in bot._quantity_text("CAPCUT", "CAPCUT_7D")
            assert "12 tháng" not in bot._quantity_text("CHATGPT", "GPT-PLUS-1M-PRIVATE")
        finally:
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
