from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore[override]
        dotenv_path = Path(__file__).resolve().parent / ".env"
        if not dotenv_path.exists():
            return False
        loaded = False
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            loaded = True
        return loaded
from telegram.error import BadRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import bank_checker
import sepay_webhook
from license_service import (
    DEFAULT_FREE_DAYS,
    DEFAULT_PAID_PRICE,
    LIFETIME_PLAN,
    YEAR_365_PLAN,
    LicenseService,
)
from payment_service import PaymentConfig, PaymentService
from repository.store_repository import StoreRepository
from scripts.import_inventory import import_inventory
from scripts.sales_flow_state import log_sales_state

PROJECT_ROOT = Path(__file__).resolve().parent
IMPORTS_DIR = PROJECT_ROOT / "imports"
INVENTORY_PATH = PROJECT_ROOT / "inventory.json"
ORDERS_DB_PATH = PROJECT_ROOT / "orders_db.json"
DEFAULT_ORDERS_DB_PATH = ORDERS_DB_PATH
PROCESSED_TRANSACTIONS_PATH = PROJECT_ROOT / "processed_transactions.json"
AI_DAILY_PRODUCT_NAME = "AI DAILY VIDEO CREATOR"
DEFAULT_DOWNLOAD_URL = "https://drive.google.com/file/d/1LtCqibeDyg11hmagprhFz6zkwgquwow5/view?usp=sharing"
ORDER_TTL_MINUTES = 5
PRODUCT_PACKAGES = {
    "7D": 99000,
    "30D": 199000,
    "90D": 499000,
}
TOOL_LICENSE_PRODUCTS = {
    "TOOL_YEAR_365": {
        "display_name": "💎 Gia hạn 1 năm - 450.000đ",
        "price": 450000,
        "delivery_type": "license",
        "plan": YEAR_365_PLAN,
        "duration_days": 365,
        "lifetime": False,
        "expire_date": "",
    },
    "TOOL_LIFETIME": {
        "display_name": "💎 Vĩnh viễn - 990.000đ",
        "price": 990000,
        "delivery_type": "license",
        "plan": LIFETIME_PLAN,
        "duration_days": None,
        "lifetime": True,
        "expire_date": "2099-12-31",
    },
}
pending_license_product_by_user: dict[int, str] = {}
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
CATALOG_DISPLAY_NAMES = {
    "CAPCUT": "CAPCUT PRO",
    "GEMINI": "GEMINI AI",
}

# UI callback keys stay unchanged. Add aliases here when extending the catalog.
TELEGRAM_PRODUCT_CODE_MAP = {
    "ADOBE": "ADOBE-1M-PRIVATE",
    "ARTLIST": "ARTLIST-1M-PRIVATE",
    "CANVA": "CANVA-PRO-1M-PRIVATE",
    "CANVA PRO": "CANVA-PRO-1M-PRIVATE",
    "CAPCUT": "CAPCUT-PRO-1M-PRIVATE",
    "CAPCUT PRO": "CAPCUT-PRO-1M-PRIVATE",
    "CHATGPT": "GPT-PLUS-1M-PRIVATE",
    "CLAUDE": "CLAUDE-PRO-1M-PRIVATE",
    "CLAUDE AI": "CLAUDE-PRO-1M-PRIVATE",
    "CURSOR": "CURSOR-PRO-1M-PRIVATE",
    "CURSOR AI": "CURSOR-PRO-1M-PRIVATE",
    "ELEVEN": "ELEVENLABS-1M-PRIVATE",
    "ELEVENLABS": "ELEVENLABS-1M-PRIVATE",
    "GAMMA": "GAMMA-1M-PRIVATE",
    "GAMMA AI": "GAMMA-1M-PRIVATE",
    "GEMINI": "GEM-AIPRO-1M-PRIVATE",
    "GEMINI AI": "GEM-AIPRO-1M-PRIVATE",
    "GROK": "GROK-SUPER-1M-PRIVATE",
    "GROK SUPER": "GROK-SUPER-1M-PRIVATE",
    "HEYGEN": "HEYGEN-1M-PRIVATE",
    "HEYGEN AI": "HEYGEN-1M-PRIVATE",
    "HIGGSFIELD": "HIGGSFIELD-1M-PRIVATE",
    "HIGGFIELD": "HIGGSFIELD-1M-PRIVATE",
    "KLING": "KLING-1M-PRIVATE",
    "KREA": "KREA-1M-PRIVATE",
    "KREA AI": "KREA-1M-PRIVATE",
    "OPENART": "OPENART-1M-PRIVATE",
    "OPENART AI": "OPENART-1M-PRIVATE",
    "SUNO": "SUNO-1M-PRIVATE",
    "SUNO AI": "SUNO-1M-PRIVATE",
    "VEO3": "VEO3-1M-PRIVATE",
    "VEO3 ULTRA": "VEO3-1M-PRIVATE",
    "VIEWMAX": "VIEWMAX-1M-PRIVATE",
}
_SQLITE_FALLBACK_LOGGED: set[str] = set()

logger = logging.getLogger(__name__)
MACHINE_ID_RE = re.compile(r"^[A-Z0-9-]{16,128}$")


class InventoryReservationError(RuntimeError):
    pass


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


def _mask_credential_for_log(credential: object) -> str:
    first_field = str(credential or "").split("|", 1)[0].strip()
    if "@" not in first_field:
        return "***"
    local, domain = first_field.split("@", 1)
    if not local or not domain:
        return "***"
    return f"{local[:3]}***@{domain}"


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


def _initialize_store_db(store_db_path: Path) -> None:
    """Create the empty SQLite store schema without importing or replacing data."""
    store_db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store_db_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                delivery_type TEXT NOT NULL CHECK (delivery_type IN ('account', 'license')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                account_type TEXT NOT NULL DEFAULT '',
                duration TEXT NOT NULL DEFAULT '',
                price_vnd INTEGER NOT NULL DEFAULT 0,
                warranty_days INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
                ,menu_order INTEGER NOT NULL DEFAULT 100
                ,show_in_menu INTEGER NOT NULL DEFAULT 1
                ,product_group TEXT NOT NULL DEFAULT 'account'
                ,category_key TEXT NOT NULL DEFAULT ''
                ,description TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS inventory_items (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id),
                secret_value TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'reserved', 'delivered', 'disabled')),
                reserved_order_id TEXT,
                delivered_order_id TEXT,
                created_at TEXT NOT NULL,
                reserved_at TEXT,
                delivered_at TEXT,
                disabled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                product_id TEXT,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                package_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price_vnd INTEGER NOT NULL CHECK (unit_price_vnd >= 0),
                total_vnd INTEGER NOT NULL CHECK (total_vnd >= 0),
                delivery_type TEXT NOT NULL CHECK (delivery_type IN ('account', 'license')),
                machine_id TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT '',
                payment_method TEXT NOT NULL DEFAULT '',
                payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded', 'expired', 'cancelled')),
                order_status TEXT NOT NULL DEFAULT 'pending' CHECK (order_status IN ('pending', 'reserved', 'paid', 'delivered', 'manual_delivery', 'cancelled', 'expired', 'failed')),
                transaction_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expire_at TEXT,
                paid_at TEXT,
                delivered_at TEXT,
                delivery_ref TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS order_inventory_items (
                order_id TEXT NOT NULL REFERENCES orders(id),
                inventory_item_id TEXT NOT NULL UNIQUE REFERENCES inventory_items(id),
                state TEXT NOT NULL CHECK (state IN ('reserved', 'delivered', 'released')),
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                released_at TEXT,
                PRIMARY KEY (order_id, inventory_item_id)
            );

            CREATE TABLE IF NOT EXISTS payment_transactions (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_transaction_id TEXT NOT NULL,
                order_id TEXT REFERENCES orders(id),
                amount_vnd INTEGER NOT NULL CHECK (amount_vnd >= 0),
                description TEXT NOT NULL DEFAULT '',
                raw_payload_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('received', 'matched', 'processed', 'duplicate', 'unmatched', 'failed')),
                received_at TEXT NOT NULL,
                processed_at TEXT,
                UNIQUE (provider, provider_transaction_id)
            );

            CREATE TABLE IF NOT EXISTS inventory_movements (
                id TEXT PRIMARY KEY,
                inventory_item_id TEXT NOT NULL REFERENCES inventory_items(id),
                action TEXT NOT NULL CHECK (action IN ('import', 'reserve', 'deliver', 'release', 'disable')),
                order_id TEXT REFERENCES orders(id),
                admin_telegram_id INTEGER,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_items_product_status ON inventory_items(product_id, status);
            CREATE INDEX IF NOT EXISTS idx_orders_payment_status_expire_at ON orders(payment_status, expire_at);
            CREATE INDEX IF NOT EXISTS idx_orders_telegram_user_id_created_at ON orders(telegram_user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_payment_transactions_order_id ON payment_transactions(order_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_movements_item_created_at ON inventory_movements(inventory_item_id, created_at);
            """
        )
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
        for name, definition in {
            "category": "TEXT NOT NULL DEFAULT ''",
            "account_type": "TEXT NOT NULL DEFAULT ''",
            "duration": "TEXT NOT NULL DEFAULT ''",
            "price_vnd": "INTEGER NOT NULL DEFAULT 0",
            "warranty_days": "INTEGER NOT NULL DEFAULT 0",
            "note": "TEXT NOT NULL DEFAULT ''",
            "menu_order": "INTEGER NOT NULL DEFAULT 100",
            "show_in_menu": "INTEGER NOT NULL DEFAULT 1",
            "product_group": "TEXT NOT NULL DEFAULT 'account'",
            "category_key": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in existing_columns:
                connection.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")


def _format_vnd(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ".")


def _shop_separator() -> str:
    return "\n\n"


def _stock_icon(available_count: int) -> str:
    return "🟢" if int(available_count or 0) > 0 else "🔴"


def _clean_product_title(value: str) -> str:
    return str(value or "").strip().upper()


def _order_suffix(product_name: str) -> str:
    cleaned = "".join(ch for ch in product_name.upper() if ch.isalnum())
    return cleaned[:8] or "ORDER"


def _make_order_id(product_name: str) -> str:
    timestamp = _utc_now().strftime("%Y%m%d%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8].upper()
    return f"ORD-{timestamp}-{_order_suffix(product_name)}-{unique_suffix}"


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


async def _safe_edit_or_send(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None, *, edit: bool = False) -> None:
    query = getattr(update, "callback_query", None)
    if edit and query:
        message = getattr(query, "message", None)
        has_caption_or_media = bool(getattr(message, "caption", None)) or bool(getattr(message, "photo", None))
        if not has_caption_or_media:
            try:
                await query.edit_message_text(text, reply_markup=reply_markup)
                return
            except BadRequest as exc:
                lowered = str(exc).lower()
                if _is_message_not_modified(exc):
                    return
                if "there is no text in the message to edit" not in lowered:
                    raise
                logger.warning("Callback edit_message_text failed on non-text message; sending a new message.")
        elif message is not None:
            logger.warning("Callback message has no text; sending a new message instead of editing.")
        if message and hasattr(message, "reply_text"):
            await message.reply_text(text, reply_markup=reply_markup)
            return
    await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def _show_navigation_screen(update: Update, text: str, reply_markup: InlineKeyboardMarkup, *, edit: bool = False) -> None:
    await _safe_edit_or_send(update, text, reply_markup, edit=edit)


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    logger.warning("MAIN MENU BUTTON: text=🎁 Sản phẩm callback_data=menu_products")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎁 Sản phẩm", callback_data="menu_products"),
                InlineKeyboardButton("🤖 Tool", callback_data="menu_tools"),
            ],
            [
                InlineKeyboardButton("🧾 Lịch sử mua hàng", callback_data="menu_history"),
                InlineKeyboardButton("💰 Nạp tiền", callback_data="menu_payment"),
                InlineKeyboardButton("📦 Đơn hàng", callback_data="menu_orders"),
            ],
            [InlineKeyboardButton("💬 Hỗ trợ", callback_data="menu_support")],
        ]
    )


def _post_delivery_navigation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Sản phẩm", callback_data="menu_products")],
            [InlineKeyboardButton("🧾 Lịch sử mua hàng", callback_data="menu_history")],
            [InlineKeyboardButton("🏠 Menu chính", callback_data="menu_main")],
            [InlineKeyboardButton("💬 Hỗ trợ", callback_data="menu_support")],
        ]
    )


def _ai_daily_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 Tải Tool", callback_data="menu_download")],
            [InlineKeyboardButton("🎁 Dùng thử 10 ngày", callback_data="menu_free")],
            [InlineKeyboardButton("💎 Mua license 365 ngày - 450.000đ", callback_data="license_product:TOOL_YEAR_365")],
            [InlineKeyboardButton("🛡 Hướng dẫn kích hoạt", callback_data="menu_help")],
            [InlineKeyboardButton("Quay lại", callback_data="menu_main")],
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
            [InlineKeyboardButton("💎 Gia hạn 1 năm - 450.000đ", callback_data="license_product:TOOL_YEAR_365")],
            [InlineKeyboardButton("💎 Vĩnh viễn - 990.000đ", callback_data="license_product:TOOL_LIFETIME")],
        ]
    )


def _paid_license_plan_keyboard(machine_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    for product_id, product in TOOL_LICENSE_PRODUCTS.items():
        label = str(product["display_name"])
        rows.append([InlineKeyboardButton(label, callback_data=f"license_product:{product_id}")])
    rows.append([InlineKeyboardButton("Quay lại", callback_data="menu_ai_daily")])
    return InlineKeyboardMarkup(rows)


def _resolve_store_db_path(store_db_path: Path | str | None = None) -> Path:
    if store_db_path is not None:
        path = Path(store_db_path).expanduser()
    else:
        path = Path(os.environ.get("STORE_DB_PATH", "database/store.db")).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _price_source_label(package: dict[str, object] | None) -> str:
    if not package:
        return "unknown"
    if str(package.get("source", "") or "") == "sqlite":
        return "sqlite.products.price_vnd"
    if str(package.get("source", "") or "") == "legacy":
        return "legacy.PRODUCT_PACKAGES"
    return str(package.get("source", "") or "unknown")


def _sqlite_orders_enabled() -> bool:
    return bool(os.environ.get("STORE_DB_PATH")) or ORDERS_DB_PATH == DEFAULT_ORDERS_DB_PATH


def _empty_sqlite_product_display_info(product_key: str, product_code: str = "") -> dict[str, object]:
    normalized_key = product_key.upper()
    return {
        "product_key": normalized_key,
        "product_code": product_code or _menu_stock_product_code(normalized_key) or normalized_key,
        "product_name": _catalog_display_name(normalized_key),
        "price_vnd": 0,
        "warranty_days": 0,
        "active": False,
        "available_count": 0,
        "available": False,
        "source": "store.db",
    }


def _log_sqlite_fallback_once(log_key: str, message: str, *args: object) -> None:
    if log_key in _SQLITE_FALLBACK_LOGGED:
        return
    _SQLITE_FALLBACK_LOGGED.add(log_key)
    logger.warning(message, *args)


def get_product_display_info(
    product_key: str, *, store_db_path: Path | str | None = None
) -> dict[str, object]:
    """Read mapped product metadata from SQLite only."""
    normalized_key = product_key.upper()
    candidate_codes: list[str] = [normalized_key]
    canonical_code = _menu_stock_product_code(normalized_key)
    if canonical_code and canonical_code not in candidate_codes:
        candidate_codes.append(canonical_code)
    mapped_code = TELEGRAM_PRODUCT_CODE_MAP.get(normalized_key, "")
    if mapped_code and mapped_code not in candidate_codes:
        candidate_codes.append(mapped_code)

    path = _resolve_store_db_path(store_db_path)
    if not path.is_file():
        _log_sqlite_fallback_once(
            f"store-missing:{path}",
            "SQLite store missing at %s for %s; treating product as unavailable",
            path,
            normalized_key,
        )
        return _empty_sqlite_product_display_info(normalized_key)
    try:
        repository = StoreRepository(path)
        product = None
        product_code = normalized_key
        for candidate_code in candidate_codes:
            product = repository.get_product_details(candidate_code)
            if product:
                product_code = candidate_code
                break
        if not product:
            _log_sqlite_fallback_once(
                f"product-missing:{normalized_key}",
                "SQLite product %s mapped from %s was not found; treating product as unavailable",
                mapped_code or normalized_key,
                normalized_key,
            )
            return _empty_sqlite_product_display_info(normalized_key, candidate_codes[0] if candidate_codes else "")
        available_count = 0
        for candidate_code in candidate_codes:
            available_count = max(available_count, repository.get_stock_count(candidate_code))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        _log_sqlite_fallback_once(
            f"lookup-error:{normalized_key}",
            "SQLite lookup failed for %s; treating product as unavailable: %s",
            normalized_key,
            exc,
        )
        return _empty_sqlite_product_display_info(normalized_key)

    active = bool(product["active"])
    return {
        "product_key": normalized_key,
        "product_code": product_code,
        "product_name": str(product["name"]),
        "price_vnd": int(product["price_vnd"]),
        "warranty_days": int(product["warranty_days"]),
        "active": active,
        "available_count": available_count,
        "available": active and available_count > 0,
        "source": "store.db",
    }


def get_product_status_for_menu(
    product_key: str, *, store_db_path: Path | str | None = None
) -> tuple[str, bool, int]:
    info = get_product_display_info(product_key, store_db_path=store_db_path)
    display_name = str(info["product_key"])
    available = bool(info["available"])
    count = int(info["available_count"])
    return f"{'🟢' if available else '🔴'} {display_name}", available, count


def get_available_count(product_key: str, *, store_db_path: Path | str | None = None) -> int:
    return int(get_product_display_info(product_key, store_db_path=store_db_path)["available_count"])


def _telegram_product_key_for_sqlite_code(product_code: str) -> str:
    normalized_code = product_code.upper()
    if normalized_code in CATALOG_DISPLAY_NAMES:
        return CATALOG_DISPLAY_NAMES[normalized_code]
    for product_key in PRODUCT_ORDER:
        if TELEGRAM_PRODUCT_CODE_MAP.get(product_key) == normalized_code:
            return product_key
    return normalized_code


def _catalog_display_name(category_key: str) -> str:
    normalized_key = str(category_key or "").strip().upper()
    return CATALOG_DISPLAY_NAMES.get(normalized_key, normalized_key)


def _catalog_lookup_key(product_name: str) -> str:
    normalized_name = str(product_name or "").strip().upper()
    return _menu_stock_product_code(normalized_name) or normalized_name


def _menu_stock_product_code(product_key: str) -> str | None:
    normalized_key = product_key.upper()
    if normalized_key.startswith("CHATGPT"):
        return "CHATGPT"
    if normalized_key.startswith("GEMINI"):
        return "GEMINI"
    if normalized_key.startswith("GROK"):
        return "GROK"
    if normalized_key.startswith("CAPCUT"):
        return "CAPCUT"
    if normalized_key.startswith("VEO3"):
        return "VEO3"
    if normalized_key.startswith("CLAUDE"):
        return "CLAUDE"
    if normalized_key.startswith("ELEVEN"):
        return "ELEVENLABS"
    if normalized_key.startswith("HEYGEN"):
        return "HEYGEN"
    if normalized_key.startswith("SUNO"):
        return "SUNO"
    if normalized_key.startswith("GAMMA"):
        return "GAMMA"
    if normalized_key.startswith("CURSOR"):
        return "CURSOR"
    if normalized_key.startswith("CANVA"):
        return "CANVA"
    if normalized_key.startswith("ADOBE"):
        return "ADOBE"
    if normalized_key.startswith("VIEWMAX"):
        return "VIEWMAX"
    if normalized_key.startswith("ARTLIST"):
        return "ARTLIST"
    if normalized_key.startswith("KREA"):
        return "KREA"
    if normalized_key.startswith("KLING"):
        return "KLING"
    if normalized_key.startswith("HIGG"):
        return "HIGGSFIELD"
    return None


def _reservation_product_code(product_name: str, package_code: str = "") -> str:
    normalized_product_name = str(product_name or "").strip().upper()
    canonical_code = _menu_stock_product_code(normalized_product_name)
    if canonical_code:
        return canonical_code
    normalized_package_code = str(package_code or "").strip().upper()
    if normalized_package_code:
        package_canonical = _menu_stock_product_code(normalized_package_code)
        if package_canonical:
            return package_canonical
        return normalized_package_code
    return normalized_product_name


def _menu_available_count(product_key: str) -> int:
    normalized_key = product_key.upper()
    product_code = _menu_stock_product_code(normalized_key)
    if not product_code:
        return 0
    path = _resolve_store_db_path()
    if not path.is_file():
        return 0
    try:
        repository = StoreRepository(path)
        product = repository.get_product_details(product_code)
        if not product or not product["active"]:
            return 0
        return repository.get_stock_count(product_code)
    except (OSError, RuntimeError, sqlite3.Error):
        return 0


def _load_orders() -> list[dict[str, object]]:
    path = _resolve_store_db_path()
    if _sqlite_orders_enabled() and path.is_file():
        try:
            return StoreRepository(path).list_orders()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order lookup failed; account sales fail closed: %s", exc)
            return []
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
    path = _resolve_store_db_path()
    if _sqlite_orders_enabled() and path.is_file():
        try:
            repository = StoreRepository(path)
            for order in orders:
                repository.upsert_order(order)
            return
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order save failed; account sales fail closed: %s", exc)
            raise
    ORDERS_DB_PATH.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_legacy_orders_json() -> list[dict[str, object]]:
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


def _migrate_legacy_orders_to_sqlite(store_db_path: Path | str | None = None) -> int:
    path = _resolve_store_db_path(store_db_path)
    if not path.is_file() or not ORDERS_DB_PATH.exists():
        return 0
    try:
        repository = StoreRepository(path)
    except (OSError, RuntimeError, sqlite3.Error):
        return 0
    migrated = 0
    for order in _load_legacy_orders_json():
        order_id = str(order.get("order_id", "")).strip()
        if not order_id or repository.order_exists(order_id):
            continue
        try:
            delivery_type = str(order.get("delivery_type", "account") or "account")
            payment_status = str(order.get("payment_status", "pending") or "pending")
            existing_delivery = str(order.get("delivery", "") or "")
            if delivery_type == "account" and not existing_delivery:
                product_name = str(order.get("product_name", "") or order.get("product_id", "")).upper()
                product_code = _reservation_product_code(product_name)
                product = repository.get_product_details(product_code) if product_code else None
                if product and product.get("active"):
                    quantity = int(order.get("quantity", 1) or 1)
                    if quantity > 0:
                        repository.create_pending_account_order_and_reserve(
                            order_id=order_id,
                            telegram_user_id=int(order.get("telegram_user_id", 0) or 0),
                            username=str(order.get("username", "") or ""),
                            product_code=product_code,
                            product_name=str(order.get("product_name", "") or product_name),
                            package_name=str(order.get("package_name", "") or ""),
                            quantity=quantity,
                            unit_price_vnd=int(order.get("unit_price_vnd", order.get("unit_price", 0)) or 0),
                            total_vnd=int(order.get("total_vnd", order.get("total", 0)) or 0),
                            created_at=str(order.get("created_at", _utc_now_iso()) or _utc_now_iso()),
                            expire_at=str(order.get("expire_at", "") or ""),
                        )
                        repository.update_order(order_id, inventory_source="sqlite")
                        if payment_status == "paid" or str(order.get("order_status", "")).lower() == "delivered":
                            transaction_id = str(order.get("transaction_id", "") or f"MIGRATED-{order_id}")
                            repository.mark_order_paid(order_id, transaction_id)
                            delivered_items = repository.deliver_reserved_items(order_id)
                            if delivered_items:
                                repository.update_order(order_id, delivery="\n".join(delivered_items))
                        migrated += 1
                        continue
            if not str(order.get("inventory_source", "") or ""):
                order["inventory_source"] = "sqlite" if delivery_type == "license" else "json"
            repository.upsert_order(order)
            migrated += 1
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            logger.warning("Failed to migrate legacy JSON order %s to SQLite: %s", order_id, exc)
    return migrated


def _load_processed_transactions() -> list[dict[str, object]]:
    if not PROCESSED_TRANSACTIONS_PATH.exists():
        return []
    try:
        data = json.loads(PROCESSED_TRANSACTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _find_order(order_id: str) -> dict[str, object] | None:
    path = _resolve_store_db_path()
    if _sqlite_orders_enabled() and path.is_file():
        try:
            order = StoreRepository(path).find_order(order_id)
            if order:
                return order
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order lookup failed for %s; account sales fail closed: %s", order_id, exc)
            return None
    for order in reversed(_load_orders() if not (_sqlite_orders_enabled() and path.is_file()) else []):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            return order
    return None


def _update_order(order_id: str, **changes: object) -> dict[str, object] | None:
    if "payment_status" in changes and "status" not in changes:
        changes["status"] = changes["payment_status"]
    path = _resolve_store_db_path()
    if _sqlite_orders_enabled() and path.is_file():
        try:
            repository = StoreRepository(path)
            updated = repository.update_order(order_id, **changes)
            if updated:
                return updated
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order update failed for %s; account sales fail closed: %s", order_id, exc)
            return None
    orders = _load_orders()
    for order in reversed(orders):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            order.update(changes)
            _save_orders(orders)
            return order
    return None


def _create_sales_order(update: Update, product_name: str, package_name: str, quantity: int) -> dict[str, object]:
    callback_data = str(getattr(getattr(update, "callback_query", None), "data", "") or "")
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    store_db_path = _resolve_store_db_path()
    logger.debug(
        "ORDER_CREATE_START callback_data=%s user_id=%s database_path=%s product_name=%s package_name=%s quantity=%s",
        callback_data,
        user_id,
        store_db_path,
        product_name,
        package_name,
        quantity,
    )
    user = update.effective_user
    package = _get_package_info(product_name, package_name)
    logger.debug(
        "PACKAGE_RESOLVED callback_data=%s user_id=%s database_path=%s price_source=%s product_name=%s package_name=%s product_id=%s package_id=%s category_key=%s product_code=%s package_code=%s display_name=%s available_before=%s price_vnd=%s",
        callback_data,
        user_id,
        store_db_path,
        _price_source_label(package),
        product_name,
        package_name,
        package.get("product_id") if package else None,
        package.get("product_code") if package else None,
        package.get("category_key") if package else None,
        package.get("product_code") if package else None,
        package.get("package_code") if package else None,
        package.get("display_name") if package else None,
        package.get("available_count") if package else None,
        package.get("price_vnd") if package else None,
    )
    if not package or int(package["available_count"]) < int(quantity):
        raise InventoryReservationError("Sản phẩm hiện đã hết hàng, vui lòng quay lại sau.")
    unit_price = int(package["price_vnd"])
    now = _utc_now()
    selected_package_code = str(package.get("package_code") or package.get("product_code", "") or "").strip().upper()
    reservation_product_code = selected_package_code or _reservation_product_code(product_name, str(package.get("product_code", "")))
    reserve_with_sqlite = False
    reservation_repo: StoreRepository | None = None
    if reservation_product_code:
        try:
            reservation_repo = StoreRepository(_resolve_store_db_path())
            reservation_product = reservation_repo.get_product_details(reservation_product_code)
            reserve_with_sqlite = bool(reservation_product and reservation_product.get("active"))
        except (OSError, RuntimeError, sqlite3.Error):
            reserve_with_sqlite = False
    logger.debug(
        "RESERVE_START callback_data=%s user_id=%s database_path=%s price_source=%s reservation_product_code=%s package_id=%s package_code=%s package_source=%s reserve_with_sqlite=%s display_name=%s available_before=%s requested_quantity=%s unit_price=%s",
        callback_data,
        user_id,
        store_db_path,
        _price_source_label(package),
        reservation_product_code,
        package.get("product_id"),
        package.get("package_code") or package.get("product_code"),
        package.get("source"),
        reserve_with_sqlite,
        package.get("display_name"),
        package.get("available_count"),
        quantity,
        unit_price,
    )
    order = {
        "order_id": _make_order_id(product_name),
        "telegram_user_id": int(user.id) if user else "",
        "username": _user_label(user) if user else "",
        "product_id": reservation_product_code if reserve_with_sqlite else product_name.upper(),
        "product_name": product_name,
        "package_name": str(package["display_name"]),
        "product_code": reservation_product_code,
        "package_code": str(package.get("package_code") or package["product_code"]),
        "quantity": int(quantity),
        "unit_price": int(unit_price),
        "total": int(unit_price) * int(quantity),
        "amount": int(unit_price) * int(quantity),
        "delivery_type": "account",
        "payment_method": "",
        "status": "pending",
        "payment_status": "pending",
        "order_status": "pending",
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(minutes=ORDER_TTL_MINUTES)).isoformat(),
        "paid_at": "",
        "delivered_at": "",
        "delivery": "",
    }
    product_code = reservation_product_code if reserve_with_sqlite else ""
    if product_code:
        try:
            logger.debug("RESERVE_CALL callback_data=%s order_id=%s product_code=%s quantity=%s", callback_data, order["order_id"], product_code, quantity)
            assert reservation_repo is not None
            reservation_repo.create_pending_account_order_and_reserve(
                order_id=str(order["order_id"]),
                telegram_user_id=int(order["telegram_user_id"]),
                username=str(order["username"]),
                product_code=product_code,
                product_name=str(order["product_name"]),
                package_name=str(order["package_name"]),
                quantity=int(order["quantity"]),
                unit_price_vnd=int(order["unit_price"]),
                total_vnd=int(order["total"]),
                created_at=str(order["created_at"]),
                expire_at=str(order["expire_at"]),
            )
            order["product_id"] = reservation_product_code
            order["product_code"] = reservation_product_code
            order["inventory_source"] = "sqlite"
            logger.debug(
                "RESERVE_OK callback_data=%s order_id=%s database_path=%s product_code=%s reserved_after=%s available_before=%s price_vnd=%s",
                callback_data,
                order["order_id"],
                store_db_path,
                product_code,
                order["quantity"],
                package.get("available_count"),
                unit_price,
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            logger.exception(
                "RESERVE_FAIL callback_data=%s order_id=%s product_code=%s quantity=%s",
                callback_data,
                order["order_id"],
                product_code,
                quantity,
            )
            raise InventoryReservationError("Sản phẩm hiện đã hết hàng, vui lòng quay lại sau.") from exc
    else:
        raise InventoryReservationError("Sản phẩm chưa có cấu hình kho SQLite.")
    orders = _load_orders()
    orders.append(order)
    _save_orders(orders)
    logger.debug(
        "ORDER_CREATED callback_data=%s order_id=%s inventory_source=%s product_code=%s package_code=%s amount_vnd=%s",
        callback_data,
        order["order_id"],
        order["inventory_source"],
        order["product_code"],
        order["package_code"],
        order["total"],
    )
    return order


def _create_license_sales_order(update: Update, product_id: str, machine_id: str) -> dict[str, object]:
    if product_id not in TOOL_LICENSE_PRODUCTS:
        raise ValueError(f"Unknown license product: {product_id}")
    user = update.effective_user
    product = TOOL_LICENSE_PRODUCTS[product_id]
    now = _utc_now()
    price = int(product["price"])
    order = {
        "order_id": _make_order_id(product_id),
        "telegram_user_id": int(user.id) if user else "",
        "username": _user_label(user) if user else "",
        "product_id": product_id,
        "product_name": product["display_name"],
        "package_name": product["plan"],
        "quantity": 1,
        "unit_price": price,
        "total": price,
        "amount": price,
        "delivery_type": "license",
        "machine_id": machine_id.strip().upper(),
        "plan": product["plan"],
        "duration_days": product["duration_days"],
        "expire_date": product["expire_date"],
        "lifetime": product["lifetime"],
        "price_vnd": price,
        "payment_method": "",
        "status": "pending",
        "payment_status": "pending",
        "order_status": "pending",
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(minutes=ORDER_TTL_MINUTES)).isoformat(),
        "paid_at": "",
        "delivered_at": "",
        "delivery": "",
        "license_file": "",
        "inventory_source": "license",
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


def _release_expired_sqlite_reservations(store_db_path: Path | str | None = None) -> int:
    path = _resolve_store_db_path(store_db_path)
    if not path.is_file():
        logger.warning("SQLite reservation cleanup skipped; store database is missing: %s", path)
        return 0
    try:
        return StoreRepository(path).release_expired_reservations(5)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.exception("SQLite reservation cleanup failed: %s", exc)
        return 0


def _release_order_reservation(order_id: str, *, store_db_path: Path | str | None = None) -> int:
    path = _resolve_store_db_path(store_db_path)
    if not path.is_file():
        return 0
    try:
        return StoreRepository(path).release_order_reservation(order_id)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        logger.exception("SQLite order reservation release failed for %s: %s", order_id, exc)
        return 0


def _latest_pending_order_id_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    pending_order_id = str((getattr(context, "user_data", {}) or {}).get("pending_order_id", "") or "").strip()
    path = _resolve_store_db_path()
    try:
        orders = StoreRepository(path).list_orders() if path.is_file() else _load_orders()
    except (OSError, RuntimeError, sqlite3.Error):
        orders = _load_orders()
    for order in reversed(orders):
        if int(order.get("telegram_user_id", 0) or 0) != int(user_id):
            continue
        if str(order.get("payment_status", "")).lower() != "pending":
            continue
        if str(order.get("order_status", "")).lower() not in {"pending", "reserved"}:
            continue
        order_id = str(order.get("order_id", "")).strip()
        if order_id:
            return order_id
    return pending_order_id


def _release_current_user_reservation(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    order_id = _latest_pending_order_id_for_user(context, user_id)
    if not order_id:
        return 0
    released = _release_order_reservation(order_id, store_db_path=context.application.bot_data.get("store_db_path"))
    if released and getattr(context, "user_data", None) is not None:
        context.user_data.pop("pending_order_id", None)
    return released


async def sqlite_reservation_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    released = _release_expired_sqlite_reservations(
        context.application.bot_data.get("store_db_path")
    )
    if released:
        logger.info("Released %s expired SQLite inventory reservation(s)", released)


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


def _catalog_category_items(product_group: str = "account") -> list[dict[str, object]]:
    if product_group != "account":
        return []
    path = _resolve_store_db_path()
    if path.is_file():
        try:
            rows = StoreRepository(path).list_menu_product_stock(product_group)
            if rows:
                stock_by_code = {
                    str(row["product_code"]).upper(): int(row["available_count"] or 0)
                    for row in rows
                    if _menu_stock_product_code(str(row["product_code"]))
                }
                catalog_items = []
                seen_codes: set[str] = set()
                for key in PRODUCT_ORDER:
                    lookup_key = _menu_stock_product_code(key) or key
                    catalog_items.append(
                        {
                            "category_key": key,
                            "lookup_key": lookup_key,
                            "available_count": stock_by_code.get(lookup_key, 0),
                        }
                    )
                    seen_codes.add(lookup_key)
                for row in rows:
                    product_code = str(row["product_code"]).upper()
                    lookup_key = _menu_stock_product_code(product_code)
                    if lookup_key and lookup_key not in seen_codes:
                        catalog_items.append(
                            {
                                "category_key": _catalog_display_name(product_code),
                                "lookup_key": lookup_key,
                                "available_count": int(row["available_count"] or 0),
                            }
                        )
                        seen_codes.add(lookup_key)
                if catalog_items:
                    return catalog_items
        except (OSError, RuntimeError, sqlite3.Error):
            pass
    return [
        {"category_key": key, "lookup_key": _menu_stock_product_code(key) or key, "available_count": _menu_available_count(key)}
        for key in PRODUCT_ORDER
    ]


def _product_menu_keyboard(product_group: str = "account") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    menu_counts: dict[str, int] = {}
    for item in _catalog_category_items(product_group):
        product_name = _clean_product_title(str(item["category_key"]))
        available_count = int(item["available_count"] or 0)
        lookup_key = str(item["lookup_key"]).upper()
        menu_counts[lookup_key] = available_count
        label = f"{_stock_icon(available_count)} {product_name} ({available_count})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"product:{lookup_key}"))
    logger.warning(
        "MENU BUILD file=%s function=_product_menu_keyboard product_group=%s CHATGPT=%s GEMINI=%s CAPCUT=%s",
        __file__,
        product_group,
        menu_counts.get("CHATGPT"),
        menu_counts.get("GEMINI"),
        menu_counts.get("CAPCUT"),
    )
    rows.extend(_chunked(buttons, 3))
    rows.append([InlineKeyboardButton("Quay lại", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _start_help_text() -> str:
    items = [
        _clean_product_title(str(item["category_key"]))
        for item in _catalog_category_items()
        if int(item.get("available_count", 0) or 0) > 0
    ]
    if not items:
        items = ["ChatGPT Plus", "Gemini AI Pro", "Grok Super", "Veo3", "CapCut Pro", "Claude", "Cursor"]
    product_lines = "\n".join(f"🟢 {name}" for name in items[:8])
    return (
        "🤖 AI STORE PRO\n"
        "💎 Tài khoản AI chính hãng\n"
        "📦 Giao tự động 24/7\n"
        "🏦 Thanh toán QR\n"
        "🛡 Bảo hành theo từng gói"
        f"{_shop_separator()}"
        "🎁 Hệ thống đang bán\n"
        f"{product_lines}"
        f"{_shop_separator()}"
        "📱 Zalo: 0909968123\n"
        "💬 Telegram: @Aidaily79"
        f"{_shop_separator()}"
        "Chọn chức năng bên dưới"
    )


def _product_list_text() -> str:
    lines = ["🎁 Sản phẩm", ""]
    for item in _catalog_category_items():
        product_name = _clean_product_title(str(item["category_key"]))
        available_count = int(item["available_count"] or 0)
        label = f"{_stock_icon(available_count)} {product_name} ({available_count})"
        lines.append(label)
    return "\n".join(lines)


def _ai_daily_text() -> str:
    return (
        "🤖 AI Daily Video Creator\n\n"
        "Tool hỗ trợ tạo video AI.\n\n"
        "🎁 Dùng thử 10 ngày\n"
        "💎 License trả phí\n"
        "🛡 Hỗ trợ kích hoạt"
    )


def _packages_for_product(product_name: str) -> list[dict[str, object]]:
    try:
        return StoreRepository(_resolve_store_db_path()).list_packages_by_category(_catalog_lookup_key(product_name))
    except (OSError, RuntimeError, sqlite3.Error):
        return []


def _product_detail_text(product_name: str, available: bool, stock: int) -> str:
    if product_name == AI_DAILY_PRODUCT_NAME:
        return _ai_daily_text()
    packages = _packages_for_product(product_name)
    prices = [int(package["price_vnd"]) for package in packages if int(package["price_vnd"] or 0) > 0]
    warranty_days = 0
    product = None
    try:
        product = StoreRepository(_resolve_store_db_path()).get_product_details(_catalog_lookup_key(product_name))
    except (OSError, RuntimeError, sqlite3.Error):
        product = None
    if product:
        warranty_days = int(product.get("warranty_days", 0) or 0)
    status_text = "Còn hàng" if available else "Hết hàng"
    price_text = f"{_format_vnd(min(prices))}đ" if prices else "Liên hệ"
    warranty_text = f"{warranty_days} ngày" if warranty_days else "Theo từng gói"
    display_name = _catalog_display_name(product_name)
    return (
        f"💎 {_clean_product_title(display_name)}"
        f"{_shop_separator()}"
        f"{_stock_icon(stock)} {status_text}\n"
        f"📦 Tồn kho: {stock}\n"
        f"💰 Giá từ: {price_text}\n"
        f"🛡 Bảo hành: {warranty_text}"
        f"{_shop_separator()}"
        "Chọn gói"
    )


def _product_detail_keyboard(product_name: str, available: bool) -> InlineKeyboardMarkup:
    if product_name == AI_DAILY_PRODUCT_NAME:
        return _ai_daily_keyboard()
    if available:
        return _package_keyboard(product_name)
    return InlineKeyboardMarkup([[InlineKeyboardButton("Quay lại sản phẩm", callback_data="menu_products")]])


def _package_keyboard(product_name: str) -> InlineKeyboardMarkup:
    rows = []
    packages = _packages_for_product(product_name)
    product_code = _catalog_lookup_key(product_name)
    if packages:
        rows.extend(
            [InlineKeyboardButton(
                f"🎁 {package['display_name']}\n💰 {_format_vnd(int(package['price_vnd']))}đ\n📦 Còn: {int(package['available_count'] or 0)}",
                callback_data=f"pkg:{product_code}:{str(package.get('package_code') or package['product_code']).upper()}",
            )]
            for package in packages
        )
    rows.append([InlineKeyboardButton("Quay lại sản phẩm", callback_data="menu_products")])
    rows.append([InlineKeyboardButton("Menu chính", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _quantity_keyboard(product_name: str, package_name: str) -> InlineKeyboardMarkup:
    product_code = _catalog_lookup_key(product_name)
    package_code = str(package_name or "").strip().upper()
    buttons = [
        InlineKeyboardButton(str(qty), callback_data=f"qty:{product_code}:{package_code}:{qty}")
        for qty in QUANTITY_OPTIONS
    ]
    rows = _chunked(buttons, 3)
    rows.append([InlineKeyboardButton("Nhập số khác", callback_data=f"manualqty:{product_code}:{package_code}")])
    rows.append([InlineKeyboardButton("Quay lại gói", callback_data=f"product:{product_code}")])
    rows.append([InlineKeyboardButton("Menu chính", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _payment_choice_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏦 Chuyển khoản QR", callback_data=f"pay_acb:{order_id}"),
                InlineKeyboardButton("💰 Ví", callback_data=f"pay_wallet:{order_id}"),
            ],
            [InlineKeyboardButton("Quay lại sản phẩm", callback_data="menu_products")],
            [InlineKeyboardButton("Menu chính", callback_data="menu_main")],
        ]
    )


def _qr_payment_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Đã chuyển khoản", callback_data=f"paid_notify:{order_id}")],
            [InlineKeyboardButton("Hủy giao dịch", callback_data=f"cancel_order:{order_id}")],
        ]
    )


def _payment_info_text(context: ContextTypes.DEFAULT_TYPE, product_name: str = "") -> str:
    payment_service: PaymentService = context.application.bot_data["payment_service"]
    bank_name = payment_service.config.bank_name or "Chua cau hinh"
    bank_account = payment_service.config.bank_account or "Chua cau hinh"
    bank_account_name = payment_service.config.bank_account_name or "Chua cau hinh"
    note = payment_service.build_transfer_note("ORDER", product_name.replace(" ", "")[:8] or "PAY")
    return (
        "🏦 Thanh toán"
        f"{_shop_separator()}"
        f"Ngân hàng: {bank_name}\n"
        f"Số tài khoản: {bank_account}\n"
        f"Chủ tài khoản: {bank_account_name}\n"
        f"Nội dung: {note}"
        f"{_shop_separator()}"
        "Sau khi chuyển khoản, hệ thống sẽ xác nhận đơn."
    )


def _package_text(product_name: str) -> str:
    stock = _menu_available_count(product_name)
    return _product_detail_text(product_name, stock > 0, stock)


def _quantity_text(product_name: str, package_name: str) -> str:
    package = _get_package_info(product_name, package_name)
    unit_price = int(package["price_vnd"]) if package else 0
    available_count = int(package["available_count"]) if package else 0
    display_name = str(package["display_name"]) if package else package_name
    warranty_days = 0
    if package:
        try:
            product = StoreRepository(_resolve_store_db_path()).get_product_details(str(package["product_code"]))
            warranty_days = int(product.get("warranty_days", 0) or 0) if product else 0
        except (OSError, RuntimeError, sqlite3.Error):
            warranty_days = 0
    warranty_text = f"{warranty_days} ngày" if warranty_days else "Theo từng gói"
    return (
        f"💎 {_clean_product_title(display_name)}"
        f"{_shop_separator()}"
        f"💰 Giá: {_format_vnd(unit_price)}đ\n"
        f"📦 Kho: {available_count}\n"
        f"🛡 Bảo hành: {warranty_text}\n"
        "Giao ngay sau thanh toán"
        f"{_shop_separator()}"
        "Chọn số lượng"
    )


def _order_payment_text(order: dict[str, object]) -> str:
    balance = 0
    return (
        "🧾 Đơn hàng"
        f"{_shop_separator()}"
        "📦 Sản phẩm\n"
        f"{order.get('package_name', '')}"
        f"{_shop_separator()}"
        "💰 Đơn giá\n"
        f"{_format_vnd(int(order.get('unit_price', 0)))}đ"
        f"{_shop_separator()}"
        "📦 Số lượng\n"
        f"{order.get('quantity', '')}"
        f"{_shop_separator()}"
        "💰 Thành tiền\n"
        f"{_format_vnd(int(order.get('total', 0)))}đ"
        f"{_shop_separator()}"
        f"💰 Số dư\n{_format_vnd(balance)}đ"
        f"{_shop_separator()}"
        "Chọn phương thức thanh toán"
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
        "🏦 Chuyển khoản QR\n\n"
        f"💰 Số tiền: {_format_vnd(int(order.get('total', 0)))}đ\n"
        f"🏦 Ngân hàng: {payment_service.config.bank_name}\n"
        f"Tài khoản: {payment_service.config.bank_account_name}\n"
        f"STK: {payment_service.config.bank_account}\n"
        f"Nội dung: {order.get('order_id', '')}\n\n"
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


async def _render_product_menu(update: Update, *, edit: bool = False, product_group: str = "account") -> None:
    text = "🎁 Sản phẩm\n\nChọn sản phẩm" if product_group == "account" else "🤖 Tool\n\nChọn sản phẩm"
    await _show_navigation_screen(update, text, _product_menu_keyboard(product_group), edit=edit)


async def _send_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, product_name: str, *, edit: bool = False) -> None:
    if update.effective_user:
        _release_current_user_reservation(context, update.effective_user.id)
    _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
    available = True
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
        try:
            has_packages = bool(StoreRepository(_resolve_store_db_path()).list_packages_by_category(_catalog_lookup_key(product_name)))
        except (OSError, RuntimeError, sqlite3.Error):
            has_packages = False
        if has_packages:
            text = _package_text(product_name)
            keyboard = _package_keyboard(product_name)
            await _safe_edit_or_send(update, text, keyboard, edit=edit)
            return
        product_info = get_product_display_info(product_name)
        available = bool(product_info["available"])
        stock = int(product_info["available_count"])
        if not available:
            text = "Sản phẩm hiện đã hết hàng, vui lòng quay lại sau."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Quay lại sản phẩm", callback_data="menu_products")]])
            await _safe_edit_or_send(update, text, keyboard, edit=edit)
            return
        text = _product_detail_text(product_name, available, stock)
    logger.debug(
        "SALES_STAGE stage=product_detail user_id=%s callback_data=%s product_code=%s available_by_menu_query=%s available_by_package_page_query=%s",
        getattr(getattr(update, "effective_user", None), "id", None),
        getattr(getattr(update, "callback_query", None), "data", "") if getattr(update, "callback_query", None) else "",
        _catalog_lookup_key(product_name),
        _menu_available_count(product_name),
        int(stock if "stock" in locals() else 0),
    )
    keyboard = _product_detail_keyboard(product_name, available)
    await _safe_edit_or_send(update, text, keyboard, edit=edit)


async def _send_package_choice(update: Update, product_name: str, *, edit: bool = False) -> None:
    text = _package_text(product_name)
    keyboard = _package_keyboard(product_name)
    logger.debug(
        "SALES_STAGE stage=package_page user_id=%s callback_data=%s product_code=%s available_by_menu_query=%s",
        getattr(getattr(update, "effective_user", None), "id", None),
        getattr(getattr(update, "callback_query", None), "data", "") if getattr(update, "callback_query", None) else "",
        _catalog_lookup_key(product_name),
        _menu_available_count(product_name),
    )
    await _safe_edit_or_send(update, text, keyboard, edit=edit)


async def _send_quantity_choice(update: Update, product_name: str, package_name: str, *, edit: bool = False) -> None:
    text = _quantity_text(product_name, package_name)
    keyboard = _quantity_keyboard(product_name, package_name)
    package = _get_package_info(product_name, package_name)
    logger.debug(
        "SALES_STAGE stage=quantity_page user_id=%s callback_data=%s product_code=%s package_code=%s package_name=%s available_by_package_page_query=%s",
        getattr(getattr(update, "effective_user", None), "id", None),
        getattr(getattr(update, "callback_query", None), "data", "") if getattr(update, "callback_query", None) else "",
        _catalog_lookup_key(product_name),
        str(package.get("product_code")) if package else "",
        str(package.get("display_name")) if package else package_name,
        int(package.get("available_count") or 0) if package else 0,
    )
    await _safe_edit_or_send(update, text, keyboard, edit=edit)


async def _send_payment_choice(update: Update, order: dict[str, object], *, edit: bool = False) -> None:
    callback_data = str(getattr(getattr(update, "callback_query", None), "data", "") or "")
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(getattr(update, "effective_message", None), "chat", None), "id", None)
    message_id = getattr(getattr(update, "callback_query", None), "message", None)
    message_id = getattr(message_id, "message_id", None)
    logger.debug(
        "PAYMENT_BUILD_START callback_data=%s user_id=%s chat_id=%s message_id=%s order_id=%s product_code=%s package_code=%s quantity=%s amount_vnd=%s bank_account=%s",
        callback_data,
        user_id,
        chat_id,
        message_id,
        str(order.get("order_id", "")),
        str(order.get("product_code", order.get("product_id", "")) or ""),
        str(order.get("package_code", "")),
        int(order.get("quantity", 0) or 0),
        int(order.get("total", 0) or 0),
        order.get("bank_account", ""),
    )
    text = _order_payment_text(order)
    keyboard = _payment_choice_keyboard(str(order.get("order_id", "")))
    logger.debug(
        "PAYMENT_BUILD_OK callback_data=%s order_id=%s amount_vnd=%s",
        callback_data,
        str(order.get("order_id", "")),
        int(order.get("total", 0) or 0),
    )
    logger.debug(
        "TELEGRAM_SEND_START callback_data=%s order_id=%s edit=%s",
        callback_data,
        str(order.get("order_id", "")),
        edit,
    )
    try:
        if edit and update.callback_query:
            await _safe_edit_or_send(update, text, keyboard, edit=True)
        else:
            await update.effective_message.reply_text(text, reply_markup=keyboard)
        logger.debug(
            "TELEGRAM_SEND_OK callback_data=%s order_id=%s branch=payment_choice",
            callback_data,
            str(order.get("order_id", "")),
        )
    except Exception as exc:
        logger.exception(
            "TELEGRAM_SEND_FAIL callback_data=%s order_id=%s exception_type=%s exception_message=%s",
            callback_data,
            str(order.get("order_id", "")),
            type(exc).__name__,
            exc,
        )
        raise


async def _send_purchase_error(update: Update, context: ContextTypes.DEFAULT_TYPE, *, reason: str, order_id: str = "") -> None:
    query = update.callback_query
    message = query.message if query and getattr(query, "message", None) else update.effective_message
    logger.error(
        "FALLBACK_MENU_TRIGGERED user_id=%s chat_id=%s message_id=%s order_id=%s reason=%s",
        getattr(getattr(update, "effective_user", None), "id", None),
        getattr(getattr(message, "chat", None), "id", None),
        getattr(message, "message_id", None),
        order_id,
        reason,
    )
    text = "Đã giữ hàng nhưng lỗi tạo thanh toán. Vui lòng liên hệ admin."
    if query:
        await _safe_edit_or_send(update, text, _main_menu_keyboard(), edit=True)
    else:
        await message.reply_text(text, reply_markup=_main_menu_keyboard())


async def _send_invalid_callback(update: Update) -> None:
    await _safe_edit_or_send(
        update,
        "Menu không hợp lệ. Vui lòng quay lại menu chính.",
        _main_menu_keyboard(),
        edit=True,
    )


async def _send_acb_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    callback_data = str(getattr(getattr(update, "callback_query", None), "data", "") or "")
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(getattr(update, "callback_query", None), "message", None), "chat_id", None)
    message_id = getattr(getattr(update, "callback_query", None), "message", None)
    message_id = getattr(message_id, "message_id", None)
    logger.debug(
        "QR_RENDER_START callback_data=%s user_id=%s chat_id=%s message_id=%s database_path=%s order_id=%s",
        callback_data,
        user_id,
        chat_id,
        message_id,
        _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
        order_id,
    )
    _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
    order = _find_order(order_id)
    query = update.callback_query
    message = query.message if query and getattr(query, "message", None) else update.effective_message
    if not order:
        if query:
            await _safe_edit_or_send(update, "Không tìm thấy đơn hàng.", _main_menu_keyboard(), edit=True)
        else:
            await message.reply_text("Không tìm thấy đơn hàng.", reply_markup=_main_menu_keyboard())
        return
    if _is_order_expired(order):
        _update_order(order_id, payment_status="expired", order_status="expired")
        _release_expired_sqlite_reservations()
        if query:
            await _safe_edit_or_send(update, "Mã Order đã hết hạn. Vui lòng tạo lại đơn mới.", _product_menu_keyboard(), edit=True)
        else:
            await message.reply_text("Mã Order đã hết hạn. Vui lòng tạo lại đơn mới.", reply_markup=_product_menu_keyboard())
        return

    payment_service: PaymentService = context.application.bot_data["payment_service"]
    if not payment_service.config.bank_name or not payment_service.config.bank_account or not payment_service.config.bank_account_name:
        logger.error(
            "QR_RENDER_FAIL callback_data=%s order_id=%s exception_type=%s exception_message=%s bank_name=%s bank_account=%s bank_account_name=%s",
            callback_data,
            order_id,
            "RuntimeError",
            "Payment configuration missing",
            payment_service.config.bank_name,
            payment_service.config.bank_account,
            payment_service.config.bank_account_name,
        )
        raise RuntimeError("Thanh toán chưa được cấu hình.")

    logger.debug(
        "QR_RENDER_OK callback_data=%s user_id=%s database_path=%s order_id=%s product_code=%s package_code=%s quantity=%s order_status=%s bank_account=%s amount_vnd=%s",
        callback_data,
        user_id,
        _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
        order_id,
        str(order.get("product_code", order.get("product_id", "")) or ""),
        str(order.get("package_code", "")),
        int(order.get("quantity", 0) or 0),
        str(order.get("order_status", "")),
        payment_service.config.bank_account,
        int(order.get("total", 0) or 0),
    )
    order = _update_order(order_id, payment_method="ACB") or order
    qr_url = _build_vietqr_url(order, payment_service)
    caption = _qr_caption(order, payment_service)
    logger.debug(
        "TELEGRAM_SEND_START callback_data=%s order_id=%s database_path=%s qr_url_length=%s amount_vnd=%s",
        callback_data,
        order_id,
        _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
        len(qr_url),
        int(order.get("total", 0) or 0),
    )
    try:
        await message.reply_photo(photo=qr_url, caption=caption, reply_markup=_qr_payment_keyboard(order_id))
        logger.debug(
            "TELEGRAM_SEND_OK callback_data=%s order_id=%s branch=qr",
            callback_data,
            order_id,
        )
    except Exception as exc:
        logger.exception(
            "TELEGRAM_SEND_FAIL callback_data=%s order_id=%s exception_type=%s exception_message=%s",
            callback_data,
            order_id,
            type(exc).__name__,
            exc,
        )
        raise


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
    _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
    order = _find_order(order_id)
    if not order:
        await _safe_edit_or_send(update, "Không tìm thấy đơn hàng.", _main_menu_keyboard(), edit=True)
        return
    await _notify_admins_order_pending(context, order)
    await _safe_edit_or_send(
        update,
        (
            "🏦 Đã ghi nhận chuyển khoản\n\n"
            "Hệ thống đang kiểm tra thanh toán.\n"
            "Tài khoản sẽ được chuẩn bị ngay sau khi xác nhận."
        ),
        _main_menu_keyboard(),
        edit=True,
    )


async def _send_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    text = (
        "📦 Đơn hàng\n\n"
        "Theo dõi đơn đã tạo và trạng thái giao hàng.\n"
        "Nếu cần hỗ trợ, gửi mã đơn cho admin."
    )
    await _show_navigation_screen(update, text, _main_menu_keyboard(), edit=edit)


def _format_history_order(order: dict[str, object], index: int) -> str:
    def _field(name: str) -> str:
        value = str(order.get(name, "") or "").strip()
        return value if value else "N/A"

    return (
        f"{index}. {_field('order_id')}\n"
        f"Sản phẩm: {_field('product_name')}\n"
        f"Mã sản phẩm: {_field('product_code')}\n"
        f"Gói: {_field('package_name')}\n"
        f"Số lượng: {_field('quantity')}\n"
        f"Tổng tiền: {_format_vnd(int(order.get('total', order.get('amount', 0)) or 0))}đ\n"
        f"Thanh toán: {_field('payment_status')}\n"
        f"Trạng thái: {_field('order_status')}\n"
        f"Ngày tạo: {_field('created_at')}\n"
        f"Ngày thanh toán: {_field('paid_at')}\n"
        f"Ngày giao hàng: {_field('delivered_at')}"
    )


async def _send_purchase_history(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    user = getattr(update, "effective_user", None)
    telegram_user_id = int(getattr(user, "id", 0) or 0)
    orders: list[dict[str, object]] = []
    store_db_path = _resolve_store_db_path(context.application.bot_data.get("store_db_path"))
    if store_db_path.is_file():
        try:
            orders = StoreRepository(store_db_path).get_orders_by_telegram_user(telegram_user_id, limit=10)
        except (OSError, RuntimeError, sqlite3.Error):
            orders = []
    if not orders:
        text = "🧾 Lịch sử mua hàng\n\nBạn chưa có đơn hàng nào."
    else:
        text = "🧾 Lịch sử mua hàng\n\n" + "\n\n".join(
            _format_history_order(order, index)
            for index, order in enumerate(orders, start=1)
        )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Sản phẩm", callback_data="menu_products"),
                InlineKeyboardButton("🏠 Menu chính", callback_data="menu_main"),
            ]
        ]
    )
    await _show_navigation_screen(update, text, keyboard, edit=edit)


async def _send_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False, product_name: str = "") -> None:
    text = _payment_info_text(context, product_name=product_name)
    await _show_navigation_screen(update, text, _main_menu_keyboard(), edit=edit)


async def _send_download(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    download_url = context.application.bot_data.get("tool_download_url", "")
    text = (
        f"🤖 AI Daily Video Creator\n\nLink tải:\n{download_url}"
        if download_url
        else "🤖 AI Daily Video Creator\n\nAdmin chưa cấu hình link tải. Vui lòng liên hệ hỗ trợ."
    )
    await _safe_edit_or_send(update, text, _ai_daily_keyboard(), edit=edit)


async def _send_free_help(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = None
    if update.effective_user:
        user_record = license_service.db.latest_user(update.effective_user.id)
        if user_record:
            machine_id = str(user_record.get("machine_id", "")).strip().upper() or None

    text = (
        "Bước 1: Tải tool\n"
        "Bước 2: Giải nén\n"
        "Bước 3: Mở app\n"
        "Bước 4: Copy Machine ID, gửi cho bot để nhận license JSON\n"
        "Bước 5: Dán license JSON vào app và bấm Activate"
    )
    if machine_id:
        text += (
            f"\n\nMachine ID hiện tại:\n{machine_id}\n"
            f"Link nhận license:\nhttps://t.me/Aidaily79_bot?start={quote(machine_id, safe='')}"
        )
    await _safe_edit_or_send(update, text, _ai_daily_keyboard(), edit=edit)


async def _handle_free_license_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("FREE_LICENSE_CLICKED", flush=True)
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = license_service.recent_machine_id_for_user(user.id) if user else ""

    if not machine_id:
        await update.effective_message.reply_text(
            "Chưa có Machine ID để cấp license.\n\n"
            "Vui lòng mở tool -> tab Kích Hoạt -> bấm Nhận License Trial 10 Ngày, "
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
            "Bạn được tặng trial 10 ngày.\n"
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
        "Machine ID này đã nhận trial 10 ngày.",
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
        "🎁 NHẬN LICENSE TRIAL 10 NGÀY\n\n"
        "Bước 4:\n"
        "Bot tự cấp license.\n\n"
        "Bước 5:\n"
        "Kích hoạt và sử dụng."
    )
    await _safe_edit_or_send(update, text, _ai_daily_keyboard(), edit=edit)


async def _send_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = license_service.recent_machine_id_for_user(user.id) if user else ""
    text = "Chọn gói license trả phí:"
    if machine_id:
        text += f"\n\nMachine ID hiện tại:\n{machine_id}"
    else:
        text += "\n\nNếu chưa có Machine ID, bấm gói rồi gửi Machine ID của bạn."
    await _safe_edit_or_send(update, text, _paid_license_plan_keyboard(machine_id), edit=edit)


def _paid_license_prompt_text(product_id: str) -> str:
    product = TOOL_LICENSE_PRODUCTS.get(product_id, TOOL_LICENSE_PRODUCTS["TOOL_LIFETIME"])
    if product["plan"] == YEAR_365_PLAN:
        return (
            "💎 Gia hạn 1 năm - 450.000đ\n\n"
            "Vui lòng gửi Machine ID của bạn.\n"
            "Sau khi thanh toán/xác nhận, bot sẽ gửi file license 365 ngày."
        )
    return (
        "💎 Bản vĩnh viễn - 990.000đ\n\n"
        "Vui lòng gửi Machine ID của bạn.\n"
        "Sau khi thanh toán/xác nhận, bot sẽ gửi file license vĩnh viễn."
    )


def _get_package_info(product_key: str, package_key: str) -> dict[str, object] | None:
    product_key_norm = str(product_key or "").strip().upper()
    package_key_norm = str(package_key or "").strip().upper()
    try:
        repository = StoreRepository(_resolve_store_db_path())
        packages = repository.list_packages_by_category(_catalog_lookup_key(product_key))
        for package in packages:
            canonical_package_code = _catalog_lookup_key(product_key)
            candidate_codes = {
                str(package.get("product_code", "") or "").strip().upper(),
                str(package.get("code", "") or "").strip().upper(),
                str(package.get("display_name", "") or "").strip().upper(),
                str(package.get("name", "") or "").strip().upper(),
                str(package.get("package_code", "") or "").strip().upper(),
                str(canonical_package_code).strip().upper(),
            }
            if package_key_norm in candidate_codes or _menu_stock_product_code(package_key_norm) in candidate_codes:
                return {
                    "product_id": str(package["id"]),
                    "product_code": canonical_package_code,
                    "package_code": canonical_package_code,
                    "category_key": str(package.get("category_key") or product_key_norm),
                    "display_name": str(package["display_name"]),
                    "price_vnd": int(package["price_vnd"]),
                    "available_count": int(package["available_count"] or 0),
                    "source": "sqlite",
                    "price_source": "sqlite.products.price_vnd",
                    "reservation_sqlite": True,
                }
        package = repository.get_product_details(package_key)
        if package and package["active"]:
            return {
                "product_id": str(package["id"]),
                "product_code": _catalog_lookup_key(product_key),
                "package_code": _catalog_lookup_key(product_key),
                "category_key": str(package.get("category_key") or product_key_norm),
                "display_name": str(package["name"]),
                "price_vnd": int(package["price_vnd"]),
                "available_count": repository.get_stock_count(str(package["code"])),
                "source": "sqlite",
                "price_source": "sqlite.products.price_vnd",
                "reservation_sqlite": True,
            }
    except (OSError, RuntimeError, sqlite3.Error):
        pass
    return None


async def _create_paid_license_order(update: Update, context: ContextTypes.DEFAULT_TYPE, machine_id: str, product_id: str, *, edit: bool = False) -> None:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = str(machine_id or "").strip().upper()
    product_id = str(product_id or "").strip().upper()
    product = TOOL_LICENSE_PRODUCTS.get(product_id)
    if not product:
        await update.effective_message.reply_text("Gói license không hợp lệ.", reply_markup=_ai_daily_keyboard())
        return

    if not machine_id:
        pending_license_product_by_user[int(user.id)] = product_id
        text = _paid_license_prompt_text(product_id)
        await _safe_edit_or_send(update, text, _ai_daily_keyboard(), edit=edit)
        return

    pending_license_product_by_user.pop(int(user.id), None)

    license_service.touch_user(
        user.id,
        _user_label(user),
        machine_id=machine_id,
        source=f"paid_{product['plan'].lower()}",
        reminder_state="pending",
    )
    order = _create_license_sales_order(update, product_id, machine_id)
    await update.effective_message.reply_text(
        "Đã tạo đơn license.\n"
        f"Order ID: {order['order_id']}\n"
        f"Machine ID: {machine_id}\n"
        f"Số tiền: {_format_vnd(int(order['total']))}đ\n\n"
        "License sẽ chỉ được gửi sau khi thanh toán được xác nhận."
    )
    await _send_acb_qr(update, context, str(order["order_id"]))


async def _create_upgrade_order(update: Update, context: ContextTypes.DEFAULT_TYPE, machine_id: str, *, edit: bool = False) -> None:
    await _create_paid_license_order(update, context, machine_id, "TOOL_LIFETIME", edit=edit)


async def _send_support(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    support_username = context.application.bot_data.get("support_username", "@Aidaily79")
    text = f"Telegram:\n{support_username}\n\nhoac username ho tro cau hinh trong .env"
    await _show_navigation_screen(update, text, _main_menu_keyboard(), edit=edit)


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
                "Bạn được tặng trial 10 ngày.\n"
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
        "Machine ID này đã nhận trial 10 ngày.",
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
                "Bạn được tặng trial 10 ngày.\n"
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
        "Machine ID này đã nhận trial 10 ngày.",
        reply_markup=_upgrade_permanent_keyboard_for_machine(machine_id),
    )
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning(
        "START HANDLER file=%s function=cmd_start user_id=%s",
        __file__,
        getattr(getattr(update, "effective_user", None), "id", None),
    )
    if update.effective_user:
        _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
        _release_current_user_reservation(context, update.effective_user.id)
    logger.debug(
        "SALES_STAGE stage=start user_id=%s callback_data=%s",
        getattr(getattr(update, "effective_user", None), "id", None),
        getattr(getattr(update, "callback_query", None), "data", "") if getattr(update, "callback_query", None) else "",
    )
    args = context.args or []
    machine_id = ""
    if args:
        candidate = str(args[0]).strip().upper()
        if _looks_like_machine_id(candidate):
            machine_id = candidate
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    license_service.touch_user(user.id, _user_label(user), machine_id=machine_id, source="start", reminder_state="new")

    if machine_id:
        handled = await _maybe_issue_deeplink_license(update, context, machine_id)
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
    pending_product_id = pending_license_product_by_user.get(int(update.effective_user.id))
    if pending_product_id:
        logger.info(
            "paid_license_machine_id telegram_user_id=%s product_id=%s machine_id=%s",
            getattr(update.effective_user, "id", None),
            pending_product_id,
            text,
        )
        await _create_paid_license_order(update, context, text, pending_product_id)
        return
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
        "Thanh cong. Da cap trial 10 ngay.\n"
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
    product_name = str(order.get("product_id") or order.get("product_name", "")).upper()
    quantity = int(order.get("quantity", 1))
    sqlite_product_code = TELEGRAM_PRODUCT_CODE_MAP.get(product_name, product_name)
    order_id = str(order.get("order_id", ""))
    logger.warning(
        "DELIVERY START order_id=%s product_name=%s sqlite_product_code=%s quantity=%s inventory_source=%s",
        order_id,
        product_name,
        sqlite_product_code,
        quantity,
        order.get("inventory_source"),
    )
    use_sqlite_delivery = bool(sqlite_product_code) and (
        order.get("inventory_source") == "sqlite"
        or (order.get("inventory_source") is None and product_name in TELEGRAM_PRODUCT_CODE_MAP)
    )
    if use_sqlite_delivery:
        try:
            repository = StoreRepository(_resolve_store_db_path())
            if not repository.mark_account_order_paid_for_fulfillment(order_id):
                logger.error("SQLite reservation order is missing for mapped account order %s", order_id)
                return False, "", "Không tìm thấy reservation SQLite cho đơn đã thanh toán."
            delivered_items = repository.deliver_reserved_items(order_id)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            logger.exception("SQLite delivery failed for mapped account order %s", order_id)
            return False, "", f"Không thể giao hàng SQLite an toàn: {exc}"
        if not delivered_items:
            logger.error("SQLite reservation has no deliverable item for mapped account order %s", order_id)
            return False, "", "Không có account SQLite đã reserve cho đơn này."
        logger.warning(
            "DELIVERY SELECTED order_id=%s delivered_count=%s delivery_preview=%s",
            order_id,
            len(delivered_items),
            [_mask_credential_for_log(item) for item in delivered_items],
        )
        return True, "\n".join(delivered_items), "Đã giao hàng từ SQLite reservation."

    logger.error("SQLite delivery required but order is not linked to SQLite inventory: order_id=%s product=%s", order.get("order_id", ""), product_name)
    return False, "", "Đơn hàng không có kho SQLite để giao tự động."


def _issue_license_for_sales_order(license_service: LicenseService, order: dict[str, object]):
    plan = str(order.get("plan") or LIFETIME_PLAN).strip().upper()
    return license_service.issue_paid_license(
        int(order.get("telegram_user_id", 0)),
        str(order.get("username", "")),
        str(order.get("machine_id", "")).strip().upper(),
        plan=plan,
        customer=str(order.get("username", "") or "Customer"),
        order_id=str(order.get("order_id", "")),
        payment_status="paid",
    )


async def fulfill_order(context: ContextTypes.DEFAULT_TYPE, order_id: str) -> dict[str, object]:
    logger.warning("FULFILLMENT START order_id=%s", order_id)
    _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
    sales_order = _find_order(order_id)
    if sales_order:
        if str(sales_order.get("delivery_type", "account")) == "license":
            license_service: LicenseService = context.application.bot_data["license_service"]
            existing_file = str(sales_order.get("license_file", ""))
            if existing_file and Path(existing_file).exists():
                result_record = license_service.find(str(sales_order.get("machine_id", "")))
                license_path = Path(existing_file)
            else:
                result = _issue_license_for_sales_order(license_service, sales_order)
                if not result.ok:
                    return {"ok": False, "type": "license", "message": result.message, "order": sales_order}
                result_record = result.record or {}
                license_path = result.license_path or Path(str(result_record.get("license_file", "")))
                license_service.update_user_from_license(result_record, source="sales_order_paid")
            now = _utc_now_iso()
            updated_order = _update_order(
                order_id,
                payment_status="paid",
                order_status="delivered",
                paid_at=sales_order.get("paid_at") or now,
                delivered_at=now,
                delivery=str(license_path),
                license_file=str(license_path),
            )
            customer_id = sales_order.get("telegram_user_id")
            if customer_id:
                plan = str(sales_order.get("plan", "")).upper()
                plan_label = "365 ngày" if plan == YEAR_365_PLAN else "vĩnh viễn"
                await context.bot.send_message(
                    chat_id=int(customer_id),
                    text=(
                        "💰 Thanh toán thành công\n\n"
                        f"Bot đã cấp license {plan_label}.\n"
                        f"Order ID: {order_id}\n"
                        f"Machine ID: {sales_order.get('machine_id', '')}"
                    ),
                    reply_markup=_post_delivery_navigation_keyboard(),
                )
                if license_path and Path(license_path).exists():
                    with Path(license_path).open("rb") as handle:
                        await context.bot.send_document(chat_id=int(customer_id), document=InputFile(handle, filename=Path(license_path).name))
                if getattr(context, "user_data", None) is not None:
                    context.user_data.pop("pending_order_id", None)
            return {
                "ok": True,
                "type": "license",
                "delivery": str(license_path),
                "message": "Đã tạo và gửi license.",
                "record": result_record,
                "order": updated_order or sales_order,
            }

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
                "💰 Thanh toán thành công\n\n"
                "Đang chuẩn bị tài khoản...\n\n"
                f"🧾 Đơn hàng: {order_id}\n"
                f"🎁 Gói: {sales_order.get('package_name', '')}\n"
                f"📦 Số lượng: {sales_order.get('quantity', '')}"
            )
            if delivery_text:
                customer_text += f"\n\n🎁 Giao hàng thành công\n\n{delivery_text}"
            else:
                customer_text += "\n\nAdmin sẽ xử lý giao hàng cho bạn."
            await context.bot.send_message(
                chat_id=int(customer_id),
                text=customer_text,
                reply_markup=_post_delivery_navigation_keyboard() if delivery_text else None,
            )
            if getattr(context, "user_data", None) is not None:
                context.user_data.pop("pending_order_id", None)
        if not delivery_text:
            await _notify_admins_paid_without_delivery(context, updated_order or sales_order, delivery_message)
            logger.error(
                "DELIVERY SENT FAIL order_id=%s reason=%s final_status=%s",
                order_id,
                delivery_message,
                (updated_order or sales_order).get("order_status", ""),
            )
        else:
            logger.warning(
                "DELIVERY SENT SUCCESS order_id=%s final_status=%s",
                order_id,
                (updated_order or sales_order).get("order_status", ""),
            )
        return {
            "ok": delivered,
            "type": "sales_order",
            "delivery": delivery_text,
            "message": delivery_message,
            "order": updated_order or sales_order,
        }

    return {"ok": False, "type": "order", "message": "Không tìm thấy order chung.", "order_id": order_id}


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

    await update.effective_message.reply_text("Không tìm thấy order chung.")


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
    _release_order_reservation(order_id, store_db_path=context.application.bot_data.get("store_db_path"))
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


async def cmd_dbstock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show SQLite inventory to configured administrators without changing sales flow."""
    telegram_user_id = update.effective_user.id if update.effective_user else "unknown"
    if not update.effective_user or not _is_admin(
        update.effective_user.id, context.application.bot_data["admin_ids"]
    ):
        await update.effective_message.reply_text(
            f"Khong co quyen. Telegram ID cua ban: {telegram_user_id}"
        )
        return
    store_db_path: Path = context.application.bot_data["store_db_path"]
    if not store_db_path.is_file():
        await update.effective_message.reply_text(
            f"Khong tim thay store.db: {store_db_path}"
        )
        return
    try:
        rows = StoreRepository(store_db_path).get_stock_summary()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.exception("dbstock read failed")
        await update.effective_message.reply_text(f"Khong the doc store.db: {exc}")
        return

    if not rows:
        await update.effective_message.reply_text("store.db chua co san pham nao.")
        return
    lines = ["TON KHO SQLITE"]
    for row in rows:
        active_label = "active" if row["active"] else "inactive"
        lines.append(
            f"{row['product_code']} | {row['display_name']} | {active_label} | "
            f"available: {row['available']} | reserved: {row['reserved']} | "
            f"delivered: {row['delivered']} | disabled: {row['disabled']}"
        )
    await update.effective_message.reply_text("\n".join(lines))


def _admin_store_repository(context: ContextTypes.DEFAULT_TYPE) -> StoreRepository:
    return StoreRepository(context.application.bot_data["store_db_path"])


async def cmd_addstock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) not in {3, 4}:
        await update.effective_message.reply_text("Dung: /addstock <product_code> <email> <password> [2fa]")
        return
    product_code, email, password, *optional_2fa = args
    credential = "|".join([email, password, *optional_2fa])
    try:
        item_id = _admin_store_repository(context).add_inventory_item(product_code, credential)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        await update.effective_message.reply_text(f"Khong the them kho SQLite: {exc}")
        return
    await update.effective_message.reply_text(
        f"Da them 1 account vao kho SQLite {product_code.upper()}. Item ID: {item_id}"
    )


async def cmd_removestock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) != 1:
        await update.effective_message.reply_text("Dung: /removestock <item_id>")
        return
    try:
        _admin_store_repository(context).set_inventory_item_disabled(args[0], True)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        await update.effective_message.reply_text(f"Khong the xoa an toan item SQLite: {exc}")
        return
    await update.effective_message.reply_text(f"Da disable item SQLite: {args[0]}")


async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) != 2 or args[0].lower() not in {"product", "item"}:
        await update.effective_message.reply_text("Dung: /disable product <product_code> hoac /disable item <item_id>")
        return
    try:
        repository = _admin_store_repository(context)
        if args[0].lower() == "product":
            repository.set_product_active(args[1], False)
        else:
            repository.set_inventory_item_disabled(args[1], True)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        await update.effective_message.reply_text(f"Khong the disable SQLite: {exc}")
        return
    await update.effective_message.reply_text(f"Da disable {args[0].lower()} SQLite: {args[1]}")


async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) != 2 or args[0].lower() not in {"product", "item"}:
        await update.effective_message.reply_text("Dung: /enable product <product_code> hoac /enable item <item_id>")
        return
    try:
        repository = _admin_store_repository(context)
        if args[0].lower() == "product":
            repository.set_product_active(args[1], True)
        else:
            repository.set_inventory_item_disabled(args[1], False)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        await update.effective_message.reply_text(f"Khong the enable SQLite: {exc}")
        return
    await update.effective_message.reply_text(f"Da enable {args[0].lower()} SQLite: {args[1]}")


async def cmd_importexcel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id, context.application.bot_data["admin_ids"]):
        await update.effective_message.reply_text("Khong co quyen.")
        return
    args = context.args or []
    if len(args) != 1:
        await update.effective_message.reply_text("Dung: /importexcel <ten_file.xlsx hoac ten_file.csv trong thu muc imports>")
        return
    candidate = (IMPORTS_DIR / args[0]).resolve()
    if not candidate.is_relative_to(IMPORTS_DIR.resolve()) or not candidate.is_file():
        await update.effective_message.reply_text("Khong tim thay file import trong thu muc imports.")
        return
    try:
        report = import_inventory(candidate, context.application.bot_data["store_db_path"])
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        logger.exception("Excel import failed")
        await update.effective_message.reply_text(f"Import SQLite that bai: {exc}")
        return
    await update.effective_message.reply_text(
        "Import SQLite xong.\n"
        f"Product tao: {report['products_created']}\n"
        f"Product cap nhat: {report['products_updated']}\n"
        f"Account them: {report['credentials_added']}\n"
        f"Trung bo qua: {report['credentials_duplicate']}\n"
        f"Dong loi: {report['row_errors']}"
    )


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
        await _show_navigation_screen(update, _start_help_text(), _main_menu_keyboard(), edit=True)
    elif data == "menu_products":
        logger.warning(
            "PRODUCT MENU CALLBACK file=%s function=_on_menu_impl callback_data=%s user_id=%s",
            __file__,
            data,
            getattr(getattr(update, "effective_user", None), "id", None),
        )
        await _render_product_menu(update, edit=True)
    elif data == "menu_tools":
        # Tool is the original license/download branch, separate from account catalog products.
        await _show_navigation_screen(update, _ai_daily_text(), _ai_daily_keyboard(), edit=True)
    elif data == "menu_orders":
        await _send_orders(update, context, edit=True)
    elif data == "menu_history":
        await _send_purchase_history(update, context, edit=True)
    elif data == "menu_payment":
        await _send_payment(update, context, edit=True)
    elif data == "menu_ai_daily":
        await _show_navigation_screen(update, _ai_daily_text(), _ai_daily_keyboard(), edit=True)
    elif data == "product_ai_daily":
        await _show_navigation_screen(update, _ai_daily_text(), _ai_daily_keyboard(), edit=True)
    elif data.startswith("pkg:"):
        parts = data.split(":")
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            await _send_invalid_callback(update)
            return
        _, product_code, package_code = parts
        logger.debug(
            "CALLBACK pkg callback_data=%s parsed_parts=%s user_id=%s product_code=%s package_code=%s",
            data,
            parts,
            getattr(getattr(update, "effective_user", None), "id", None),
            product_code,
            package_code,
        )
        package = _get_package_info(product_code, package_code)
        if not package or int(package["available_count"]) <= 0:
            await _safe_edit_or_send(update, "Gói không hợp lệ.", _product_menu_keyboard(), edit=True)
            return
        log_sales_state(
            "package_callback",
            product_code,
            callback_data=data,
            package_id=package_code,
            quantity=0,
            database_path=context.application.bot_data.get("store_db_path"),
        )
        await _send_quantity_choice(update, product_code, package_code, edit=True)
    elif data.startswith("qty:"):
        parts = data.split(":")
        if len(parts) != 4 or not parts[1].strip() or not parts[2].strip() or not parts[3].strip():
            await _send_invalid_callback(update)
            return
        _, product_code, package_code, raw_qty = parts
        try:
            requested_quantity = int(raw_qty)
        except (TypeError, ValueError):
            await _send_invalid_callback(update)
            return
        logger.debug(
            "CALLBACK qty callback_data=%s parsed_parts=%s user_id=%s product_code=%s package_code=%s requested_quantity=%s",
            data,
            parts,
            getattr(getattr(update, "effective_user", None), "id", None),
            product_code,
            package_code,
            raw_qty,
        )
        log_sales_state(
            "QTY_HANDLER_ENTER",
            product_code,
            callback_data=data,
            package_id=package_code,
            quantity=requested_quantity,
            database_path=context.application.bot_data.get("store_db_path"),
        )
        package = _get_package_info(product_code, package_code)
        menu_available = _menu_available_count(product_code)
        package_available = int(package["available_count"]) if package else 0
        logger.debug(
            "CALLBACK qty resolution callback_data=%s user_id=%s database_path=%s price_source=%s product_code=%s product_id=%s package_id=%s package_code=%s package_name=%s quantity=%s available_by_menu_query=%s available_by_package_page_query=%s available_by_reserve_query=%s price_vnd=%s",
            data,
            getattr(getattr(update, "effective_user", None), "id", None),
            _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
            _price_source_label(package),
            product_code,
            package.get("product_id") if package else None,
            package.get("product_id") if package else None,
            package_code,
            package.get("display_name") if package else None,
            requested_quantity,
            menu_available,
            package_available,
            package_available,
            package.get("price_vnd") if package else None,
        )
        if not package:
            await _safe_edit_or_send(update, "Gói không hợp lệ.", _product_menu_keyboard(), edit=True)
            return
        try:
            if update.effective_user:
                _release_current_user_reservation(context, update.effective_user.id)
            _release_expired_sqlite_reservations(context.application.bot_data.get("store_db_path"))
            logger.debug(
                "ORDER_CREATE_START callback_data=%s user_id=%s database_path=%s price_source=%s product_code=%s package_code=%s quantity=%s available_by_menu_query=%s available_by_package_page_query=%s available_by_reserve_query=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
                _price_source_label(package),
                product_code,
                package_code,
                requested_quantity,
                menu_available,
                package_available,
                package_available,
            )
            order = _create_sales_order(update, product_code, package_code, requested_quantity)
            logger.debug(
                "ORDER_CREATED callback_data=%s user_id=%s database_path=%s price_source=%s order_id=%s product_code=%s package_code=%s amount_vnd=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
                _price_source_label(package),
                order.get("order_id", ""),
                order.get("product_code", ""),
                order.get("package_code", ""),
                order.get("total", 0),
            )
        except InventoryReservationError as exc:
            logger.debug(
                "CALLBACK qty failed callback_data=%s user_id=%s product_code=%s package_code=%s quantity=%s reason=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                product_code,
                package_code,
                raw_qty,
                exc,
            )
            await _safe_edit_or_send(update, str(exc), _product_menu_keyboard(), edit=True)
            return
        except Exception as exc:
            logger.exception(
                "EXCEPTION_CAUGHT callback_data=%s user_id=%s product_code=%s package_code=%s quantity=%s exception_type=%s exception_message=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                product_code,
                package_code,
                raw_qty,
                type(exc).__name__,
                exc,
            )
            await _send_purchase_error(update, context, reason=str(exc), order_id="")
            return
        log_sales_state(
            "RESERVE_OK",
            str(order.get("product_code") or product_code),
            order_id=str(order.get("order_id", "")),
            callback_data=data,
            package_id=str(order.get("package_code", package_code)),
            quantity=int(order.get("quantity", requested_quantity)),
            database_path=context.application.bot_data.get("store_db_path"),
        )
        if update.effective_user and getattr(context, "user_data", None) is not None:
            context.user_data["pending_order_id"] = str(order.get("order_id", ""))
        try:
            logger.debug(
                "PAYMENT_BUILD_START callback_data=%s user_id=%s chat_id=%s message_id=%s database_path=%s price_source=%s order_id=%s product_code=%s package_code=%s quantity=%s amount_vnd=%s bank_account=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                getattr(getattr(getattr(update, "effective_message", None), "chat", None), "id", None),
                getattr(getattr(update, "callback_query", None), "message", None).message_id if getattr(getattr(update, "callback_query", None), "message", None) else None,
                _resolve_store_db_path(context.application.bot_data.get("store_db_path")),
                _price_source_label(package),
                str(order.get("order_id", "")),
                str(order.get("product_code", order.get("product_id", "")) or ""),
                str(order.get("package_code", "")),
                int(order.get("quantity", 0) or 0),
                int(order.get("total", 0) or 0),
                "",
            )
            await _send_payment_choice(update, order, edit=True)
            logger.debug(
                "TELEGRAM_SEND_OK callback_data=%s order_id=%s branch=payment_choice",
                data,
                str(order.get("order_id", "")),
            )
        except Exception as exc:
            logger.exception(
                "PAYMENT_BUILD_FAIL callback_data=%s user_id=%s order_id=%s exception_type=%s exception_message=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                str(order.get("order_id", "")),
                type(exc).__name__,
                exc,
            )
            await _send_purchase_error(update, context, reason=str(exc), order_id=str(order.get("order_id", "")))
            return
    elif data.startswith("pay_acb:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        order_id = parts[1].strip()
        try:
            await _send_acb_qr(update, context, order_id)
        except Exception as exc:
            logger.exception(
                "EXCEPTION_CAUGHT callback_data=%s user_id=%s order_id=%s exception_type=%s exception_message=%s",
                data,
                getattr(getattr(update, "effective_user", None), "id", None),
                order_id,
                type(exc).__name__,
                exc,
            )
            await _send_purchase_error(update, context, reason=str(exc), order_id=order_id)
    elif data.startswith("pay_wallet:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        order_id = parts[1].strip()
        await _safe_edit_or_send(
            update,
            "Số dư ví hiện tại không đủ hoặc chưa được cấu hình. Vui lòng chọn ACB để thanh toán.",
            _payment_choice_keyboard(order_id),
            edit=True,
        )
    elif data.startswith("paid_notify:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        order_id = parts[1].strip()
        await _send_paid_notify(update, context, order_id)
    elif data.startswith("cancel_order:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        order_id = parts[1].strip()
        _release_order_reservation(order_id, store_db_path=context.application.bot_data.get("store_db_path"))
        _update_order(order_id, payment_status="cancelled", order_status="cancelled")
        await _safe_edit_or_send(update, "Giao dịch đã hủy.", _main_menu_keyboard(), edit=True)
    elif data.startswith("product:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        product_code = parts[1].strip()
        logger.debug(
            "CALLBACK product callback_data=%s parsed_parts=%s user_id=%s product_code=%s",
            data,
            parts,
            getattr(getattr(update, "effective_user", None), "id", None),
            product_code,
        )
        if update.effective_user:
            _release_current_user_reservation(context, update.effective_user.id)
        await _send_product_detail(update, context, product_code, edit=True)
    elif data.startswith("buy:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        product_code = parts[1].strip()
        await _send_payment(update, context, edit=True, product_name=product_code)
    elif data == "menu_download":
        await _send_download(update, context, edit=True)
    elif data == "menu_free":
        await _handle_free_license_click(update, context)
    elif data == "menu_help":
        await _send_help(update, context, edit=True)
    elif data.startswith("license_product_machine:"):
        parts = data.split(":")
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            await _send_invalid_callback(update)
            return
        _, product_id, machine_id = parts
        await _create_paid_license_order(update, context, "", product_id, edit=True)
    elif data.startswith("license_product:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        product_id = parts[1].strip().upper()
        await _create_paid_license_order(update, context, "", product_id, edit=True)
    elif data.startswith("upgrade_machine:"):
        parts = data.split(":", 1)
        if len(parts) != 2 or not parts[1].strip():
            await _send_invalid_callback(update)
            return
        machine_id = parts[1].strip().upper()
        await _create_upgrade_order(update, context, machine_id, edit=True)
    elif data == "menu_upgrade":
        await _send_upgrade(update, context, edit=True)
    elif data == "menu_support":
        await _send_support(update, context, edit=True)
    else:
        await _send_invalid_callback(update)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning(
        "CALLBACK RECEIVED: handler=on_menu callback_data=%s user_id=%s",
        getattr(getattr(update, "callback_query", None), "data", None),
        getattr(getattr(update, "effective_user", None), "id", None),
    )
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
        "STORE_DB_PATH": os.environ.get("STORE_DB_PATH", "database/store.db").strip(),
        "FREE_LICENSE_DAYS": os.environ.get("FREE_LICENSE_DAYS", str(DEFAULT_FREE_DAYS)).strip(),
        "PAID_LICENSE_PRICE": os.environ.get("PAID_LICENSE_PRICE", str(DEFAULT_PAID_PRICE)).strip(),
        "BANK_NAME": os.environ.get("BANK_NAME", "").strip(),
        "BANK_ACCOUNT": os.environ.get("BANK_ACCOUNT", "").strip(),
        "BANK_ACCOUNT_NAME": os.environ.get("BANK_ACCOUNT_NAME", "").strip(),
        "BANK_QR_URL": os.environ.get("BANK_QR_URL", "").strip(),
        "BANK_PROVIDER": os.environ.get("BANK_PROVIDER", "webhook").strip() or "webhook",
        "TOOL_DOWNLOAD_URL": os.environ.get("TOOL_DOWNLOAD_URL", "").strip(),
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
        free_days=10,
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
    store_db_path = Path(cfg["STORE_DB_PATH"]).expanduser()
    if not store_db_path.is_absolute():
        store_db_path = PROJECT_ROOT / store_db_path
    try:
        _initialize_store_db(store_db_path)
    except sqlite3.Error as exc:
        raise SystemExit(f"Cannot initialize store database at {store_db_path}: {exc}") from exc
    migrated_orders = _migrate_legacy_orders_to_sqlite(store_db_path)
    if migrated_orders:
        logger.info("Migrated %s legacy JSON order(s) into SQLite", migrated_orders)

    app = Application.builder().token(cfg["BOT_TOKEN"]).post_init(post_init).build()
    app.bot_data["admin_ids"] = admin_ids
    app.bot_data["license_service"] = license_service
    app.bot_data["payment_service"] = payment_service
    app.bot_data["store_db_path"] = store_db_path
    app.bot_data["bank_provider"] = cfg["BANK_PROVIDER"]
    app.bot_data["tool_download_url"] = cfg["TOOL_DOWNLOAD_URL"]
    app.bot_data["support_username"] = cfg["SUPPORT_USERNAME"] or "@Aidaily79"
    logger.info(
        "Runtime store database loaded DATABASE_PATH=%s bank_provider=%s",
        store_db_path,
        cfg["BANK_PROVIDER"],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("license", cmd_license))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("grant_free", cmd_grant_free))
    app.add_handler(CommandHandler("grant_permanent", cmd_grant_permanent))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("cancel_order", cmd_cancel_order))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("pending_orders", cmd_pending_orders))
    app.add_handler(CommandHandler("dbstock", cmd_dbstock))
    app.add_handler(CommandHandler("addstock", cmd_addstock))
    app.add_handler(CommandHandler("removestock", cmd_removestock))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("importexcel", cmd_importexcel))
    app.add_handler(CommandHandler("check_bank", cmd_check_bank))
    app.add_handler(CommandHandler("transactions", cmd_transactions))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("list_licenses", cmd_list_licenses))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_machine_id))
    logger.warning("CALLBACK HANDLER ORDER: 1 handler=on_menu pattern=None")
    app.add_handler(CallbackQueryHandler(on_menu))
    if app.job_queue:
        app.job_queue.run_repeating(bank_checker_job, interval=15, first=5, name="bank_checker")
        app.job_queue.run_repeating(
            sqlite_reservation_cleanup_job,
            interval=60,
            first=60,
            name="sqlite_reservation_cleanup",
        )
    return app


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
