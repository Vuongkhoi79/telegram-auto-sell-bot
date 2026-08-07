from __future__ import annotations

import asyncio
import hashlib
import os
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


CAPCUT_CODE = "CAPCUT"


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.replies: list[tuple[str, object | None]] = []
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
        self.effective_user = SimpleNamespace(id=user_id, full_name="CapCut User", username="")
        self.effective_message = SimpleNamespace(
            chat=SimpleNamespace(id=123456),
            text="",
            reply_text=self.callback_query._reply_text,
            reply_photo=self.callback_query._reply_photo,
        )


def _write_capcut_workbook(path: Path) -> list[str]:
    credentials = [
        "capcut-new-1@example.com|new-pass-1",
        "capcut-new-2@example.com|new-pass-2",
    ]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "CAPCUT"
    sheet.append(REQUIRED_COLUMNS)
    for credential in credentials:
        sheet.append(
            [
                CAPCUT_CODE,
                "AI",
                "CAPCUT PRO",
                "personal",
                "365D",
                400000,
                60,
                credential,
                "",
                1,
            ]
        )
    workbook.save(path)
    workbook.close()
    return credentials


def _seed_delivered_capcut_365d(db_path: Path) -> None:
    bot._initialize_store_db(db_path)
    now = "2026-08-07T00:00:00+00:00"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            INSERT INTO products
                (id, code, name, active, delivery_type, created_at, updated_at,
                 category, account_type, duration, price_vnd, warranty_days, note,
                 category_key, product_group)
            VALUES
                ('capcut-product', 'CAPCUT', 'CAPCUT PRO 365 ngay', 1, 'account', ?, ?,
                 'account', 'personal', '365D', 400000, 365, '', 'CAPCUT', 'account')
            """,
            (now, now),
        )
        for index in range(3):
            connection.execute(
                """
                INSERT INTO inventory_items
                    (id, product_id, secret_value, status, created_at, delivered_at)
                VALUES (?, 'capcut-product', ?, 'delivered', ?, ?)
                """,
                (f"old-delivered-{index}", f"capcut-old-{index}@example.com|old-pass", now, now),
            )
        connection.commit()


def _stock_counts(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT i.status, COUNT(*) AS qty
            FROM inventory_items AS i
            JOIN products AS p ON p.id = i.product_id
            WHERE p.code = 'CAPCUT'
            GROUP BY i.status
            """
        ).fetchall()
    return {str(status): int(qty) for status, qty in rows}


def test_capcut_365d_warranty_60_import_and_purchase_flow_uses_only_new_stock() -> None:
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    previous_orders_path = bot.ORDERS_DB_PATH
    previous_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    previous_make_order_id = bot._make_order_id
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        workbook_path = root / "capcut.xlsx"
        _seed_delivered_capcut_365d(db_path)
        credentials = _write_capcut_workbook(workbook_path)
        new_hashes = {hashlib.sha256(value.encode("utf-8")).hexdigest() for value in credentials}

        assert _stock_counts(db_path) == {"delivered": 3}
        report = import_inventory(workbook_path, db_path, mode="append")
        assert report["rows_read"] == 2
        assert report["valid_rows"] == 2
        assert report["row_errors"] == 0
        assert report["credentials_added"] == 2
        assert report["stock"] == {"CAPCUT": 2}
        assert _stock_counts(db_path) == {"available": 2, "delivered": 3}

        repo = StoreRepository(db_path)
        product = repo.get_product_details("CAPCUT")
        assert product is not None
        assert product["name"] == "CAPCUT PRO 12 tháng"
        assert product["account_type"] == "personal"
        assert product["duration"] == "365D"
        assert product["price_vnd"] == 400000
        assert product["warranty_days"] == 60
        assert product["active"] == 1
        assert repo.get_stock_count("CAPCUT_7D") == 0
        assert repo.get_stock_count("CAPCUT_30D") == 0

        try:
            os.environ["STORE_DB_PATH"] = str(db_path)
            bot.ORDERS_DB_PATH = root / "orders.json"
            bot.BUSINESS_PARTNERS_PATH = root / "business_partners.json"
            bot._make_order_id = lambda _product_name: "ORD-CAPCUT-W60-1"
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

            package = bot._get_package_info("CAPCUT", "CAPCUT_365D")
            assert package is not None
            assert package["price_vnd"] == 400000
            assert package["available_count"] == 2
            assert package["display_name"] == "CAPCUT PRO 12 tháng"

            qty_one_order = bot._create_sales_order(
                FakeUpdate("qty:CAPCUT:CAPCUT_365D:1"),
                "CAPCUT",
                "CAPCUT_365D",
                1,
                attach_existing_referral=False,
            )
            assert qty_one_order["unit_price"] == 400000
            assert qty_one_order["total"] == 400000
            StoreRepository(db_path).release_order_reservation(str(qty_one_order["order_id"]))

            bot._make_order_id = lambda _product_name: "ORD-CAPCUT-W60-2"
            qty_update = FakeUpdate("qty:CAPCUT:CAPCUT_365D:2")
            asyncio.run(bot._on_menu_impl(qty_update, context))
            assert context.user_data["dtkd_order_ref"] == {
                "product_code": "CAPCUT",
                "package_code": "CAPCUT_365D",
                "quantity": 2,
            }

            skip_update = FakeUpdate("dtkd_order_ref_skip")
            asyncio.run(bot._on_menu_impl(skip_update, context))
            payment_text, payment_markup = skip_update.callback_query.edits[-1]
            assert "800.000" in payment_text
            pay_button = next(
                button
                for row in payment_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("pay_acb:")
            )
            assert pay_button.callback_data == "pay_acb:ORD-CAPCUT-W60-2"

            order = StoreRepository(db_path).find_order("ORD-CAPCUT-W60-2")
            assert order is not None
            assert order["quantity"] == 2
            assert order["unit_price"] == 400000
            assert order["total"] == 800000

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT p.code, i.secret_value, i.status
                    FROM order_inventory_items AS oi
                    JOIN inventory_items AS i ON i.id = oi.inventory_item_id
                    JOIN products AS p ON p.id = i.product_id
                    WHERE oi.order_id = (SELECT id FROM orders WHERE order_id = ?)
                    ORDER BY oi.created_at, i.id
                    """,
                    ("ORD-CAPCUT-W60-2",),
                ).fetchall()
            assert len(rows) == 2
            for row in rows:
                digest = hashlib.sha256(str(row["secret_value"]).encode("utf-8")).hexdigest()
                assert row["code"] == "CAPCUT"
                assert row["status"] == "reserved"
                assert digest in new_hashes

            qr_update = FakeUpdate("pay_acb:ORD-CAPCUT-W60-2")
            asyncio.run(bot._on_menu_impl(qr_update, context))
            assert qr_update.callback_query.photos
            assert "800.000" in qr_update.callback_query.photos[-1][0]

            StoreRepository(db_path).mark_order_paid("ORD-CAPCUT-W60-2", "TX-CAPCUT-W60-2")

            sent_messages: list[str] = []

            async def send_message(chat_id, text, reply_markup=None):
                sent_messages.append(text)

            context.bot = SimpleNamespace(send_message=send_message)
            fulfillment = asyncio.run(bot.fulfill_order(context, "ORD-CAPCUT-W60-2"))
            assert fulfillment["ok"] is True
            delivery = str(fulfillment["delivery"])
            assert credentials[0] in delivery
            assert credentials[1] in delivery
            assert "capcut-old-" not in delivery
            assert _stock_counts(db_path) == {"delivered": 5}
        finally:
            bot._make_order_id = previous_make_order_id
            bot.ORDERS_DB_PATH = previous_orders_path
            bot.BUSINESS_PARTNERS_PATH = previous_business_partners_path
            if previous_store_db_path is None:
                os.environ.pop("STORE_DB_PATH", None)
            else:
                os.environ["STORE_DB_PATH"] = previous_store_db_path
