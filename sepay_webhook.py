from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import bank_checker


PROJECT_ROOT = Path(__file__).resolve().parent
ORDERS_DB_PATH = PROJECT_ROOT / "orders_db.json"
PROCESSED_TRANSACTIONS_PATH = PROJECT_ROOT / "processed_transactions.json"
UNMATCHED_TRANSACTIONS_PATH = PROJECT_ROOT / "unmatched_transactions.json"
WEBHOOK_PATH = "/sepay-webhook"
HEALTH_PATH = "/health"

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
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def save_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_amount(payload: dict[str, Any]) -> int:
    for key in ("amount", "transferAmount", "transfer_amount", "money", "value"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            if digits:
                return int(digits)
    return 0


def extract_description(payload: dict[str, Any]) -> str:
    values = []
    for key in ("content", "description", "addInfo", "add_info", "transferContent", "transfer_content"):
        value = payload.get(key)
        if value:
            values.append(str(value))
    return " ".join(values)


def extract_transaction_id(payload: dict[str, Any]) -> str:
    for key in ("transaction_id", "transactionId", "id", "referenceCode", "reference_code", "code"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    description = extract_description(payload)
    amount = extract_amount(payload)
    digest = hashlib.sha256(f"{amount}|{description}".encode("utf-8")).hexdigest()[:16].upper()
    return f"SEPAY-{amount}-{digest}"


def normalize_order_token(value: str) -> str:
    return re.sub(r"[\s-]+", "", str(value or "")).upper()


def find_pending_order(description: str, amount: int) -> dict[str, Any] | None:
    normalized_description = normalize_order_token(description)
    for order in bank_checker.load_pending_orders():
        order_id = str(order.get("order_id", ""))
        if not order_id:
            continue
        normalized_order_id = normalize_order_token(order_id)
        if not normalized_order_id:
            continue
        if normalized_order_id not in normalized_description and order_id not in str(description):
            continue
        try:
            total = int(order.get("total", 0))
        except (TypeError, ValueError):
            total = 0
        if total == int(amount):
            return order
    return None


def append_unmatched(payload: dict[str, Any], reason: str) -> None:
    items = load_json_list(UNMATCHED_TRANSACTIONS_PATH)
    items.append({"payload": payload, "reason": reason, "created_at": utc_now_iso()})
    save_json_list(UNMATCHED_TRANSACTIONS_PATH, items)


async def process_sepay_payload(payload: dict[str, Any], fulfill_order: FulfillOrder) -> dict[str, Any]:
    print("[SEPAY] received transaction", flush=True)
    amount = extract_amount(payload)
    description = extract_description(payload)
    transaction_id = extract_transaction_id(payload)

    processed = load_json_list(PROCESSED_TRANSACTIONS_PATH)
    if transaction_id in {str(item.get("transaction_id", "")) for item in processed}:
        print("[SEPAY] duplicate transaction", flush=True)
        return {"ok": True, "status": "duplicate", "transaction_id": transaction_id}

    order = find_pending_order(description, amount)
    order_type = "order"
    if not order:
        print("[SEPAY] unmatched transaction", flush=True)
        append_unmatched(payload, "No pending order matched amount and description")
        return {"ok": False, "status": "unmatched", "transaction_id": transaction_id}

    print("[SEPAY] matched order", flush=True)
    order_id = str(order.get("order_id", ""))
    paid_at = utc_now_iso()
    bank_checker.update_order(
        order_id,
        payment_status="paid",
        order_status="paid",
        paid_at=paid_at,
        payment_method="SEPAY",
        transaction_id=transaction_id,
    )
    fulfillment = await fulfill_order(order_id)
    processed.append(
        {
            "transaction_id": transaction_id,
            "order_id": order_id,
            "amount": amount,
            "order_type": order_type,
            "processed_at": utc_now_iso(),
        }
    )
    save_json_list(PROCESSED_TRANSACTIONS_PATH, processed)
    return {
        "ok": True,
        "status": "paid",
        "transaction_id": transaction_id,
        "order_id": order_id,
        "order_type": order_type,
        "fulfillment": fulfillment,
    }


def start_sepay_webhook_server(application, fulfill_order: FulfillOrder, *, host: str, port: int) -> ThreadingHTTPServer:
    loop = asyncio.get_running_loop()

    class SePayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == HEALTH_PATH:
                self._send_json(200, {"ok": True, "bot": "Aidaily79", "time": utc_now_iso()})
                return
            if self.path == "/":
                self._send_json(200, {"ok": True, "service": "telegram-auto-sell-bot"})
                return
            self._send_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != WEBHOOK_PATH:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw_body = self.rfile.read(length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid_json"})
                return

            future = asyncio.run_coroutine_threadsafe(process_sepay_payload(payload, fulfill_order), loop)
            try:
                result = future.result(timeout=30)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, result)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), SePayHandler)
    thread = threading.Thread(target=server.serve_forever, name="sepay-webhook", daemon=True)
    thread.start()
    application.bot_data["sepay_webhook_server"] = server
    application.bot_data["sepay_webhook_url_path"] = WEBHOOK_PATH
    application.bot_data["health_url_path"] = HEALTH_PATH
    print(f"[SEPAY] webhook server listening on {host}:{port}{WEBHOOK_PATH}", flush=True)
    print(f"[HEALTH] health endpoint listening on {host}:{port}{HEALTH_PATH}", flush=True)
    return server
