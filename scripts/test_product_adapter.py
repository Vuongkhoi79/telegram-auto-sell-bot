from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self) -> None:
        self.callback_query = None
        self.effective_message = FakeMessage()


class FakeTelegramBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def main() -> None:
    previous_inventory_path = bot.INVENTORY_PATH
    previous_orders_path = bot.ORDERS_DB_PATH
    previous_store_db_path = os.environ.get("STORE_DB_PATH")
    previous_make_order_id = bot._make_order_id
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "store.db"
        inventory_path = root / "inventory.json"
        orders_path = root / "orders_db.json"
        bot._initialize_store_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO products
                        (id, code, name, active, delivery_type, created_at, updated_at, price_vnd, warranty_days, category_key)
                    VALUES (?, ?, ?, 1, 'account', ?, ?, ?, ?, ?)
                    """,
                    ("chatgpt", "GPT-PLUS-1M-PRIVATE", "ChatGPT Plus 1 tháng", now, now, 70000, 7, "CHATGPT"),
                )
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                    ("chatgpt-item", "chatgpt", "test@example.com|pass", now),
                )
                connection.execute(
                    """
                    INSERT INTO products
                        (id, code, name, active, delivery_type, created_at, updated_at)
                    VALUES (?, ?, ?, 0, 'account', ?, ?)
                    """,
                    ("claude-inactive", "CLAUDE-PRO-1M-PRIVATE", "Claude Pro", now, now),
                )
        inventory_path.write_text(json.dumps({"VEO3 ULTRA": {"stock": 2, "active": True}}), encoding="utf-8")
        bot.INVENTORY_PATH = inventory_path
        bot.ORDERS_DB_PATH = orders_path
        os.environ["STORE_DB_PATH"] = str(db_path)

        mapped = bot.get_product_display_info("CHATGPT", store_db_path=db_path)
        assert mapped["source"] == "store.db", mapped
        assert mapped["product_code"] == "GPT-PLUS-1M-PRIVATE", mapped
        assert mapped["available_count"] == 1, mapped
        assert bot.get_available_count("CHATGPT", store_db_path=db_path) == 1
        assert not [key for key in bot.PRODUCT_ORDER if key not in bot.TELEGRAM_PRODUCT_CODE_MAP]

        fallback = bot.get_product_display_info("VEO3 ULTRA", store_db_path=db_path)
        assert fallback["source"] == "inventory.json", fallback
        assert fallback["available_count"] == 2, fallback
        assert bot.get_product_status_for_menu("VEO3 ULTRA", store_db_path=db_path) == ("🟢 VEO3 ULTRA", True, 2)

        menu = bot._product_menu_keyboard()
        menu_callbacks = {
            button.callback_data for row in menu.inline_keyboard for button in row
        }
        assert "product:CHATGPT" in menu_callbacks
        assert "product:VEO3 ULTRA" not in menu_callbacks
        assert "product:CLAUDE AI" not in menu_callbacks
        assert "🟢 CHATGPT" in bot._product_list_text()
        assert "VEO3 ULTRA" not in bot._product_list_text()

        empty_db_path = root / "empty-store.db"
        bot._initialize_store_db(empty_db_path)
        os.environ["STORE_DB_PATH"] = str(empty_db_path)
        empty_menu_callbacks = {
            button.callback_data for row in bot._product_menu_keyboard().inline_keyboard for button in row
        }
        assert "product:VEO3 ULTRA" in empty_menu_callbacks
        os.environ["STORE_DB_PATH"] = str(db_path)

        update = FakeUpdate()
        asyncio.run(bot._send_product_detail(update, None, "CHATGPT"))
        detail_text, detail_keyboard = update.effective_message.replies[0]
        detail_callbacks = {
            button.callback_data for row in detail_keyboard.inline_keyboard for button in row
        }
        assert "pkg:CHATGPT:GPT-PLUS-1M-PRIVATE" in detail_callbacks

        order_ids = iter(["ORD-MAPPED-1", "ORD-MAPPED-2", "ORD-UNMAPPED-1"])
        bot._make_order_id = lambda _product_name: next(order_ids)
        fake_update = type(
            "FakeOrderUpdate",
            (),
            {"effective_user": type("FakeUser", (), {"id": 42, "full_name": "Test User"})()},
        )()
        mapped_order = bot._create_sales_order(fake_update, "CHATGPT", "7D", 1)
        assert mapped_order["order_id"] == "ORD-MAPPED-1"
        assert mapped_order["inventory_source"] == "sqlite"
        assert len(json.loads(orders_path.read_text(encoding="utf-8"))) == 1
        with closing(sqlite3.connect(db_path)) as connection:
            reserved = connection.execute(
                "SELECT status, reserved_order_id FROM inventory_items WHERE id = 'chatgpt-item'"
            ).fetchone()
            assert reserved == ("reserved", "ORD-MAPPED-1"), reserved
            assert connection.execute("SELECT COUNT(*) FROM order_inventory_items").fetchone()[0] == 1
        repository = bot.StoreRepository(db_path)
        repeat = repository.create_pending_account_order_and_reserve(
            order_id="ORD-MAPPED-1",
            telegram_user_id=42,
            username="Test User",
            product_code="GPT-PLUS-1M-PRIVATE",
            product_name="CHATGPT",
            package_name="7D",
            quantity=1,
            unit_price_vnd=99000,
            total_vnd=99000,
            created_at=mapped_order["created_at"],
            expire_at=mapped_order["expire_at"],
        )
        assert repeat == ["chatgpt-item"], repeat
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM order_inventory_items").fetchone()[0] == 1

        # A catalog category with no available account is rendered red.
        with closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute("UPDATE inventory_items SET status = 'disabled' WHERE product_id = 'chatgpt'")
        red_menu_buttons = [
            button.text
            for row in bot._product_menu_keyboard().inline_keyboard
            for button in row
            if button.callback_data == "product:CHATGPT"
        ]
        assert red_menu_buttons == ["🔴 CHATGPT"], red_menu_buttons

        try:
            bot._create_sales_order(fake_update, "CHATGPT", "7D", 1)
        except bot.InventoryReservationError:
            pass
        else:
            raise AssertionError("Mapped order without available inventory must be blocked")
        assert len(json.loads(orders_path.read_text(encoding="utf-8"))) == 1

        with closing(sqlite3.connect(db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO inventory_items (id, product_id, secret_value, status, created_at) VALUES (?, ?, ?, 'available', ?)",
                    ("chatgpt-item-2", "chatgpt", "second@example.com|pass", now),
                )
        bot._update_order("ORD-MAPPED-1", payment_status="paid", transaction_id="SEPAY-MAPPED-1")
        fulfillment_context = type("FakeContext", (), {"bot": FakeTelegramBot()})()
        first_fulfillment = asyncio.run(bot.fulfill_order(fulfillment_context, "ORD-MAPPED-1"))
        assert first_fulfillment["ok"], first_fulfillment
        assert first_fulfillment["delivery"] == "test@example.com|pass", first_fulfillment
        with closing(sqlite3.connect(db_path)) as connection:
            states = dict(connection.execute("SELECT id, status FROM inventory_items").fetchall())
            assert states == {"chatgpt-item": "delivered", "chatgpt-item-2": "available"}, states
        duplicate_fulfillment = asyncio.run(bot.fulfill_order(fulfillment_context, "ORD-MAPPED-1"))
        assert duplicate_fulfillment["ok"], duplicate_fulfillment
        assert duplicate_fulfillment["delivery"] == "test@example.com|pass", duplicate_fulfillment
        with closing(sqlite3.connect(db_path)) as connection:
            states = dict(connection.execute("SELECT id, status FROM inventory_items").fetchall())
            assert states == {"chatgpt-item": "delivered", "chatgpt-item-2": "available"}, states

        unmapped_order = bot._create_sales_order(fake_update, "VEO3 ULTRA", "7D", 1)
        assert unmapped_order["inventory_source"] == "json"
        assert bot._find_order(str(unmapped_order["order_id"])) is not None
        inventory_path.write_text(
            json.dumps({"VEO3 ULTRA": {"stock": 1, "active": True, "deliverables": ["legacy@example.com|pass"]}}),
            encoding="utf-8",
        )
        unmapped_delivered, unmapped_delivery, _ = bot._deliver_sales_order(unmapped_order)
        assert unmapped_delivered
        assert unmapped_delivery == "legacy@example.com|pass"
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM order_inventory_items").fetchone()[0] == 1
    bot.INVENTORY_PATH = previous_inventory_path
    bot.ORDERS_DB_PATH = previous_orders_path
    bot._make_order_id = previous_make_order_id
    if previous_store_db_path is None:
        os.environ.pop("STORE_DB_PATH", None)
    else:
        os.environ["STORE_DB_PATH"] = previous_store_db_path
    print("PRODUCT_ADAPTER_TEST=PASS")


if __name__ == "__main__":
    main()
