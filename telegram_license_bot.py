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

PROJECT_ROOT = Path(__file__).resolve().parent
IMPORTS_DIR = PROJECT_ROOT / "imports"
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
        "display_name": "🚀 Vĩnh viễn - 990.000đ",
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
                InlineKeyboardButton("🛍 Sản Phẩm", callback_data="menu_products"),
                InlineKeyboardButton("🛠 Tool", callback_data="menu_tools"),
            ],
            [
                InlineKeyboardButton("💰 Nạp tiền", callback_data="menu_payment"),
                InlineKeyboardButton("👤 TÀI KHOẢN", callback_data="menu_orders"),
            ],
            [InlineKeyboardButton("📦 Đơn hàng", callback_data="menu_orders"), InlineKeyboardButton("📦 Đơn đặt trước", callback_data="menu_orders")],
            [InlineKeyboardButton("🌐 Đổi ngôn ngữ", callback_data="menu_main"), InlineKeyboardButton("💬 Hỗ trợ", callback_data="menu_support")],
            [InlineKeyboardButton("Đóng", callback_data="menu_main")],
        ]
    )


def _ai_daily_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 TẢI TOOL", url=os.environ.get("DOWNLOAD_URL", DEFAULT_DOWNLOAD_URL)),
                InlineKeyboardButton("🎁 NHẬN LICENSE TRIAL 10 NGÀY", callback_data="menu_free"),
            ],
            [
                InlineKeyboardButton("📖 HƯỚNG DẪN", callback_data="menu_help"),
                InlineKeyboardButton("🔥 NÂNG CẤP VĨNH VIỄN", callback_data="menu_upgrade"),
            ],
            [InlineKeyboardButton("💎 Gia hạn 1 năm - 450.000đ", callback_data="license_product:TOOL_YEAR_365")],
            [InlineKeyboardButton("🚀 Vĩnh viễn - 990.000đ", callback_data="license_product:TOOL_LIFETIME")],
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
            [InlineKeyboardButton("💎 Gia hạn 1 năm - 450.000đ", callback_data="license_product:TOOL_YEAR_365")],
            [InlineKeyboardButton("🚀 Vĩnh viễn - 990.000đ", callback_data="license_product:TOOL_LIFETIME")],
        ]
    )


def _paid_license_plan_keyboard(machine_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    for product_id, product in TOOL_LICENSE_PRODUCTS.items():
        label = str(product["display_name"])
        rows.append([InlineKeyboardButton(label, callback_data=f"license_product:{product_id}")])
    rows.append([InlineKeyboardButton("↩️ Quay lại", callback_data="menu_ai_daily")])
    return InlineKeyboardMarkup(rows)


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


def _resolve_store_db_path(store_db_path: Path | str | None = None) -> Path:
    if store_db_path is not None:
        path = Path(store_db_path).expanduser()
    else:
        path = Path(os.environ.get("STORE_DB_PATH", "database/store.db")).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _json_product_display_info(product_key: str) -> dict[str, object]:
    normalized_key = product_key.upper()
    item = _load_inventory().get(normalized_key)
    _, available, stock = _inventory_status(normalized_key, item)
    return {
        "product_key": normalized_key,
        "product_code": None,
        "product_name": normalized_key,
        "price_vnd": 0,
        "warranty_days": 0,
        "active": bool(item.get("active", True)) if item else True,
        "available_count": stock,
        "available": available,
        "source": "inventory.json",
    }


def _log_sqlite_fallback_once(log_key: str, message: str, *args: object) -> None:
    if log_key in _SQLITE_FALLBACK_LOGGED:
        return
    _SQLITE_FALLBACK_LOGGED.add(log_key)
    logger.warning(message, *args)


def get_product_display_info(
    product_key: str, *, store_db_path: Path | str | None = None
) -> dict[str, object]:
    """Read mapped product metadata from SQLite; otherwise retain JSON fallback."""
    normalized_key = product_key.upper()
    product_code = TELEGRAM_PRODUCT_CODE_MAP.get(normalized_key, normalized_key)

    path = _resolve_store_db_path(store_db_path)
    if not path.is_file():
        _log_sqlite_fallback_once(
            f"store-missing:{path}",
            "SQLite store missing at %s for %s; using inventory.json",
            path,
            normalized_key,
        )
        return _json_product_display_info(normalized_key)
    try:
        repository = StoreRepository(path)
        product = repository.get_product_details(product_code)
        if not product:
            _log_sqlite_fallback_once(
                f"product-missing:{product_code}",
                "SQLite product %s mapped from %s was not found; using inventory.json",
                product_code,
                normalized_key,
            )
            return _json_product_display_info(normalized_key)
        available_count = repository.get_stock_count(product_code)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        _log_sqlite_fallback_once(
            f"lookup-error:{normalized_key}",
            "SQLite lookup failed for %s; using inventory.json: %s",
            normalized_key,
            exc,
        )
        return _json_product_display_info(normalized_key)

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
    for product_key in PRODUCT_ORDER:
        if TELEGRAM_PRODUCT_CODE_MAP.get(product_key) == normalized_code:
            return product_key
    return normalized_code


def _catalog_product_keys() -> list[str]:
    path = _resolve_store_db_path()
    if not path.is_file():
        return list(PRODUCT_ORDER)
    try:
        products = StoreRepository(path).list_active_catalog_products()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.warning("SQLite catalog lookup failed; using PRODUCT_ORDER: %s", exc)
        return list(PRODUCT_ORDER)
    if not products:
        return list(PRODUCT_ORDER)
    return [_telegram_product_key_for_sqlite_code(str(product["code"])) for product in products]


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
    if "payment_status" in changes and "status" not in changes:
        changes["status"] = changes["payment_status"]
    orders = _load_orders()
    for order in reversed(orders):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            order.update(changes)
            _save_orders(orders)
            return order
    return None


def _create_sales_order(update: Update, product_name: str, package_name: str, quantity: int) -> dict[str, object]:
    user = update.effective_user
    package = _get_package_info(product_name, package_name)
    if not package or int(package["available_count"]) < int(quantity):
        raise InventoryReservationError("Sản phẩm hiện đã hết hàng, vui lòng quay lại sau.")
    unit_price = int(package["price_vnd"])
    now = _utc_now()
    order = {
        "order_id": _make_order_id(product_name),
        "telegram_user_id": int(user.id) if user else "",
        "username": _user_label(user) if user else "",
        "product_id": str(package["product_code"]).upper() if package["source"] == "sqlite" else product_name.upper(),
        "product_name": product_name,
        "package_name": str(package["display_name"]),
        "package_code": str(package["product_code"]),
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
    product_code = str(package["product_code"]) if package.get("reservation_sqlite") else ""
    if product_code:
        try:
            StoreRepository(_resolve_store_db_path()).create_pending_account_order_and_reserve(
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
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            logger.warning(
                "Mapped account order %s was not created because SQLite reservation failed: %s",
                order["order_id"],
                exc,
            )
            raise InventoryReservationError("Sản phẩm hiện đã hết hàng, vui lòng quay lại sau.") from exc
        order["inventory_source"] = "sqlite"
    else:
        # Missing SQLite products intentionally retain the legacy JSON path.
        order["inventory_source"] = "json"
    orders = _load_orders()
    orders.append(order)
    _save_orders(orders)
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
        return StoreRepository(path).release_expired_reservations()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.exception("SQLite reservation cleanup failed: %s", exc)
        return 0


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


def _catalog_category_keys(product_group: str = "account") -> list[str]:
    try:
        categories = StoreRepository(_resolve_store_db_path()).list_visible_categories(product_group)
    except (OSError, RuntimeError, sqlite3.Error):
        categories = []
    if categories:
        return [str(item["category_key"]).upper() for item in categories]
    return list(PRODUCT_ORDER) if product_group == "account" else []


def _product_menu_keyboard(product_group: str = "account") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    for product_name in _catalog_category_keys(product_group):
        label, _, _ = get_product_status_for_menu(product_name)
        buttons.append(InlineKeyboardButton(label, callback_data=f"product:{product_name}"))
    rows.extend(_chunked(buttons, 4))
    rows.append([InlineKeyboardButton("↩️ Quay lại", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _start_help_text() -> str:
    return "Chọn chức năng bên dưới:"


def _product_list_text() -> str:
    lines = ["🎁 SẢN PHẨM", ""]
    for product_name in _catalog_category_keys():
        label, _, _ = get_product_status_for_menu(product_name)
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
    rows = []
    try:
        packages = StoreRepository(_resolve_store_db_path()).list_packages_by_category(product_name.upper())
    except (OSError, RuntimeError, sqlite3.Error):
        packages = []
    if packages:
        rows.extend(
            [InlineKeyboardButton(
                f"🎁 {package['display_name']} - {_format_vnd(int(package['price_vnd']))}đ",
                callback_data=f"pkg:{product_name}:{package['product_code']}",
            )]
            for package in packages
        )
    else:
        rows.extend(
            [InlineKeyboardButton(f"🎁 Gói {name} - {_format_vnd(price)}đ", callback_data=f"pkg:{product_name}:{name}")]
            for name, price in PRODUCT_PACKAGES.items()
        )
    rows.append([InlineKeyboardButton("↩️ Quay lại sản phẩm", callback_data="menu_products")])
    rows.append([InlineKeyboardButton("🏠 Quay lại Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _quantity_keyboard(product_name: str, package_name: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(qty), callback_data=f"qty:{product_name}:{package_name}:{qty}")
        for qty in QUANTITY_OPTIONS
    ]
    rows = _chunked(buttons, 3)
    rows.append([InlineKeyboardButton("📝 Nhập số khác", callback_data=f"manualqty:{product_name}:{package_name}")])
    rows.append([InlineKeyboardButton("↩️ Quay lại gói", callback_data=f"product:{product_name}")])
    rows.append([InlineKeyboardButton("Đóng", callback_data="menu_main")])
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
    package = _get_package_info(product_name, package_name)
    unit_price = int(package["price_vnd"]) if package else 0
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
        f"🆔 Mã đơn: {order.get('order_id', '')}\n"
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


async def _send_products(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False, product_group: str = "account") -> None:
    text = _product_list_text() if product_group == "account" else "🛠 TOOL"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_product_menu_keyboard(product_group))
    else:
        await update.effective_message.reply_text(text, reply_markup=_product_menu_keyboard(product_group))


async def _send_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, product_name: str, *, edit: bool = False) -> None:
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
            has_packages = bool(StoreRepository(_resolve_store_db_path()).list_packages_by_category(product_name.upper()))
        except (OSError, RuntimeError, sqlite3.Error):
            has_packages = False
        if has_packages:
            text = _package_text(product_name)
            keyboard = _package_keyboard(product_name)
            if edit and update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=keyboard)
            else:
                await update.effective_message.reply_text(text, reply_markup=keyboard)
            return
        product_info = get_product_display_info(product_name)
        available = bool(product_info["available"])
        stock = int(product_info["available_count"])
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
    query = update.callback_query
    message = query.message if query and getattr(query, "message", None) else update.effective_message
    if not order:
        if query:
            await query.edit_message_text("Không tìm thấy đơn hàng.", reply_markup=_main_menu_keyboard())
        else:
            await message.reply_text("Không tìm thấy đơn hàng.", reply_markup=_main_menu_keyboard())
        return
    if _is_order_expired(order):
        _update_order(order_id, payment_status="expired", order_status="expired")
        _release_expired_sqlite_reservations()
        if query:
            await query.edit_message_text("Mã Order đã hết hạn. Vui lòng tạo lại đơn mới.", reply_markup=_product_menu_keyboard())
        else:
            await message.reply_text("Mã Order đã hết hạn. Vui lòng tạo lại đơn mới.", reply_markup=_product_menu_keyboard())
        return

    payment_service: PaymentService = context.application.bot_data["payment_service"]
    order = _update_order(order_id, payment_method="ACB") or order
    qr_url = _build_vietqr_url(order, payment_service)
    caption = _qr_caption(order, payment_service)
    await message.reply_photo(photo=qr_url, caption=caption, reply_markup=_qr_payment_keyboard(order_id))


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
        "Mở tool -> tab Kích Hoạt -> bấm Nhận License Trial 10 Ngày.\n\n"
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
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())


async def _send_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    user = update.effective_user
    license_service: LicenseService = context.application.bot_data["license_service"]
    machine_id = license_service.recent_machine_id_for_user(user.id) if user else ""
    text = "Chọn gói license trả phí:"
    if machine_id:
        text += f"\n\nMachine ID hiện tại:\n{machine_id}"
    else:
        text += "\n\nNếu chưa có Machine ID, bấm gói rồi gửi Machine ID của bạn."
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_paid_license_plan_keyboard(machine_id))
    else:
        await update.effective_message.reply_text(text, reply_markup=_paid_license_plan_keyboard(machine_id))


def _paid_license_prompt_text(product_id: str) -> str:
    product = TOOL_LICENSE_PRODUCTS.get(product_id, TOOL_LICENSE_PRODUCTS["TOOL_LIFETIME"])
    if product["plan"] == YEAR_365_PLAN:
        return (
            "💎 Gia hạn 1 năm - 450.000đ\n\n"
            "Vui lòng gửi Machine ID của bạn.\n"
            "Sau khi thanh toán/xác nhận, bot sẽ gửi file license 365 ngày."
        )
    return (
        "🚀 Bản vĩnh viễn - 990.000đ\n\n"
        "Vui lòng gửi Machine ID của bạn.\n"
        "Sau khi thanh toán/xác nhận, bot sẽ gửi file license vĩnh viễn."
    )


def _get_package_info(product_key: str, package_key: str) -> dict[str, object] | None:
    if package_key in PRODUCT_PACKAGES:
        product_info = get_product_display_info(product_key)
        return {
            "product_code": str(product_info.get("product_code") or product_key.upper()),
            "display_name": package_key,
            "price_vnd": PRODUCT_PACKAGES[package_key],
            "available_count": int(product_info["available_count"]),
            "source": "legacy",
            "reservation_sqlite": product_info["source"] == "store.db",
        }
    try:
        repository = StoreRepository(_resolve_store_db_path())
        package = repository.get_product_details(package_key)
        if not package or not package["active"]:
            return None
        return {
            "product_code": str(package["code"]),
            "display_name": str(package["name"]),
            "price_vnd": int(package["price_vnd"]),
            "available_count": repository.get_stock_count(str(package["code"])),
            "source": "sqlite",
            "reservation_sqlite": True,
        }
    except (OSError, RuntimeError, sqlite3.Error):
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
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=_ai_daily_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=_ai_daily_keyboard())
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
    use_sqlite_delivery = bool(sqlite_product_code) and (
        order.get("inventory_source") == "sqlite"
        or (order.get("inventory_source") is None and product_name in TELEGRAM_PRODUCT_CODE_MAP)
    )
    if use_sqlite_delivery:
        order_id = str(order.get("order_id", ""))
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
        return True, "\n".join(delivered_items), "Đã giao hàng từ SQLite reservation."

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
                        "Thanh toán thành công.\n"
                        f"Bot đã cấp license {plan_label}.\n"
                        f"Order ID: {order_id}\n"
                        f"Machine ID: {sales_order.get('machine_id', '')}"
                    ),
                )
                if license_path and Path(license_path).exists():
                    with Path(license_path).open("rb") as handle:
                        await context.bot.send_document(chat_id=int(customer_id), document=InputFile(handle, filename=Path(license_path).name))
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
        await query.edit_message_text(_start_help_text(), reply_markup=_main_menu_keyboard())
    elif data == "menu_products":
        await _send_products(update, context, edit=True)
    elif data == "menu_tools":
        await _send_products(update, context, edit=True, product_group="tool")
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
        package = _get_package_info(product_name, package_name)
        if not package or int(package["available_count"]) <= 0:
            await query.edit_message_text("Gói không hợp lệ.", reply_markup=_product_menu_keyboard())
            return
        await _send_quantity_choice(update, product_name, package_name, edit=True)
    elif data.startswith("qty:"):
        _, product_name, package_name, raw_qty = data.split(":", 3)
        if not _get_package_info(product_name, package_name):
            await query.edit_message_text("Gói không hợp lệ.", reply_markup=_product_menu_keyboard())
            return
        try:
            order = _create_sales_order(update, product_name, package_name, int(raw_qty))
        except InventoryReservationError as exc:
            await query.edit_message_text(str(exc), reply_markup=_product_menu_keyboard())
            return
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
    elif data.startswith("license_product_machine:"):
        _, product_id, machine_id = data.split(":", 2)
        await _create_paid_license_order(update, context, "", product_id, edit=True)
    elif data.startswith("license_product:"):
        product_id = data.split(":", 1)[1].strip().upper()
        await _create_paid_license_order(update, context, "", product_id, edit=True)
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
        "STORE_DB_PATH": os.environ.get("STORE_DB_PATH", "database/store.db").strip(),
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
    store_db_path = Path(cfg["STORE_DB_PATH"]).expanduser()
    if not store_db_path.is_absolute():
        store_db_path = PROJECT_ROOT / store_db_path
    try:
        _initialize_store_db(store_db_path)
    except sqlite3.Error as exc:
        raise SystemExit(f"Cannot initialize store database at {store_db_path}: {exc}") from exc

    app = Application.builder().token(cfg["BOT_TOKEN"]).post_init(post_init).build()
    app.bot_data["admin_ids"] = admin_ids
    app.bot_data["license_service"] = license_service
    app.bot_data["payment_service"] = payment_service
    app.bot_data["store_db_path"] = store_db_path
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
