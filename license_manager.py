import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if platform.system() == "Windows":
    import winreg
else:
    winreg = None

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
USER_DATA_DIR = APP_DIR / "user_data"
LICENSE_PATH = USER_DATA_DIR / "license.json"
PUBLIC_KEY_PATH = RESOURCE_DIR / "public_key.pem"
PERMANENT_EXPIRE_DATE = "2099-12-31"


def _run_wmic(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["wmic", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[1] if len(lines) >= 2 else ""


def _normalize_machine_fragment(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.strip().upper())


def _legacy_machine_id() -> str:
    fragments = [
        _run_wmic(["path", "win32_processor", "get", "processorid"]),
        _run_wmic(["path", "win32_operatingsystem", "get", "serialnumber"]),
        _run_wmic(["path", "win32_computersystemproduct", "get", "uuid"]),
    ]
    raw = "".join(_normalize_machine_fragment(fragment) for fragment in fragments if fragment)
    if not raw:
        return ""
    return re.sub(r"(.)\1{4,}", r"\1\1\1\1", raw)


def get_machine_id() -> str:
    legacy = _legacy_machine_id()
    if legacy:
        return legacy

    machine_guid = ""
    if winreg is not None:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            machine_guid = winreg.QueryValueEx(key, "MachineGuid")[0]
        except Exception:
            machine_guid = ""

    parts = [
        machine_guid,
        platform.node(),
        platform.system(),
        platform.machine(),
    ]
    raw = "|".join(part.strip().upper() for part in parts if part and part.strip())
    if not raw:
        raw = os.environ.get("COMPUTERNAME", "UNKNOWN_MACHINE")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    return "-".join(digest[i : i + 8] for i in range(0, 32, 8))


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_license_payload(
    machine_id: str,
    customer: str = "Customer",
    *,
    license_type: str = "free_90d",
    days: int = 90,
    expire_date: str | None = None,
    issued_at: str | None = None,
    created_at_utc: str | None = None,
) -> dict:
    issued = date.today()
    normalized_license_type = (license_type or "free_90d").strip().lower()
    if expire_date:
        normalized_expire_date = expire_date
    elif normalized_license_type == "permanent":
        normalized_expire_date = PERMANENT_EXPIRE_DATE
    else:
        normalized_expire_date = (issued + timedelta(days=days)).isoformat()

    return {
        "license_version": 1,
        "license_type": normalized_license_type,
        "customer": customer,
        "machine_id": machine_id.strip().upper(),
        "issued_at": issued_at or issued.isoformat(),
        "expire_date": normalized_expire_date,
        "duration_days": days if normalized_license_type != "permanent" else None,
        "created_at_utc": created_at_utc or utc_now_iso(),
    }


def package_license(payload: dict, private_key) -> dict:
    signature = private_key.sign(
        canonical_payload(payload),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def build_license_package(
    private_key,
    machine_id: str,
    customer: str = "Customer",
    *,
    license_type: str = "free_90d",
    days: int = 90,
    expire_date: str | None = None,
    issued_at: str | None = None,
    created_at_utc: str | None = None,
) -> dict:
    payload = build_license_payload(
        machine_id,
        customer,
        license_type=license_type,
        days=days,
        expire_date=expire_date,
        issued_at=issued_at,
        created_at_utc=created_at_utc,
    )
    return package_license(payload, private_key)


def _load_public_key():
    return serialization.load_pem_public_key(PUBLIC_KEY_PATH.read_bytes())


def load_license() -> dict | None:
    if not LICENSE_PATH.exists():
        return None
    try:
        return json.loads(LICENSE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_license(license_data: dict | None = None) -> tuple[bool, str, str]:
    data = license_data or load_license()
    if not data:
        return False, "Chua co license. Vui long kich hoat.", ""

    payload = data.get("payload")
    signature_b64 = data.get("signature")
    if not isinstance(payload, dict) or not signature_b64:
        return False, "License khong hop le.", ""

    current_machine_id = get_machine_id()
    if payload.get("machine_id") != current_machine_id:
        return False, "License khong dung may nay.", payload.get("expire_date", "")

    expire_date = payload.get("expire_date", "")
    license_type = str(payload.get("license_type", "free_90d")).strip().lower()
    try:
        expire = datetime.strptime(expire_date, "%Y-%m-%d").date()
    except Exception:
        return False, "Ngay het han license khong hop le.", expire_date

    if license_type != "permanent" and expire_date != PERMANENT_EXPIRE_DATE and date.today() > expire:
        return False, "License da het han, vui long gia han.", expire_date

    try:
        public_key = _load_public_key()
        public_key.verify(
            base64.b64decode(signature_b64),
            canonical_payload(payload),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return False, "Chu ky license khong hop le.", expire_date
    except Exception as exc:
        return False, f"Khong kiem tra duoc license: {exc}", expire_date

    return True, "License hop le.", expire_date


def save_license_text(license_text: str) -> tuple[bool, str, str]:
    try:
        data = json.loads(license_text)
    except json.JSONDecodeError:
        return False, "License phai la JSON hop le.", ""

    ok, message, expire_date = verify_license(data)
    if not ok:
        return False, message, expire_date

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True, "Kich hoat thanh cong.", expire_date


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
