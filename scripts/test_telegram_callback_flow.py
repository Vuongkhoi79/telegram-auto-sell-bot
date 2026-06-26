from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_inventory import REQUIRED_COLUMNS, import_inventory
import telegram_license_bot as bot


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[tuple[str, object | None]] = []
        self.captions: list[tuple[str, object | None]] = []
        self.answered = False
        self.message = SimpleNamespace(caption=None)

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.captions.append((caption, reply_markup))


class FakeUpdate:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=user_id, full_name="Test User", username="")
        self.effective_message = SimpleNamespace(
            reply_text=self._reply_text,
            reply_photo=self._reply_photo,
            reply_document=self._reply_document,
        )
        self.replies: list[tuple[str, object | None]] = []
        self.photos: list[tuple[str, object | None]] = []
        self.documents: list[tuple[str, object | None]] = []

    async def _reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))

    async def _reply_photo(self, photo=None, caption=None, reply_markup=None) -> None:
        self.photos.append((caption or "", reply_markup))

    async def _reply_document(self, document=None, filename=None) -> None:
        self.documents.append((filename or "", None))


class TelegramCallbackFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "store.db"
        source_db = PROJECT_ROOT / "database" / "store.db"
        with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(self.db_path)) as target:
            source.backup(target)
        workbook_path = Path(self.temp_dir.name) / "inventory.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "GEMINI"
        sheet.append(REQUIRED_COLUMNS)
        for idx in range(8):
            sheet.append(["GEMINI", "account", "Gemini AI Pro", "account", "30D", 70000, 7, f"gem{idx}@example.com|pass", "", 1])
        workbook.save(workbook_path)
        workbook.close()
        import_inventory(workbook_path, self.db_path, mode="replace")
        self.previous_store_db_path = os.environ.get("STORE_DB_PATH")
        os.environ["STORE_DB_PATH"] = str(self.db_path)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={"store_db_path": self.db_path}), user_data={})

    def tearDown(self) -> None:
        if self.previous_store_db_path is None:
            os.environ.pop("STORE_DB_PATH", None)
        else:
            os.environ["STORE_DB_PATH"] = self.previous_store_db_path
        self.temp_dir.cleanup()

    def test_quantity_flow_uses_canonical_callback_data_and_reserves_one(self) -> None:
        menu_buttons = [button for row in bot._product_menu_keyboard().inline_keyboard for button in row]
        gemini_button = next(button for button in menu_buttons if "GEMINI AI" in button.text)
        self.assertEqual(gemini_button.callback_data, "product:GEMINI")

        update = FakeUpdate("product:GEMINI")
        asyncio.run(bot._on_menu_impl(update, self.context))
        package_markup = update.callback_query.edits[-1][1]
        package_buttons = [button for row in package_markup.inline_keyboard for button in row]
        gemini_package_button = next(button for button in package_buttons if "Gemini AI Pro" in button.text)
        self.assertEqual(gemini_package_button.callback_data, "pkg:GEMINI:GEMINI")

        update = FakeUpdate("pkg:GEMINI:GEMINI")
        asyncio.run(bot._on_menu_impl(update, self.context))
        quantity_markup = update.callback_query.edits[-1][1]
        quantity_buttons = [button for row in quantity_markup.inline_keyboard for button in row]
        one_button = next(button for button in quantity_buttons if button.text == "1")
        self.assertEqual(one_button.callback_data, "qty:GEMINI:GEMINI:1")

        update = FakeUpdate("qty:GEMINI:GEMINI:1")
        asyncio.run(bot._on_menu_impl(update, self.context))
        final_text = update.callback_query.edits[-1][0]
        self.assertIn("Chọn phương thức thanh toán", final_text)
        self.assertNotIn("Sản phẩm hiện đã hết hàng", final_text)
        with closing(sqlite3.connect(self.db_path)) as connection:
            stock = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'available'
                """
            ).fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'reserved'
                """
            ).fetchone()[0]
        self.assertEqual((stock, reserved), (7, 1))

    def test_quantity_flow_rejects_when_quantity_exceeds_stock(self) -> None:
        update = FakeUpdate("qty:GEMINI:GEMINI:9")
        asyncio.run(bot._on_menu_impl(update, self.context))
        final_text = update.callback_query.edits[-1][0]
        self.assertIn("Sản phẩm hiện đã hết hàng", final_text)
        with closing(sqlite3.connect(self.db_path)) as connection:
            stock = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'available'
                """
            ).fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_items AS i
                JOIN products AS p ON p.id = i.product_id
                WHERE UPPER(p.code) = 'GEMINI' AND i.status = 'reserved'
                """
            ).fetchone()[0]
        self.assertEqual((stock, reserved), (8, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
