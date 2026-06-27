from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from repository.store_repository import StoreRepository


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
ORDERS_DB_PATH = PROJECT_ROOT / "orders_db.json"
BANK_TRANSACTIONS_PATH = PROJECT_ROOT / "bank_transactions.json"
PROCESSED_TRANSACTIONS_PATH = PROJECT_ROOT / "processed_transactions.json"

FulfillOrder = Callable[[str], Awaitable[dict[str, Any]]]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def save_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_store_db_path() -> Path:
    path = Path(os.environ.get("STORE_DB_PATH", "database/store.db")).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _orders_repository() -> StoreRepository | None:
    path = _resolve_store_db_path()
    if not path.is_file():
        return None
    try:
        return StoreRepository(path)
    except (OSError, RuntimeError, sqlite3.Error):
        return None


def load_orders() -> list[dict[str, Any]]:
    path = _resolve_store_db_path()
    repository = _orders_repository()
    if repository:
        try:
            return repository.list_orders()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order lookup failed; account sales fail closed: %s", exc)
            return []
    if path.is_file():
        return []
    return load_json_list(ORDERS_DB_PATH)


def load_pending_orders() -> list[dict[str, Any]]:
    path = _resolve_store_db_path()
    repository = _orders_repository()
    if repository:
        try:
            return [
                order
                for order in repository.list_pending_orders()
                if not is_expired(order)
            ]
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite pending order lookup failed; account sales fail closed: %s", exc)
            return []
    if path.is_file():
        return []
    return [
        order
        for order in load_json_list(ORDERS_DB_PATH)
        if order.get("payment_status") == "pending" and not is_expired(order)
    ]


def is_expired(order: dict[str, Any]) -> bool:
    expire_at = str(order.get("expire_at", ""))
    if not expire_at:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(expire_at)
    except ValueError:
        return False


def update_order(order_id: str, **changes: Any) -> dict[str, Any] | None:
    if "payment_status" in changes and "status" not in changes:
        changes["status"] = changes["payment_status"]
    path = _resolve_store_db_path()
    repository = _orders_repository()
    if repository:
        try:
            updated = repository.update_order(order_id, **changes)
            if updated:
                return updated
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.error("SQLite order update failed for %s; account sales fail closed: %s", order_id, exc)
            return None
    if path.is_file():
        return None
    orders = load_json_list(ORDERS_DB_PATH)
    for order in reversed(orders):
        if str(order.get("order_id", "")).upper() == order_id.upper():
            order.update(changes)
            save_json_list(ORDERS_DB_PATH, orders)
            return order
    return None


def load_transactions(provider: str = "manual") -> list[dict[str, Any]]:
    if provider == "manual":
        return load_json_list(BANK_TRANSACTIONS_PATH)
    if provider in {"webhook", "api"}:
        return load_json_list(BANK_TRANSACTIONS_PATH)
    return []


def find_matching_transaction(
    order: dict[str, Any],
    transactions: list[dict[str, Any]],
    processed_ids: set[str],
) -> dict[str, Any] | None:
    order_id = str(order.get("order_id", ""))
    total = int(order.get("total", 0))
    for tx in transactions:
        tx_id = str(tx.get("transaction_id", "")).strip()
        if not tx_id or tx_id in processed_ids:
            continue
        try:
            amount = int(tx.get("amount", 0))
        except (TypeError, ValueError):
            continue
        description = str(tx.get("description", ""))
        if amount == total and order_id and order_id in description:
            return tx
    return None


async def check_bank_transactions(
    fulfill_order: FulfillOrder,
    *,
    provider: str = "manual",
) -> list[dict[str, Any]]:
    orders = load_pending_orders()
    transactions = load_transactions(provider)
    processed = load_json_list(PROCESSED_TRANSACTIONS_PATH)
    processed_ids = {str(item.get("transaction_id", "")) for item in processed}
    results: list[dict[str, Any]] = []

    for order in orders:
        tx = find_matching_transaction(order, transactions, processed_ids)
        if not tx:
            continue

        order_id = str(order.get("order_id", ""))
        tx_id = str(tx.get("transaction_id", ""))
        paid_at = utc_now_iso()
        update_order(
            order_id,
            payment_method=str(tx.get("bank", "ACB") or "ACB"),
            payment_status="paid",
            paid_at=paid_at,
        )
        fulfillment = await fulfill_order(order_id)
        processed_record = {
            "transaction_id": tx_id,
            "order_id": order_id,
            "amount": int(tx.get("amount", 0)),
            "processed_at": utc_now_iso(),
        }
        processed.append(processed_record)
        processed_ids.add(tx_id)
        save_json_list(PROCESSED_TRANSACTIONS_PATH, processed)
        results.append(
            {
                "order_id": order_id,
                "transaction_id": tx_id,
                "amount": int(tx.get("amount", 0)),
                "fulfillment": fulfillment,
            }
        )

    return results
