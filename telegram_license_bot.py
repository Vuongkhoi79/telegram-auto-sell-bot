from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from telegram.error import BadRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import bank_checker
import sepay_webhook
from license_service import DEFAULT_FREE_DAYS, DEFAULT_PAID_PRICE, LicenseService
from payment_service import PaymentConfig, PaymentService

PROJECT_ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = PROJECT_ROOT / "inventory.json"
ORDERS_DB_PATH = PROJECT_ROOT / "orders_db.json"
PROCESSED_TRANSACTIONS_PATH = PROJECT_ROOT / "processed_transactions.json"
AI_DAILY_PRODUCT_NAME = "AI DAILY VIDEO CREATOR"
DEFAULT_DOWNLOAD_URL = "https://drive.google.com/file/d/1LtCqibeDyg11hmagprhFz6zkwgquwow5/view?usp=sharing"
ORDER_TTL_MINUTES = 5
PRODUCT_PACKAGES = {
    "7D": 99000,
    "30D": 199000,
    "90D": 499000,
}
QUANTITY_OPTIONS = [1, 2, 3, 5, 10]
PRODUCT_ORDER = [
    "ADOBE",
    "ARTLIST",
    "CANVA",
    "CAPCUT PRO",
    "CHATGPT",
    "CLAUDE AI",
    "CURSOR AI",
    "ELEVEN",
    "GAMMA AI",
    "GEMINI AI",
    "GROK SUPER",
    "HEYGEN AI",
    "HIGGFIELD",
    "KLING",
    "KREA AI",
    "OPENART AI",
    "SUNO AI",
    "VEO3 ULTRA",
    "VIEWMAX",
]

logger = logging.getLogger(__name__)
MACHINE_ID_RE = re.compile(r"^[A-Z0-9-]{16,128}$")


def _parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    values = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            continue
    return values


def _machine_arg(args: list[str]) -> str | None:
    if not args:
        return None
    candidate = args[0].strip().upper()
    return candidate if _looks_like_machine_id(candidate) else None


def _looks_like_machine_id(value: str) -> bool:
    value = str(value or "").strip().upper()
    return bool(MACHINE_ID_RE.fullmatch(value))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _format_vnd(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ".")


def _order_suffix(product_name: str) -> str:
    cleaned = "".join(ch for ch in product_name.upper() if ch.isalnum())
    return cleaned[:8] or "ORDER"


def _make_order_id(product_name: str) -> str:
    timestamp = _utc_now().strftime("%Y%m%d%H%M%S")
    return f"ORD-{timestamp}-{_order_suffix(product_name)}"


def _user_label(user) -> str:
    username = f"@{user.username}" if getattr(user, "username", None) else ""
    if username:
        return f"{user.full_name} {username}".strip()
    return user.full_name or str(user.id)


def _format_license_record(record: dict | None) -> str:
    if not record:
        return "Khong tim thay license."
    return "\n".join(
        [
            f"telegram_user_id: {record.get('telegram_user_id', '')}",
            f"username: {record.get('username', '')}",
            f"machine_id: {record.get('machine_id', '')}",
            f"license_type: {record.get('license_type', '')}",
            f"price: {record.get('price', '')}",
            f"order_id: {record.get('order_id', '')}",
            f"payment_status: {record.get('payment_status', '')}",
            f"issued_at: {record.get('issued_at', '')}",
            f"expire_date: {record.get('expire_date', '')}",
            f"license_file: {record.get('license_file', '')}",
            f"created_at: {record.get('created_at', '')}",
        ]
    )


def _is_admin(user_id: int, admin_ids: set[int]) -> bool:
    return int(user_id) in admin_ids


def _is_message_not_modified(exc: BadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def _safe_edit_message_text(query, *args, **kwargs) -> bool:
    try:
        await query.edit_message_text(*args, **kwargs)
        return True
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return False
        raise


async def _safe_edit_message_caption(query, *args, **kwargs) -> bool:
    try:
        await query.edit_message_caption(*args, **kwargs)
        return True
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return False
        raise


async def _safe_edit_message_reply_markup(query, *args, **kwargs) -> bool:
    try:
        await query.edit_message_reply_markup(*args, **kwargs)
        return True
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return False
        raise


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎁 Sản phẩm", callback_data="menu_products"),
                InlineKeyboardButton("🎁 TOOL AI FREE 90 NGÀY", callback_data="menu_ai_daily"),
            ],
            [
                InlineKeyboardButton("📦 Đơn hàng", callback_data="menu_orders"),
                InlineKeyboardButton("💳 Thanh toán", callback_data="menu_payment"),
            ],
            [InlineKeyboardButton("💬 Hỗ trợ", callback_data="menu_support")],
        ]
    )


def _ai_daily_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 TẢI TOOL", url=os.environ.get("DOWNLOAD_URL", DEFAULT_DOWNLOAD_URL)),
                InlineKeyboardButton("🎁 NHẬN LICENSE FREE 90 NGÀY", callback_data="menu_free"),
            ],
            [
                InlineKeyboardButton("📖 HƯỚNG DẪN", callback_data="menu_help"),
                InlineKeyboardButton("🔥 NÂNG CẤP VĨNH VIỄN", callback_data="menu_upgrade"),
            ],
            [InlineKeyboardButton("↩️ Quay lại", callback_data="menu_main")],
        ]
    )


def _upgrade_permanent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Nâng cấp vĩnh viễn", callback_data="menu_upgrade")],
        ]
    )


def _upgrade_permanent_keyboard_for_machine(machine_id: str) -> InlineKeyboardMarkup:
    machine_id = str(machine_id or "").strip().upper()
    if not machine_id:
        return _upgrade_permanent_keyboard()
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Nâng cấp vĩnh viễn", callback_data=f"upgrade_machine:{machine_id}")],
        ]
    )


def _load_inventory() -> dict[str, dict[str, object]]:
    if not INVENTORY_PATH.exists():
        return {}
    try:
        data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, dict[str, object]] = {}
    for name, value in data.items():
        if isinstance(value, dict):
            normalized[str(name).upper()] = value
    return normalized


def _save_inventory(inventory: dict[str, dict[str, object]]) -> None:
    INVENTORY_PATH.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_orders() -> list[dict[str, object]]:
    if not ORDERS_DB_PATH.exists():
        return []
    try:
        data = json.loads(ORDERS_DB_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("orders"), list):
        return [item for item in data["orders"] if isinstance(item, dict)]
    return []


def _save_orders(orders: list[dict[str, object]]) -> None:
    ORDERS_DB_PATH.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_processed_transactions() -> list[dict[str, object]]:
    if not PROCESSED_TRANSACTIONS_PATH.exists():
        return []
    try:
        data = json.loads(PROCESSED_TRANSACTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _find_order(order_id: str) -> dict[str, object] | None:
    for order in reversed(_load_orders()):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            return order
    return None


def _update_order(order_id: str, **changes: object) -> dict[str, object] | None:
    orders = _load_orders()
    for order in reversed(orders):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            order.update(changes)
            _save_orders(orders)
            return order
    return None


def _create_sales_order(update: Update, product_name: str, package_name: str, quantity: int) -> dict[str, object]:
    user = update.effective_user
    unit_price = PRODUCT_PACKAGES[package_name]
    now = _utc_now()
    order = {
        "order_id": _make_order_id(product_name),
        "telegram_user_id": int(user.id) if user else "",
        "username": _user_label(user) if user else "",
        "product_name": product_name,
        "package_name": package_name,
        "quantity": int(quantity),
        "unit_price": int(unit_price),
        "total": int(unit_price) * int(quantity),
        "payment_method": "",
        "payment_status": "pending",
        "order_status": "pending",
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(minutes=ORDER_TTL_MINUTES)).isoformat(),
        "paid_at": "",
        "delivered_at": "",
        "delivery": "",
    }
    orders = _load_orders()
    orders.append(order)
    _save_orders(orders)
    return order


def _is_order_expired(order: dict[str, object]) -> bool:
    expire_at = str(order.get("expire_at", ""))
    try:
        expire_dt = datetime.fromisoformat(expire_at)
    except ValueError:
        return False
    return _utc_now() > expire_dt


def _inventory_status(name: str, item: dict[str, object] | None) -> tuple[str, bool, int]:
    stock = 0
    active = True
    if item:
        try:
            stock = int(item.get("stock", 0))
        except (TypeError, ValueError):
            stock = 0
        active = bool(item.get("active", True))
    available = active and stock > 0
    prefix = "🟢" if available else "🔴"
    return f"{prefix} {name}", available, stock


def _chunked(items: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _product_menu_keyboard() -> InlineKeyboardMarkup:
    inventory = _load_inventory()
    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    for product_name in PRODUCT_ORDER:
        label, _, _ = _inventory_status(product_name, inventory.get(product_name))
        buttons.append(InlineKeyboardButton(label, callback_data=f"product:{product_name}"))
    rows.extend(_chunked(buttons, 4))
    rows.append([InlineKeyboardButton("↩️ Quay lại", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _start_help_text() -> str:
    return "Chọn chức năng bên dưới:"


def _product_list_text() -> str:
    inventory = _load_inventory()
    lines = ["🎁 SẢN PHẨM", ""]
    for product_name in PRODUCT_ORDER:
        label, _, _ = _inventory_status(product_name, inventory.get(product_name))
        lines.append(label)
    return "\n".join(lines)


def _ai_daily_text() -> str:
    return (
        "🎁 AI DAILY VIDEO CREATOR\n\n"
        "Quà tặng miễn phí dành cho thành viên.\n\n"
        "Text To Video\n"
        "Image To Video\n"
        "Grok Workflow\n"
        "Đồng Bộ Nhân Vật\n"
        "Viết Lại Kịch Bản"
    )


def _product_detail_text(product_name: str, available: bool, stock: int) -> str:
    if product_name == AI_DAILY_PRODUCT_NAME:
        return _ai_daily_text()
    status_text = "Còn hàng" if available else "Hết hàng"
    return f"{product_name}\n\nTrạng thái: {status_text}\nTồn kho: {stock}"


def _product_detail_keyboard(product_name: str, available: bool) -> InlineKeyboardMarkup:
    if product_name == AI_DAILY_PRODUCT_NAME:
        return _ai_daily_keyboard()
    if available:
        return _package_keyboard(product_name)
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Quay lại sản phẩm", callback_data="menu_products")]])


def _package_keyboard(product_name: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🎁 Gói {name} - {_format_vnd(price)}đ", callback_data=f"pkg:{product_name}:{name}")]
        for name, price in PRODUCT_PACKAGES.items()
    ]
    rows.append([InlineKeyboardButton("↩️ Quay lại sản phẩm", callback_data="menu_products")])
    rows.append([InlineKeyboardButton("🏠 Quay lại Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _quantity_keyboard(product_name: str, package_name: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(qty), callback_data=f"qty:{product_name}:{package_name}:{qty}")
        for qty in QUANTITY_OPTIONS
    ]
    rows = _chunked(buttons, 3)
    rows.append([InlineKeyboardButton("↩️ Quay lại gói", callback_data=f"product:{product_name}")])
    rows.append([InlineKeyboardButton("🏠 Quay lại Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _payment_choice_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💳 ACB", callback_data=f"pay_acb:{order_id}"),
                InlineKeyboardButton("💰 Trừ ví", callback_data=f"pay_wallet:{order_id}"),
            ],
            [InlineKeyboardButton("↩️ Quay lại sản phẩm", callback_data="menu_products")],
            [InlineKeyboardButton("🏠 Quay lại Menu", callback_data="menu_main")],
        ]
    )


def _qr_payment_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"paid_notify:{order_id}")],
            [InlineKeyboardButton("❌ Hủy giao dịch", callback_data=f"cancel_order:{order_id}")],
        ]
    )


def _payment_info_text(context: ContextTypes.DEFAULT_TYPE, product_name: str = "") -> str:
    payment_service: PaymentService = context.application.bot_data["payment_service"]
    bank_name = payment_service.config.bank_name or "Chua cau hinh"
    bank_account = payment_service.config.bank_account or "Chua cau hinh"
    bank_account_name = payment_service.config.bank_account_name or "Chua cau hinh"
    note = payment_service.build_transfer_note("ORDER", product_name.replace(" ", "")[:8] or "PAY")
    return (
        "💳 THANH TOÁN\n\n"
        f"- Ngân hàng: {bank_name}\n"
        f"- Số tài khoản: {bank_account}\n"
        f"- Chủ tài khoản: {bank_account_name}\n"
        f"- Nội dung chuyển khoản: {note}\n\n"
        "Sau khi chuyển khoản, vui lòng gửi bill cho hỗ trợ để được xác nhận."
    )


def _package_text(product_name: str) -> str:
    return (
        "Chọn gói sản phẩm\n\n"
        f"📦 Sản phẩm: {product_name}\n"
        "Vui lòng chọn gói bên dưới."
    )


def _quantity_text(product_name: str, package_name: str) -> str:
    unit_price = PRODUCT_PACKAGES[package_name]
    return (
        "Chọn số lượng\n\n"
        f"📦 Sản phẩm: {product_name}\n"
        f"🎁 Gói: {package_name}\n"
        f"💵 Đơn giá: {_format_vnd(unit_price)}đ"
    )


def _order_payment_text(order: dict[str, object]) -> str:
    balance = 0
    return (
        "Chọn cách thanh toán\n\n"
        "🧾 Chi tiết đơn\n"
        f"📦 Sản phẩm: {order.get('product_name', '')}\n"
        f"🎁 Gói: {order.get('package_name', '')}\n"
        f"🔢 Số lượng: {order.get('quantity', '')}\n"
        f"💵 Đơn giá: {_format_vnd(int(order.get('unit_price', 0)))}đ\n\n"
        f"💰 Tổng thanh toán: {_format_vnd(int(order.get('total', 0)))}đ\n"
        f"💳 Số dư: {_format_vnd(balance)}đ\n\n"
        "Ví: trừ số dư nếu đủ\n"
        "ACB: chuyển khoản ngân hàng"
    )


def _build_vietqr_url(order: dict[str, object], payment_service: PaymentService) -> str:
    amount = int(order.get("total", 0))
    order_id = str(order.get("order_id", ""))
    account_name = quote(payment_service.config.bank_account_name)
    add_info = quote(order_id)
    return (
        f"https://img.vietqr.io/image/{payment_service.config.bank_name}-{payment_service.config.bank_account}-compact2.png"
        f"?amount={amount}&addInfo={add_info}&accountName={account_name}"
    )


def _qr_caption(order: dict[str, object], payment_service: PaymentService) -> str:
    return (
        "📲 MÃ QR THANH TOÁN\n\n"
        f"💰 Số tiền: {_format_vnd(int(order.get('total', 0)))}đ\n"
        f"🏦 Ngân hàng: {payment_service.config.bank_name}\n"
        f"👤 Tài khoản: {payment_service.config.bank_account_name}\n"
        f"🔢 STK: {payment_service.config.bank_account}\n"
        f"📝 Nội dung CK: {order.get('order_id', '')}\n\n"
        "Mã Order có hiệu lực trong 5 phút.\n"
        "Sau khi chuyển khoản, hệ thống sẽ xác nhận đơn."
    )


async def _send_license_file(update: Update, license_path: str | None) -> None:
    if not license_path:
        logger.error("send_license_file skipped: empty license_path")
        return
    path = Path(license_path)
    if not path.exists():
        logger.error("send_license_file skipped: file not found path=%s", license_path)
        await update.effective_message.reply_text(f"Khong tim thay file license: {license_path}")
        return
    try:
        with open(path, "rb") as handle:
            await update.effective_message.reply_document(document=handle, filename=path.name)
    except Exception:
        logger.exception("send_license_file failed path=%s", path)
        raise


async def _send_products(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    text = _product_list_text()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_product_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_product_menu_keyboard())


async def _send_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, product_name: str, *, edit: bool = False) -> None:
    inventory = _load_inventory()
    item = inventory.get(product_name.upper(), {})
    _, available, stock = _inventory_status(product_name, item)
    if product_name == AI_DAILY_PRODUCT_NAME:
        text = (
            "🎁 AI DAILY VIDEO CREATOR\n\n"
            "Quà tặng miễn phí dành cho thành viên hệ thống.\n\n"
            "Text To Video\n"
            "Image To Video\n"
            "Grok Workflow\n"
            "Đồng Bộ Nhân Vật\n"
            "Viết Lại Kịch Bản"
        )
    else:
        if not available:
            text = "Sản phẩm hiện đã hết hàng, vui lòng quay lại sau."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Quay lại sản phẩm", callback_data="menu_products")]])
            if edit and update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=keyboard)
            else:
                await update.effective_message.reply_text(text, reply_markup=keyboard)
            return
        text = _product_detail_text(product_name, available, stock)
    keyboard = _product_detail_keyboard(product_name, available)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _send_package_choice(update: Update, product_name: str, *, edit: bool = False) -> None:
    text = _package_text(product_name)
    keyboard = _package_keyboard(product_name)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _send_quantity_choice(update: Update, product_name: str, package_name: str, *, edit: bool = False) -> None:
    text = _quantity_text(product_name, package_name)
    keyboard = _quantity_keyboard(product_name, package_name)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _send_payment_choice(update: Update, order: dict[str, object], *, edit: bool = False) -> None:
    text = _order_payment_text(order)
    keyboard = _payment_choice_keyboard(str(order.get("order_id", "")))
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _send_acb_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = _find_order(order_id)
    if not order:
        await update.callback_query.edit_message_text("Không tìm thấy đơn hàng.", reply_markup=_main_menu_keyboard())
        return
    if _is_order_expired(order):
        _update_order(order_id, payment_status="expired", order_status="expired")
        await update.callback_query.edit_message_text("Mã Order đã hết hạn. Vui lòng tạo lại đơn mới.", reply_markup=_product_menu_keyboard())
        return

    payment_service: PaymentService = context.application.bot_data["payment_service"]
    order = _update_order(order_id, payment_method="ACB") or order
    qr_url = _build_vietqr_url(order, payment_service)
    caption = _qr_caption(order, payment_service)
    await update.callback_query.message.reply_photo(photo=qr_url, caption=caption, reply_markup=_qr_payment_keyboard(order_id))


async def _notify_admins_order_pending(context: ContextTypes.DEFAULT_TYPE, order: dict[str, object]) -> None:
    admin_ids: set[int] = context.application.bot_data.get("admin_ids", set())
    if not admin_ids:
        return
    text = (
        "Khách báo đã chuyển khoản.\n\n"
        f"Order ID: {order.get('order_id', '')}\n"
        f"Khách: {order.get('username', '')} ({order.get('telegram_user_id', '')})\n"
        f"Sản phẩm: {order.get('product_name', '')}\n"
        f"Gói: {order.get('package_name', '')}\n"
        f"Số lượng: {order.get('quantity', '')}\n"
        f"Tổng: {_format_vnd(int(order.get('total', 0)))}đ\n\n"
        f"Xác nhận: /paid {order.get('order_id', '')}"
    )
    for admin_id in admin_ids:
        await context.bot.send_message(chat_id=admin_id, text=text)


async def _send_paid_notify(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = _find_order(order_id)
    if not order:
        await update.callback_query.edit_message_text("Không tìm thấy đơn hàng.", reply_markup=_main_menu_keyboard())
        return
    await _notify_admins_order_pending(context, order)
    await update.callback_query.edit_message_caption(
        caption="Bot đã ghi nhận bạn đã chuyển khoản. Admin sẽ xác nhận đơn trong thời gian sớm nhất.",
        reply_markup=_main_menu_keyboard(),
    )


async def _send_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    text = (
        "📦 ĐƠN HÀNG\n\n"
        "Chức năng này dùng để xem và quản lý đơn hàng của bạn.\n"
        "Nếu bạn đã nhận license, hãy kiểm tra lại trong mục Kích hoạt của tool."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_main_menu_keyboard())


async def _send_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False, product_name: str = "") -> None:
    text = _payment_info_text(context, product_name=product_name)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_main_menu_keyboard())


async def _send_download(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    download_url = context.application.bot_data.get("download_url", "")
    text = f"Tai tool:\n{download_url or 'Chua cau hinh DOWNLOAD_URL.'}"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())


async def _send_free_help(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = None
    if update.effective_user:
        user_record = license_service.db.latest_user(update.effective_user.id)
        if user_record:
            machine_id = str(user_record.get("machine_id", "")).strip().upper() or None

    text = (
        "Mở tool -> tab Kích Hoạt -> bấm Nhận License Free 90 Ngày.\n\n"
        "Bot sẽ tự cấp license ngay nếu link từ tool có Machine ID hợp lệ."
    )
    if machine_id:
        text += (
            f"\n\nMachine ID hiện tại:\n{machine_id}\n"
            f"Link nhận license:\nhttps://t.me/Aidaily79_bot?start={quote(machine_id, safe='')}"
        )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())


async def _handle_free_license_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("FREE_LICENSE_CLICKED", flush=True)
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = license_service.recent_machine_id_for_user(user.id) if user else ""

    if not machine_id:
        await update.effective_message.reply_text(
            "Chưa có Machine ID để cấp license.\n\n"
            "Vui lòng mở tool -> tab Kích Hoạt -> bấm Nhận License Free 90 Ngày, "
            "hoặc copy link nhận license free trong tab Kích Hoạt.",
            reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
        )
        return

    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source="free_button", reminder_state="active")
    if license_service.can_grant_free(user.id, machine_id):
        result = license_service.issue_free_license(user.id, _user_label(user), machine_id, customer=_user_label(user))
        if not result.ok:
            await update.effective_message.reply_text(result.message, reply_markup=_ai_daily_keyboard())
            return
        print("LICENSE_CREATED", flush=True)
        license_service.update_user_from_license(result.record or {}, source="free_button")
        record = result.record or {}
        await update.effective_message.reply_text(
            "Bạn được tặng 90 ngày miễn phí.\n"
            f"Machine ID: {machine_id}\n"
            f"Hạn dùng: {record.get('expire_date', '')}\n"
            "Bot đã gửi file license bên dưới.\n"
            "Mở tool tab Kích Hoạt dán license hoặc nạp file license.",
            reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
        )
        await _send_license_file(update, result.license_path or record.get("license_file"))
        print("LICENSE_SENT", flush=True)
        return

    await update.effective_message.reply_text(
        "Machine ID này đã nhận free 90 ngày.",
        reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
    )


async def _send_help(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    text = (
        "Bước 1:\n"
        "Tải tool.\n\n"
        "Bước 2:\n"
        "Mở AI DAILY VIDEO CREATOR.\n\n"
        "Bước 3:\n"
        "Bấm:\n"
        "🎁 NHẬN LICENSE FREE 90 NGÀY\n\n"
        "Bước 4:\n"
        "Bot tự cấp license.\n\n"
        "Bước 5:\n"
        "Kích hoạt và sử dụng."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())


async def _send_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    payment_service: PaymentService = context.application.bot_data["payment_service"]
    machine_id = license_service.recent_machine_id_for_user(user.id) if user else ""

    if not machine_id:
        text = (
            "Chưa có Machine ID để tạo đơn nâng cấp.\n\n"
            "Vui lòng mở tool -> tab Kích Hoạt -> bấm Nhận License Free 90 Ngày trước."
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())
        return

    license_service.touch_user(
        user.id,
        _user_label(user),
        machine_id=machine_id,
        source="upgrade_permanent",
        reminder_state="pending",
    )
    order = license_service.create_pending_order(user.id, _user_label(user), machine_id, customer=_user_label(user))
    text = payment_service.build_payment_text(order.get("price", DEFAULT_PAID_PRICE), order["order_id"], machine_id)

    if payment_service.config.qr_url:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text("Đã tạo đơn nâng cấp vĩnh viễn.")
        await update.effective_message.reply_photo(photo=payment_service.config.qr_url, caption=text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))
        return

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))
    else:
        await update.effective_message.reply_text(text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))


async def _create_upgrade_order(update: Update, context: ContextTypes.DEFAULT_TYPE, machine_id: str, *, edit: bool = False) -> None:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    payment_service: PaymentService = context.application.bot_data["payment_service"]
    machine_id = str(machine_id or "").strip().upper()

    if not machine_id:
        text = (
            "Chưa có Machine ID để tạo đơn nâng cấp.\n\n"
            "Vui lòng gửi Machine ID hoặc mở tool -> tab Kích Hoạt -> bấm Nhận License Free 90 Ngày trước."
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())
        return

    license_service.touch_user(
        user.id,
        _user_label(user),
        machine_id=machine_id,
        source="upgrade_permanent",
        reminder_state="pending",
    )
    order = license_service.create_pending_order(user.id, _user_label(user), machine_id, customer=_user_label(user))
    text = payment_service.build_payment_text(order.get("price", DEFAULT_PAID_PRICE), order["order_id"], machine_id)

    if payment_service.config.qr_url:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text("Đã tạo đơn nâng cấp vĩnh viễn.")
        await update.effective_message.reply_photo(photo=payment_service.config.qr_url, caption=text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))
        return

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))
    else:
        await update.effective_message.reply_text(text, reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id))


async def _send_support(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    support_username = context.application.bot_data.get("support_username", "@Aidaily79")
    text = f"Telegram:\n{support_username}\n\nhoac username ho tro cau hinh trong .env"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_main_menu_keyboard())


async def _handle_machine_id_free_license(update: Update, context: ContextTypes.DEFAULT_TYPE, machine_id: str, *, source: str) -> bool:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]

    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source=source, reminder_state="active")

    if license_service.can_grant_free(user.id, machine_id):
        result = license_service.issue_free_license(user.id, _user_label(user), machine_id, customer=_user_label(user))
        if result.ok:
            license_service.update_user_from_license(result.record or {}, source=f"{source}_free")
            record = result.record or {}
            await update.effective_message.reply_text(
                "Bạn được tặng 90 ngày miễn phí.\n"
                f"Machine ID: {machine_id}\n"
                f"Hạn dùng: {record.get('expire_date', '')}\n"
                "Bot đã gửi file license bên dưới.\n"
                "Mở tool tab Kích Hoạt dán license hoặc nạp file license.",
                reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
            )
            await _send_license_file(update, result.license_path or record.get("license_file"))
            return True
        await update.effective_message.reply_text(result.message, reply_markup=_ai_daily_keyboard())
        return True

    license_service.touch_user(
        user.id,
        _user_label(user),
        machine_id=machine_id,
        source=f"{source}_free_already_used",
        reminder_state="active",
    )
    await update.effective_message.reply_text(
        "Machine ID này đã nhận free 90 ngày.",
        reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
    )
    return True


async def _maybe_issue_deeplink_license(update: Update, context: ContextTypes.DEFAULT_TYPE, machine_id: str) -> bool:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]

    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source="deeplink", reminder_state="active")

    if license_service.can_grant_free(user.id, machine_id):
        result = license_service.issue_free_license(user.id, _user_label(user), machine_id, customer=_user_label(user))
        if result.ok:
            license_service.update_user_from_license(result.record or {}, source="deeplink_free")
            record = result.record or {}
            await update.effective_message.reply_text(
                "Bạn được tặng 90 ngày miễn phí.\n"
                f"Machine ID: {machine_id}\n"
                f"Hạn dùng: {record.get('expire_date', '')}\n"
                "Bot đã gửi file license bên dưới.\n"
                "Mở tool tab Kích Hoạt dán license hoặc nạp file license.",
                reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
            )
            await _send_license_file(update, result.license_path or record.get("license_file"))
            return True
        await update.effective_message.reply_text(result.message, reply_markup=_ai_daily_keyboard())
        return True

    license_service.touch_user(
        user.id,
        _user_label(user),
        machine_id=machine_id,
        source="deeplink_free_already_used",
        reminder_state="active",
    )
    await update.effective_message.reply_text(
        "Machine ID này đã nhận free 90 ngày.",
        reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
    )
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    machine_id = _machine_arg(args)
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source="start", reminder_state="new")

    if machine_id:
        handled = await _handle_machine_id_free_license(update, context, machine_id, source="start")
        if handled:
            return

    await update.effective_message.reply_text(_start_help_text(), reply_markup=_main_menu_keyboard())


async def cmd_license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    machine_id = _machine_arg(args)
    if not machine_id:
        await update.effective_message.reply_text("Dung: /license MACHINE_ID_CUA_BAN")
        return

    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source="license_command", reminder_state="active")
    await _handle_machine_id_free_license(update, context, machine_id, source="license_command")


async def on_text_machine_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip().upper()
    if not _looks_like_machine_id(text):
        return
    logger.info("text_machine_id telegram_user_id=%s machine_id=%s", getattr(update.effective_user, "id", None), text)
    await _handle_machine_id_free_license(update, context, text, source="text_machine")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    machine_id = _machine_arg(args)
    license_service: LicenseService = context.application.bot_data["license_service"]
    license_service.touch_user(
        update.effective_user.id,
        _user_label(update.effective_user),
        machine_id=machine_id,
        source="status",
        reminder_state="checked",
    )
    record = license_service.find(machine_id) if machine_id else license_service.db.latest_license_by_user(update.effective_user.id)

    if not record:
        await update.effective_message.reply_text("Khong tim thay license nao cua ban.", reply_markup=_main_menu_keyboard())
        return
    await update.effective_message.reply_text(_format_license_record(record), reply_markup=_main_menu_keyboard())


async def cmd_grant_free(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Dung: /grant_free <telegram_user_id> <machine_id>")
        return
    telegram_user_id = int(args[0])
    machine_id = args[1].strip().upper()
    license_service: LicenseService = context.application.bot_data["license_service"]
    result = license_service.issue_free_license(telegram_user_id, "admin_grant", machine_id, customer="admin_grant")
    if not result.ok:
        await update.effective_message.reply_text(result.message)
        return
    license_service.update_user_from_license(result.record or {}, source="admin_grant_free")
    await update.effective_message.reply_text(
        "Thanh cong. Da cap free 90 ngay.\n"
        f"Machine ID: {machine_id}\n"
        f"Expire date: {result.record.get('expire_date', '') if result.record else ''}"
    )
    await _send_license_file(update, result.license_path or (result.record or {}).get("license_file"))


async def cmd_grant_permanent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Dung: /grant_permanent <telegram_user_id> <machine_id>")
        return
    telegram_user_id = int(args[0])
    machine_id = args[1].strip().upper()
    license_service: LicenseService = context.application.bot_data["license_service"]
    result = license_service.issue_permanent_license(telegram_user_id, "admin_grant", machine_id, customer="admin_grant")
    if not result.ok:
        await update.effective_message.reply_text(result.message)
        return
    license_service.update_user_from_license(result.record or {}, source="admin_grant_permanent")
    await update.effective_message.reply_text(
        "Thanh cong. Da cap license vinh vien.\n"
        f"Machine ID: {machine_id}\n"
        f"Expire date: {result.record.get('expire_date', '') if result.record else ''}"
    )
    await _send_license_file(update, result.license_path or (result.record or {}).get("license_file"))


def _deliver_sales_order(order: dict[str, object]) -> tuple[bool, str, str]:
    product_name = str(order.get("product_name", "")).upper()
    quantity = int(order.get("quantity", 1))
    inventory = _load_inventory()
    item = inventory.get(product_name)
    if not item:
        return False, "", "Không tìm thấy sản phẩm trong inventory."

    stock = int(item.get("stock", 0))
    if stock < quantity:
        return False, "", f"Tồn kho không đủ. Hiện còn {stock}, đơn cần {quantity}."

    delivery_text = ""
    deliverables = item.get("deliverables")
    if isinstance(deliverables, list) and len(deliverables) >= quantity:
        delivered_items = [str(deliverables.pop(0)) for _ in range(quantity)]
        delivery_text = "\n".join(delivered_items)

    item["stock"] = stock - quantity
    inventory[product_name] = item
    _save_inventory(inventory)
    if delivery_text:
        return True, delivery_text, "Đã trừ tồn kho và giao hàng tự động."
    return True, "", "Đã trừ tồn kho. Chưa có hàng deliverable, admin cần xử lý giao hàng."


async def fulfill_order(context: ContextTypes.DEFAULT_TYPE, order_id: str) -> dict[str, object]:
    sales_order = _find_order(order_id)
    if sales_order:
        delivered, delivery_text, delivery_message = _deliver_sales_order(sales_order)
        now = _utc_now_iso()
        order_status = "delivered" if delivered and delivery_text else "paid"
        updated_order = _update_order(
            order_id,
            payment_status="paid",
            order_status=order_status,
            paid_at=sales_order.get("paid_at") or now,
            delivered_at=now if delivered and delivery_text else "",
            delivery=delivery_text,
        )
        customer_id = sales_order.get("telegram_user_id")
        if customer_id:
            customer_text = (
                "Thanh toán thành công.\n\n"
                f"Order ID: {order_id}\n"
                f"Sản phẩm: {sales_order.get('product_name', '')}\n"
                f"Gói: {sales_order.get('package_name', '')}\n"
                f"Số lượng: {sales_order.get('quantity', '')}"
            )
            if delivery_text:
                customer_text += f"\n\nThông tin nhận hàng:\n{delivery_text}"
            else:
                customer_text += "\n\nAdmin sẽ xử lý giao hàng cho bạn."
            await context.bot.send_message(chat_id=int(customer_id), text=customer_text)
        if not delivery_text:
            await _notify_admins_paid_without_delivery(context, updated_order or sales_order, delivery_message)
        return {
            "ok": delivered,
            "type": "sales_order",
            "delivery": delivery_text,
            "message": delivery_message,
            "order": updated_order or sales_order,
        }

    license_service: LicenseService = context.application.bot_data["license_service"]
    result = license_service.mark_paid(order_id)
    if not result.ok:
        return {"ok": False, "type": "license", "message": result.message}
    if result.record:
        license_service.update_user_from_license(result.record, source="bank_paid")
        customer_id = int(result.record.get("telegram_user_id", 0))
        await context.bot.send_message(
            chat_id=customer_id,
            text=f"Thanh toán thành công.\nBot đã cấp license vĩnh viễn.\nOrder ID: {order_id}",
        )
        path = result.license_path or result.record.get("license_file")
        if path and Path(path).exists():
            with Path(path).open("rb") as handle:
                await context.bot.send_document(chat_id=customer_id, document=InputFile(handle, filename=Path(path).name))
    return {"ok": True, "type": "license", "message": result.message, "record": result.record}


async def _notify_admins_paid_without_delivery(
    context: ContextTypes.DEFAULT_TYPE,
    order: dict[str, object],
    message: str,
) -> None:
    admin_ids: set[int] = context.application.bot_data.get("admin_ids", set())
    if not admin_ids:
        return
    text = (
        "Có đơn đã thanh toán nhưng chưa có hàng giao.\n\n"
        f"Order ID: {order.get('order_id', '')}\n"
        f"Sản phẩm: {order.get('product_name', '')}\n"
        f"Gói: {order.get('package_name', '')}\n"
        f"Số lượng: {order.get('quantity', '')}\n"
        f"Lý do: {message}"
    )
    for admin_id in admin_ids:
        await context.bot.send_message(chat_id=admin_id, text=text)


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Dung: /paid <order_id>")
        return
    order_id = args[0].strip()
    sales_order = _find_order(order_id)
    if sales_order:
        if sales_order.get("payment_status") == "paid":
            await update.effective_message.reply_text("Order đã được thanh toán trước đó.")
            return
        _update_order(order_id, payment_method="ACB", payment_status="paid", paid_at=_utc_now_iso())
        fulfillment = await fulfill_order(context, order_id)
        await update.effective_message.reply_text(
            "Đã xác nhận thanh toán.\n"
            f"Order ID: {order_id}\n"
            f"{fulfillment.get('message', '')}"
        )
        return

    license_service: LicenseService = context.application.bot_data["license_service"]
    result = license_service.mark_paid(order_id)
    if not result.ok:
        await update.effective_message.reply_text(result.message)
        return
    if result.record:
        license_service.update_user_from_license(result.record, source="admin_paid")
    await update.effective_message.reply_text(
        "Thanh toan thanh cong.\nBot da cap license vinh vien cho ban.\n"
        f"Order ID: {order_id}"
    )
    await _send_license_file(update, result.license_path or (result.record or {}).get("license_file"))


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Dung: /revoke <machine_id>")
        return
    machine_id = args[0].strip().upper()
    license_service: LicenseService = context.application.bot_data["license_service"]
    result = license_service.revoke(machine_id)
    if result.record:
        license_service.touch_user(
            int(result.record.get("telegram_user_id", 0)),
            str(result.record.get("username", "")),
            machine_id=machine_id,
            source="revoke",
            reminder_state="revoked",
        )
    await update.effective_message.reply_text(result.message)


async def cmd_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Dung: /cancel_order <order_id>")
        return
    order_id = args[0].strip()
    order = _update_order(order_id, payment_status="cancelled", order_status="cancelled")
    if not order:
        await update.effective_message.reply_text("Không tìm thấy order.")
        return
    await update.effective_message.reply_text(f"Đã hủy order: {order_id}")


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Dung: /order <order_id>")
        return
    order = _find_order(args[0].strip())
    if not order:
        await update.effective_message.reply_text("Không tìm thấy order.")
        return
    await update.effective_message.reply_text(json.dumps(order, ensure_ascii=False, indent=2))


async def cmd_pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    pending = [order for order in _load_orders() if order.get("payment_status") == "pending"]
    if not pending:
        await update.effective_message.reply_text("Không có order pending.")
        return
    lines = []
    for order in pending[-30:]:
        lines.append(
            f"{order.get('order_id', '')} | {order.get('product_name', '')} | "
            f"{order.get('package_name', '')} x{order.get('quantity', '')} | "
            f"{_format_vnd(int(order.get('total', 0)))}đ"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def run_bank_check(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, object]]:
    async def _fulfill(order_id: str) -> dict[str, object]:
        return await fulfill_order(context, order_id)

    return await bank_checker.check_bank_transactions(_fulfill, provider="manual")


async def bank_checker_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.application.bot_data.get("bank_provider", "webhook") != "manual":
        return
    results = await run_bank_check(context)
    if results:
        context.application.bot_data["last_bank_check_results"] = results


async def cmd_check_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    results = await run_bank_check(context)
    if not results:
        await update.effective_message.reply_text("Không có giao dịch mới khớp order pending.")
        return
    lines = ["Đã xử lý giao dịch:"]
    for item in results:
        lines.append(
            f"{item.get('transaction_id', '')} -> {item.get('order_id', '')} | "
            f"{_format_vnd(int(item.get('amount', 0)))}đ | "
            f"{(item.get('fulfillment') or {}).get('message', '')}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    items = _load_processed_transactions()
    if not items:
        await update.effective_message.reply_text("Chưa có giao dịch nào được xử lý.")
        return
    lines = []
    for item in items[-30:]:
        lines.append(
            f"{item.get('transaction_id', '')} | {item.get('order_id', '')} | "
            f"{_format_vnd(int(item.get('amount', 0)))}đ | {item.get('processed_at', '')}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_list_licenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    license_service: LicenseService = context.application.bot_data["license_service"]
    items = license_service.list_licenses()
    if not items:
        await update.effective_message.reply_text("Chua co license nao.")
        return
    lines = []
    for item in items[-30:]:
        lines.append(
            f"{item.get('machine_id', '')} | {item.get('license_type', '')} | {item.get('payment_status', '')} | {item.get('expire_date', '')}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Dung: /find <machine_id>")
        return
    machine_id = args[0].strip().upper()
    license_service: LicenseService = context.application.bot_data["license_service"]
    record = license_service.find(machine_id)
    if not record:
        await update.effective_message.reply_text("Khong tim thay.")
        return
    await update.effective_message.reply_text(json.dumps(record, indent=2, ensure_ascii=False))


async def post_init(application: Application) -> None:
    if application.bot_data.get("bank_provider", "webhook") != "webhook":
        return

    async def _fulfill(order_id: str) -> dict[str, object]:
        context = type("WebhookContext", (), {"application": application, "bot": application.bot})()
        return await fulfill_order(context, order_id)

    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "10000")))
    sepay_webhook.start_sepay_webhook_server(application, _fulfill, host=host, port=port)


async def _on_menu_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if data == "menu_main":
        await query.edit_message_text(_start_help_text(), reply_markup=_main_menu_keyboard())
    elif data == "menu_products":
        await _send_products(update, context, edit=True)
    elif data == "menu_orders":
        await _send_orders(update, context, edit=True)
    elif data == "menu_payment":
        await _send_payment(update, context, edit=True)
    elif data == "menu_ai_daily":
        await query.edit_message_text(_ai_daily_text(), reply_markup=_ai_daily_keyboard())
    elif data == "product_ai_daily":
        await query.edit_message_text(_ai_daily_text(), reply_markup=_ai_daily_keyboard())
    elif data.startswith("pkg:"):
        _, product_name, package_name = data.split(":", 2)
        if package_name not in PRODUCT_PACKAGES:
            await query.edit_message_text("Gói không hợp lệ.", reply_markup=_product_menu_keyboard())
            return
        await _send_quantity_choice(update, product_name, package_name, edit=True)
    elif data.startswith("qty:"):
        _, product_name, package_name, raw_qty = data.split(":", 3)
        if package_name not in PRODUCT_PACKAGES:
            await query.edit_message_text("Gói không hợp lệ.", reply_markup=_product_menu_keyboard())
            return
        order = _create_sales_order(update, product_name, package_name, int(raw_qty))
        await _send_payment_choice(update, order, edit=True)
    elif data.startswith("pay_acb:"):
        order_id = data.split(":", 1)[1].strip()
        await _send_acb_qr(update, context, order_id)
    elif data.startswith("pay_wallet:"):
        order_id = data.split(":", 1)[1].strip()
        await query.edit_message_text(
            "Số dư ví hiện tại không đủ hoặc chưa được cấu hình. Vui lòng chọn ACB để thanh toán.",
            reply_markup=_payment_choice_keyboard(order_id),
        )
    elif data.startswith("paid_notify:"):
        order_id = data.split(":", 1)[1].strip()
        await _send_paid_notify(update, context, order_id)
    elif data.startswith("cancel_order:"):
        order_id = data.split(":", 1)[1].strip()
        _update_order(order_id, payment_status="cancelled", order_status="cancelled")
        if query.message and query.message.caption:
            await query.edit_message_caption(caption="Giao dịch đã hủy.", reply_markup=_main_menu_keyboard())
        else:
            await query.edit_message_text("Giao dịch đã hủy.", reply_markup=_main_menu_keyboard())
    elif data.startswith("product:"):
        product_name = data.split(":", 1)[1].strip()
        await _send_product_detail(update, context, product_name, edit=True)
    elif data.startswith("buy:"):
        product_name = data.split(":", 1)[1].strip()
        await _send_payment(update, context, edit=True, product_name=product_name)
    elif data == "menu_download":
        await _send_download(update, context, edit=True)
    elif data == "menu_free":
        await _handle_free_license_click(update, context)
    elif data == "menu_help":
        await _send_help(update, context, edit=True)
    elif data.startswith("upgrade_machine:"):
        machine_id = data.split(":", 1)[1].strip().upper()
        await _create_upgrade_order(update, context, machine_id, edit=True)
    elif data == "menu_upgrade":
        await _send_upgrade(update, context, edit=True)
    elif data == "menu_support":
        await _send_support(update, context, edit=True)
    else:
        await query.edit_message_text("Menu khong hop le.", reply_markup=_main_menu_keyboard())


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _on_menu_impl(update, context)
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return
        raise


def _load_config() -> dict[str, str]:
    load_dotenv()
    return {
        "BOT_TOKEN": os.environ.get("BOT_TOKEN", "").strip(),
        "ADMIN_IDS": os.environ.get("ADMIN_IDS", "").strip(),
        "PRIVATE_KEY_PATH": os.environ.get("PRIVATE_KEY_PATH", "private_key.pem").strip(),
        "LICENSE_DB_PATH": os.environ.get("LICENSE_DB_PATH", "licenses_db.json").strip(),
        "LICENSE_OUTPUT_DIR": os.environ.get("LICENSE_OUTPUT_DIR", "issued_licenses").strip(),
        "FREE_LICENSE_DAYS": os.environ.get("FREE_LICENSE_DAYS", str(DEFAULT_FREE_DAYS)).strip(),
        "PAID_LICENSE_PRICE": os.environ.get("PAID_LICENSE_PRICE", str(DEFAULT_PAID_PRICE)).strip(),
        "BANK_NAME": os.environ.get("BANK_NAME", "").strip(),
        "BANK_ACCOUNT": os.environ.get("BANK_ACCOUNT", "").strip(),
        "BANK_ACCOUNT_NAME": os.environ.get("BANK_ACCOUNT_NAME", "").strip(),
        "BANK_QR_URL": os.environ.get("BANK_QR_URL", "").strip(),
        "BANK_PROVIDER": os.environ.get("BANK_PROVIDER", "webhook").strip() or "webhook",
        "DOWNLOAD_URL": os.environ.get("DOWNLOAD_URL", "").strip(),
        "SUPPORT_USERNAME": os.environ.get("SUPPORT_USERNAME", "").strip(),
    }


def build_application() -> Application:
    cfg = _load_config()
    if not cfg["BOT_TOKEN"]:
        raise SystemExit("Missing BOT_TOKEN in environment or .env file.")
    admin_ids = _parse_admin_ids(cfg["ADMIN_IDS"])
    license_service = LicenseService(
        private_key_path=Path(cfg["PRIVATE_KEY_PATH"]),
        db_path=Path(cfg["LICENSE_DB_PATH"]),
        output_dir=Path(cfg["LICENSE_OUTPUT_DIR"]),
        free_days=int(cfg["FREE_LICENSE_DAYS"]),
        paid_price=int(cfg["PAID_LICENSE_PRICE"]),
    )
    payment_service = PaymentService(
        PaymentConfig(
            bank_name=cfg["BANK_NAME"],
            bank_account=cfg["BANK_ACCOUNT"],
            bank_account_name=cfg["BANK_ACCOUNT_NAME"],
            qr_url=cfg["BANK_QR_URL"],
        )
    )

    app = Application.builder().token(cfg["BOT_TOKEN"]).post_init(post_init).build()
    app.bot_data["admin_ids"] = admin_ids
    app.bot_data["license_service"] = license_service
    app.bot_data["payment_service"] = payment_service
    app.bot_data["bank_provider"] = cfg["BANK_PROVIDER"]
    app.bot_data["download_url"] = cfg["DOWNLOAD_URL"]
    app.bot_data["support_username"] = cfg["SUPPORT_USERNAME"] or "@Aidaily79"

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("license", cmd_license))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("grant_free", cmd_grant_free))
    app.add_handler(CommandHandler("grant_permanent", cmd_grant_permanent))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("cancel_order", cmd_cancel_order))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("pending_orders", cmd_pending_orders))
    app.add_handler(CommandHandler("check_bank", cmd_check_bank))
    app.add_handler(CommandHandler("transactions", cmd_transactions))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("list_licenses", cmd_list_licenses))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_machine_id))
    app.add_handler(CallbackQueryHandler(on_menu))
    if app.job_queue:
        app.job_queue.run_repeating(bank_checker_job, interval=15, first=5, name="bank_checker")
    return app


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
