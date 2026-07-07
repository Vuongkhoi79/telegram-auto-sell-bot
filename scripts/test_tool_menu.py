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
            "application": type("FakeApp", (), {"bot_data": {"license_service": FakeLicenseService(), "admin_ids": {999}}})(),
        },
    )()
    start_message = FakeMessage()
    start_update = type("FakeUpdate", (), {"effective_user": FakeUser(), "effective_message": start_message})()
    await bot.cmd_start(start_update, context)
    _, main_keyboard = start_message.replies[-1]
    assert {"menu_products", "menu_tools", "menu_partners"}.issubset(callback_data(main_keyboard))

    tool_query = FakeQuery("menu_tools")
    tool_update = type("FakeUpdate", (), {"callback_query": tool_query})()
    await bot._on_menu_impl(tool_update, context)
    tool_text, tool_keyboard = tool_query.edits[-1]
    assert tool_text == bot._ai_daily_text()
    assert callback_data(tool_keyboard) == {
        "menu_download", "menu_free", "license_product:TOOL_YEAR_365", "menu_help", "menu_main"
    }

    download_query = FakeQuery("menu_download")
    download_update = type("FakeUpdate", (), {"callback_query": download_query})()
    await bot._on_menu_impl(download_update, context)
    assert "AI Daily Video Creator" in download_query.edits[-1][0]

    context.application.bot_data["tool_download_url"] = "https://example.test/ai-daily.zip"
    configured_download_query = FakeQuery("menu_download")
    configured_download_update = type("FakeUpdate", (), {"callback_query": configured_download_query})()
    await bot._on_menu_impl(configured_download_update, context)
    assert "https://example.test/ai-daily.zip" in configured_download_query.edits[-1][0]

    back_query = FakeQuery("menu_main")
    back_update = type("FakeUpdate", (), {"callback_query": back_query})()
    await bot._on_menu_impl(back_update, context)
    _, restored_main_keyboard = back_query.edits[-1]
    assert {"menu_products", "menu_tools", "menu_partners"}.issubset(callback_data(restored_main_keyboard))

    partner_query = FakeQuery("menu_partners")
    partner_update = type("FakeUpdate", (), {"callback_query": partner_query, "effective_user": FakeUser()})()
    await bot._on_menu_impl(partner_update, context)
    partner_text, partner_keyboard = partner_query.edits[-1]
    assert "Module" in partner_text
    assert {
        "dtkd_register",
        "dtkd_ref_code",
        "dtkd_sales",
        "dtkd_orders",
        "dtkd_income",
        "dtkd_withdraw",
        "dtkd_payments",
        "dtkd_ref_link",
        "dtkd_marketing",
        "dtkd_kpi",
        "dtkd_rank",
    }.issubset(callback_data(partner_keyboard))

    user_help_message = FakeMessage()
    user_help_update = type("FakeUpdate", (), {"effective_user": FakeUser(), "effective_message": user_help_message})()
    await bot.cmd_help(user_help_update, context)
    user_help_text, _ = user_help_message.replies[-1]
    assert "/dtkd" not in user_help_text

    admin_user = type("FakeAdminUser", (), {"id": 999, "username": "admin", "full_name": "Admin"})()
    admin_help_message = FakeMessage()
    admin_help_update = type("FakeUpdate", (), {"effective_user": admin_user, "effective_message": admin_help_message})()
    await bot.cmd_help(admin_help_update, context)
    admin_help_text, _ = admin_help_message.replies[-1]
    assert "Dùng /dtkd" in admin_help_text

    dtkd_message = FakeMessage()
    dtkd_update = type("FakeUpdate", (), {"effective_user": admin_user, "effective_message": dtkd_message})()
    await bot.cmd_dtkd(dtkd_update, context)
    dtkd_text, _ = dtkd_message.replies[-1]
    assert "/dtkd_approve <ma_dtkd>" in dtkd_text
    assert "/dtkd_approve_commission <commission_id_or_order_id>" in dtkd_text
    print("TOOL_MENU_TEST=PASS")


if __name__ == "__main__":
    asyncio.run(main())
