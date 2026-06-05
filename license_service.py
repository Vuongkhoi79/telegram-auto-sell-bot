from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization

from license_manager import (
    PERMANENT_EXPIRE_DATE,
    build_license_package,
    utc_now_iso,
    verify_license,
)


DEFAULT_FREE_DAYS = 90
DEFAULT_PAID_PRICE = 450_000
YEAR_365_PLAN = "YEAR_365"
YEAR_365_DAYS = 365
YEAR_365_PRICE = 450_000
LIFETIME_PLAN = "LIFETIME"
LIFETIME_PRICE = 990_000


LICENSE_PLANS = {
    YEAR_365_PLAN: {
        "label": "Gia hạn 1 năm",
        "license_type": "paid_365d",
        "duration_days": YEAR_365_DAYS,
        "price_vnd": YEAR_365_PRICE,
        "lifetime": False,
        "expire_date": None,
    },
    LIFETIME_PLAN: {
        "label": "Vĩnh viễn",
        "license_type": "permanent",
        "duration_days": 0,
        "price_vnd": LIFETIME_PRICE,
        "lifetime": True,
        "expire_date": PERMANENT_EXPIRE_DATE,
    },
}


def get_license_plan(plan: str | None) -> dict[str, Any]:
    normalized = str(plan or LIFETIME_PLAN).strip().upper()
    if normalized not in LICENSE_PLANS:
        normalized = LIFETIME_PLAN
    data = dict(LICENSE_PLANS[normalized])
    data["plan"] = normalized
    return data


def _slug(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value.strip("_") or "unknown"


@dataclass
class LicenseIssueResult:
    ok: bool
    message: str
    record: dict[str, Any] | None = None
    license_path: Path | None = None
    order: dict[str, Any] | None = None


class LicenseDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "users": [],
            "licenses": [],
            "orders": [],
            "meta": {
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        }
        self.load()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self.data["schema_version"] = loaded.get("schema_version", 1)
                        self.data["users"] = list(loaded.get("users", []))
                        self.data["licenses"] = list(loaded.get("licenses", []))
                        self.data["orders"] = list(loaded.get("orders", []))
                        meta = loaded.get("meta") if isinstance(loaded.get("meta"), dict) else {}
                        self.data["meta"].update(meta)
                except Exception:
                    pass
            self._normalize()
            return self.data

    def save(self) -> None:
        with self._lock:
            self._normalize()
            self.data["meta"]["updated_at"] = utc_now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.path)

    def _normalize(self) -> None:
        self.data.setdefault("schema_version", 1)
        self.data.setdefault("users", [])
        self.data.setdefault("licenses", [])
        self.data.setdefault("orders", [])
        self.data.setdefault(
            "meta",
            {
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )
        if not isinstance(self.data["users"], list):
            self.data["users"] = []
        if not isinstance(self.data["licenses"], list):
            self.data["licenses"] = []
        if not isinstance(self.data["orders"], list):
            self.data["orders"] = []
        if not isinstance(self.data["meta"], dict):
            self.data["meta"] = {
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }

    def add_license(self, record: dict[str, Any]) -> dict[str, Any]:
        self.data["licenses"].append(record)
        self.save()
        return record

    def upsert_user(self, record: dict[str, Any]) -> dict[str, Any]:
        telegram_user_id = int(record.get("telegram_user_id"))
        for existing in reversed(self.data["users"]):
            if int(existing.get("telegram_user_id", -1)) == telegram_user_id:
                existing.update(record)
                self.save()
                return existing
        self.data["users"].append(record)
        self.save()
        return record

    def latest_user(self, telegram_user_id: int) -> dict[str, Any] | None:
        for record in reversed(self.data["users"]):
            if int(record.get("telegram_user_id", -1)) == int(telegram_user_id):
                return record
        return None

    def add_order(self, record: dict[str, Any]) -> dict[str, Any]:
        self.data["orders"].append(record)
        self.save()
        return record

    def purge_machine(self, machine_id: str) -> dict[str, int]:
        normalized = machine_id.strip().upper()
        removed_users = 0
        removed_licenses = 0
        removed_orders = 0

        kept_users = []
        for record in self.data["users"]:
            if str(record.get("machine_id", "")).strip().upper() == normalized:
                removed_users += 1
                continue
            kept_users.append(record)
        self.data["users"] = kept_users

        kept_licenses = []
        for record in self.data["licenses"]:
            if str(record.get("machine_id", "")).strip().upper() == normalized:
                removed_licenses += 1
                continue
            kept_licenses.append(record)
        self.data["licenses"] = kept_licenses

        kept_orders = []
        for record in self.data["orders"]:
            if str(record.get("machine_id", "")).strip().upper() == normalized:
                removed_orders += 1
                continue
            kept_orders.append(record)
        self.data["orders"] = kept_orders

        if removed_users or removed_licenses or removed_orders:
            self.save()
        return {
            "users": removed_users,
            "licenses": removed_licenses,
            "orders": removed_orders,
        }

    def update_order(self, order_id: str, **changes: Any) -> dict[str, Any] | None:
        for order in reversed(self.data["orders"]):
            if order.get("order_id") == order_id:
                order.update(changes)
                self.save()
                return order
        return None

    def find_order(self, order_id: str) -> dict[str, Any] | None:
        for order in reversed(self.data["orders"]):
            if order.get("order_id") == order_id:
                return order
        return None

    def latest_license_by_machine(self, machine_id: str) -> dict[str, Any] | None:
        normalized = machine_id.strip().upper()
        for record in reversed(self.data["licenses"]):
            if record.get("machine_id") == normalized:
                return record
        return None

    def latest_license_by_user(self, telegram_user_id: int) -> dict[str, Any] | None:
        for record in reversed(self.data["licenses"]):
            if int(record.get("telegram_user_id", -1)) == int(telegram_user_id):
                return record
        return None

    def has_free_license_for_user(self, telegram_user_id: int) -> bool:
        for record in self.data["licenses"]:
            if int(record.get("telegram_user_id", -1)) == int(telegram_user_id) and record.get("license_type") == "free_90d":
                return True
        return False

    def has_free_license_for_machine(self, machine_id: str) -> bool:
        normalized = machine_id.strip().upper()
        for record in self.data["licenses"]:
            if record.get("machine_id") == normalized and record.get("license_type") == "free_90d":
                return True
        return False

    def has_active_pending_order(self, machine_id: str) -> dict[str, Any] | None:
        normalized = machine_id.strip().upper()
        for order in reversed(self.data["orders"]):
            if order.get("machine_id") == normalized and order.get("payment_status") == "pending":
                return order
        return None


class LicenseService:
    def __init__(
        self,
        private_key_path: Path,
        db_path: Path,
        output_dir: Path,
        *,
        free_days: int = DEFAULT_FREE_DAYS,
        paid_price: int = DEFAULT_PAID_PRICE,
    ):
        self.private_key_path = Path(private_key_path)
        self.db = LicenseDatabase(Path(db_path))
        self.output_dir = Path(output_dir)
        self.free_days = free_days
        self.paid_price = paid_price

    def _load_private_key(self):
        private_key_pem = os.environ.get("PRIVATE_KEY_PEM", "").strip()
        if private_key_pem:
            return serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not self.private_key_path.exists():
            raise FileNotFoundError(f"Missing private key: {self.private_key_path}")
        return serialization.load_pem_private_key(self.private_key_path.read_bytes(), password=None)

    def _make_license_file_path(self, machine_id: str, license_type: str, order_id: str) -> Path:
        safe_machine = _slug(machine_id)
        safe_type = _slug(license_type)
        safe_order = _slug(order_id)
        return self.output_dir / f"license_{safe_machine}_{safe_type}_{safe_order}.json"

    def _build_record(
        self,
        telegram_user_id: int,
        username: str,
        machine_id: str,
        *,
        license_type: str,
        price: int,
        order_id: str,
        payment_status: str,
        expire_date: str,
        license_path: Path,
        customer: str,
        plan: str | None = None,
        duration_days: int | None = None,
        lifetime: bool | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        record = {
            "telegram_user_id": int(telegram_user_id),
            "username": username or "",
            "machine_id": machine_id.strip().upper(),
            "license_type": license_type,
            "price": price,
            "order_id": order_id,
            "payment_status": payment_status,
            "issued_at": now,
            "expire_date": expire_date,
            "license_file": str(license_path),
            "created_at": now,
            "customer": customer or "",
        }
        if plan:
            record["plan"] = plan
        if duration_days is not None:
            record["duration_days"] = duration_days
        if lifetime is not None:
            record["lifetime"] = bool(lifetime)
        return record

    def _write_license_file(
        self,
        *,
        machine_id: str,
        customer: str,
        license_type: str,
        days: int,
        file_tag: str | None = None,
        expire_date: str | None = None,
        issued_at: str | None = None,
        created_at_utc: str | None = None,
        extra_payload: dict | None = None,
    ) -> tuple[dict[str, Any], Path]:
        private_key = self._load_private_key()
        package = build_license_package(
            private_key,
            machine_id,
            customer,
            license_type=license_type,
            days=days,
            expire_date=expire_date,
            issued_at=issued_at,
            created_at_utc=created_at_utc,
            extra_payload=extra_payload,
        )
        order_id = _slug(file_tag or uuid.uuid4().hex[:8])
        file_path = self._make_license_file_path(machine_id, license_type, order_id)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
        return package, file_path

    def issue_free_license(
        self,
        telegram_user_id: int,
        username: str,
        machine_id: str,
        *,
        customer: str | None = None,
    ) -> LicenseIssueResult:
        if self.db.has_free_license_for_machine(machine_id):
            existing = self.db.latest_license_by_machine(machine_id)
            return LicenseIssueResult(
                ok=False,
                message="Machine ID nay da nhan free 90 ngay.",
                record=existing,
            )

        order_id = f"FREE-{_slug(machine_id)}-{uuid.uuid4().hex[:8].upper()}"
        package, license_path = self._write_license_file(
            machine_id=machine_id,
            customer=customer or username or "Customer",
            license_type="free_90d",
            days=self.free_days,
            file_tag=order_id,
        )
        record = self._build_record(
            telegram_user_id,
            username,
            machine_id,
            license_type="free_90d",
            price=0,
            order_id=order_id,
            payment_status="free",
            expire_date=package["payload"]["expire_date"],
            license_path=license_path,
            customer=customer or username or "Customer",
        )
        record["signature"] = package["signature"]
        self.db.add_license(record)
        return LicenseIssueResult(ok=True, message="Free license 90 ngay da duoc cap.", record=record, license_path=license_path)

    def create_pending_order(
        self,
        telegram_user_id: int,
        username: str,
        machine_id: str,
        *,
        customer: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        plan_info = get_license_plan(plan)
        existing = self.db.has_active_pending_order(machine_id)
        if existing:
            if existing.get("plan", LIFETIME_PLAN) == plan_info["plan"]:
                return existing

        now = utc_now_iso()
        compact_time = now.replace("+00:00", "Z").replace("-", "").replace(":", "").replace("T", "")
        order_id = f"ORD-{compact_time}-{_slug(plan_info['plan'])[:4]}-{_slug(machine_id)[:8]}"
        order = {
            "order_id": order_id,
            "telegram_user_id": int(telegram_user_id),
            "username": username or "",
            "machine_id": machine_id.strip().upper(),
            "license_type": plan_info["license_type"],
            "plan": plan_info["plan"],
            "price": int(plan_info["price_vnd"]),
            "price_vnd": int(plan_info["price_vnd"]),
            "duration_days": plan_info["duration_days"],
            "lifetime": bool(plan_info["lifetime"]),
            "payment_status": "pending",
            "issued_at": "",
            "expire_date": "",
            "license_file": "",
            "created_at": now,
            "customer": customer or username or "Customer",
        }
        self.db.add_order(order)
        return order

    def issue_paid_license(
        self,
        telegram_user_id: int,
        username: str,
        machine_id: str,
        *,
        plan: str,
        customer: str | None = None,
        order_id: str | None = None,
        payment_status: str = "paid",
    ) -> LicenseIssueResult:
        plan_info = get_license_plan(plan)
        order_id = order_id or f"{plan_info['plan']}-{_slug(machine_id)}"
        package, license_path = self._write_license_file(
            machine_id=machine_id,
            customer=customer or username or "Customer",
            license_type=plan_info["license_type"],
            days=int(plan_info["duration_days"]),
            file_tag=order_id,
            expire_date=plan_info["expire_date"],
            extra_payload={
                "plan": plan_info["plan"],
                "price_vnd": int(plan_info["price_vnd"]),
                "lifetime": bool(plan_info["lifetime"]),
            },
        )
        record = self._build_record(
            telegram_user_id,
            username,
            machine_id,
            license_type=plan_info["license_type"],
            price=int(plan_info["price_vnd"]),
            order_id=order_id,
            payment_status=payment_status,
            expire_date=package["payload"]["expire_date"],
            license_path=license_path,
            customer=customer or username or "Customer",
            plan=plan_info["plan"],
            duration_days=plan_info["duration_days"],
            lifetime=plan_info["lifetime"],
        )
        record["signature"] = package["signature"]
        self.db.add_license(record)
        return LicenseIssueResult(ok=True, message=f"{plan_info['plan']} license da duoc cap.", record=record, license_path=license_path)

    def issue_permanent_license(
        self,
        telegram_user_id: int,
        username: str,
        machine_id: str,
        *,
        customer: str | None = None,
        order_id: str | None = None,
        payment_status: str = "paid",
    ) -> LicenseIssueResult:
        return self.issue_paid_license(
            telegram_user_id,
            username,
            machine_id,
            plan=LIFETIME_PLAN,
            customer=customer,
            order_id=order_id,
            payment_status=payment_status,
        )

    def mark_paid(self, order_id: str) -> LicenseIssueResult:
        order = self.db.find_order(order_id)
        if not order:
            return LicenseIssueResult(ok=False, message="Khong tim thay order.", order=None)
        if order.get("payment_status") == "paid":
            existing = self.db.latest_license_by_machine(str(order.get("machine_id", "")))
            return LicenseIssueResult(ok=True, message="Order da duoc thanh toan truoc do.", record=existing, order=order)

        self.db.update_order(order_id, payment_status="paid")
        result = self.issue_permanent_license(
            int(order["telegram_user_id"]),
            str(order.get("username", "")),
            str(order.get("machine_id", "")),
            customer=str(order.get("customer", "")),
            order_id=order_id,
            payment_status="paid",
        ) if not order.get("plan") else self.issue_paid_license(
            int(order["telegram_user_id"]),
            str(order.get("username", "")),
            str(order.get("machine_id", "")),
            plan=str(order.get("plan")),
            customer=str(order.get("customer", "")),
            order_id=order_id,
            payment_status="paid",
        )
        if result.record:
            self.db.update_order(
                order_id,
                payment_status="paid",
                issued_at=result.record.get("issued_at", ""),
                expire_date=result.record.get("expire_date", ""),
                license_file=result.record.get("license_file", ""),
                license_type=result.record.get("license_type", ""),
                plan=result.record.get("plan", ""),
            )
        return result

    def revoke(self, machine_id: str) -> LicenseIssueResult:
        record = self.db.latest_license_by_machine(machine_id)
        if not record:
            return LicenseIssueResult(ok=False, message="Khong tim thay license de revoke.")
        record["payment_status"] = "revoked"
        record["revoked_at"] = utc_now_iso()
        self.db.save()
        return LicenseIssueResult(ok=True, message="Da revoke license.", record=record)

    def find(self, machine_id: str) -> dict[str, Any] | None:
        return self.db.latest_license_by_machine(machine_id)

    def list_licenses(self) -> list[dict[str, Any]]:
        return list(self.db.data.get("licenses", []))

    def recent_machine_id_for_user(self, telegram_user_id: int) -> str:
        user_record = self.db.latest_user(telegram_user_id) or {}
        machine_id = str(user_record.get("machine_id", "")).strip().upper()
        if machine_id:
            return machine_id

        license_record = self.db.latest_license_by_user(telegram_user_id) or {}
        machine_id = str(license_record.get("machine_id", "")).strip().upper()
        if machine_id:
            return machine_id

        return ""

    def touch_user(
        self,
        telegram_user_id: int,
        username: str,
        *,
        machine_id: str | None = None,
        source: str | None = None,
        reminder_state: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        current = self.db.latest_user(telegram_user_id) or {}
        record = {
            "telegram_user_id": int(telegram_user_id),
            "username": username or current.get("username", ""),
            "machine_id": (machine_id or current.get("machine_id") or "").strip().upper(),
            "source": source or current.get("source", ""),
            "reminder_state": reminder_state or current.get("reminder_state", current.get("status", "unknown")),
            "last_seen_at": now,
            "last_command_at": now,
            "last_license_type": current.get("last_license_type", ""),
            "last_license_expire_date": current.get("last_license_expire_date", ""),
            "last_license_path": current.get("last_license_path", ""),
            "next_reminder_at": current.get("next_reminder_at", ""),
            "created_at": current.get("created_at", now),
            "updated_at": now,
        }
        return self.db.upsert_user(record)

    def update_user_from_license(self, record: dict[str, Any], *, source: str = "license") -> dict[str, Any]:
        now = utc_now_iso()
        user_record = {
            "telegram_user_id": int(record.get("telegram_user_id")),
            "username": record.get("username", ""),
            "machine_id": str(record.get("machine_id", "")).strip().upper(),
            "source": source,
            "reminder_state": "active",
            "last_seen_at": now,
            "last_command_at": now,
            "last_license_type": record.get("license_type", ""),
            "last_license_expire_date": record.get("expire_date", ""),
            "last_license_path": record.get("license_file", ""),
            "next_reminder_at": record.get("expire_date", ""),
            "created_at": now,
            "updated_at": now,
        }
        return self.db.upsert_user(user_record)

    def can_grant_free(self, telegram_user_id: int, machine_id: str) -> bool:
        return not self.db.has_free_license_for_machine(machine_id)

    def free_or_paid_status(self, machine_id: str) -> dict[str, Any] | None:
        return self.db.latest_license_by_machine(machine_id)

    def purge_machine(self, machine_id: str) -> dict[str, int]:
        return self.db.purge_machine(machine_id)

    def verify_license_file(self, license_file: Path | str) -> tuple[bool, str, str]:
        path = Path(license_file)
        if not path.exists():
            return False, "License file khong ton tai.", ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False, "License file khong hop le.", ""
        return verify_license(data)
