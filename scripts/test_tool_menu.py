from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.edits: list[tuple[str, object]] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))


class FakeLicenseDb:
    def latest_user(self, _user_id: int):
        return None


class FakeLicenseService:
    db = FakeLicenseDb()

    def touch_user(self, *_args, **_kwargs) -> None:
        return None


class FakeUser:
    id = 123
    username = "tool-test"
    full_name = "Tool Test"


def callback_data(keyboard) -> set[str]:
    return {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }


async def main() -> None:
    context = type(
        "FakeContext",
        (),
        {
            "args": [],
            "application": type("FakeApp", (), {"bot_data": {"license_service": FakeLicenseService()}})(),
        },
    )()
    start_message = FakeMessage()
    start_update = type("FakeUpdate", (), {"effective_user": FakeUser(), "effective_message": start_message})()
    await bot.cmd_start(start_update, context)
    _, main_keyboard = start_message.replies[-1]
    assert {"menu_products", "menu_tools"}.issubset(callback_data(main_keyboard))

    tool_query = FakeQuery("menu_tools")
    tool_update = type("FakeUpdate", (), {"callback_query": tool_query})()
    await bot._on_menu_impl(tool_update, context)
    tool_text, tool_keyboard = tool_query.edits[-1]
    assert tool_text == bot._ai_daily_text()
    assert {"menu_free", "menu_help", "menu_upgrade", "menu_main"}.issubset(callback_data(tool_keyboard))

    back_query = FakeQuery("menu_main")
    back_update = type("FakeUpdate", (), {"callback_query": back_query})()
    await bot._on_menu_impl(back_update, context)
    _, restored_main_keyboard = back_query.edits[-1]
    assert {"menu_products", "menu_tools"}.issubset(callback_data(restored_main_keyboard))
    print("TOOL_MENU_TEST=PASS")


if __name__ == "__main__":
    asyncio.run(main())
