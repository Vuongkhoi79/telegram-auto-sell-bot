from __future__ import annotations

import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from license_service import LicenseService


OLD_MACHINE = "F461FE60-342ADFEF-C1AE4E7B-A5FC97A4"
NEW_MACHINE = "BFEBFBFF000406E30033080000AA4234C4C4544005847108057B3C04F523732"


def _write_private_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "licenses_db.json"
        key_path = tmp_path / "private_key.pem"
        out_dir = tmp_path / "out"
        _write_private_key(key_path)

        db = {
            "schema_version": 1,
            "users": [
                {
                    "telegram_user_id": 123,
                    "username": "tester",
                    "machine_id": OLD_MACHINE,
                    "source": "seed",
                    "reminder_state": "active",
                    "last_seen_at": "2026-06-02T00:00:00+00:00",
                    "last_command_at": "2026-06-02T00:00:00+00:00",
                    "last_license_type": "free_90d",
                    "last_license_expire_date": "2026-09-01",
                    "last_license_path": "",
                    "next_reminder_at": "",
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                }
            ],
            "licenses": [
                {
                    "telegram_user_id": 123,
                    "username": "tester",
                    "machine_id": OLD_MACHINE,
                    "license_type": "free_90d",
                    "price": 0,
                    "order_id": "FREE-OLD",
                    "payment_status": "free",
                    "issued_at": "2026-06-02T00:00:00+00:00",
                    "expire_date": "2026-09-01",
                    "license_file": "",
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "customer": "tester",
                    "signature": "x",
                }
            ],
            "orders": [],
            "meta": {"created_at": "2026-06-02T00:00:00+00:00", "updated_at": "2026-06-02T00:00:00+00:00"},
        }
        db_path.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")

        service = LicenseService(key_path, db_path, out_dir)
        assert service.can_grant_free(123, NEW_MACHINE) is True, "new machine should be eligible"
        result = service.issue_free_license(123, "tester", NEW_MACHINE, customer="tester")
        assert result.ok is True, result.message
        assert result.record is not None
        assert result.record["machine_id"] == NEW_MACHINE

        payload = json.loads(Path(result.license_path).read_text(encoding="utf-8"))
        assert payload["payload"]["machine_id"] == NEW_MACHINE
        assert service.db.latest_license_by_machine(NEW_MACHINE) is not None

        removed = service.purge_machine(OLD_MACHINE)
        assert removed["licenses"] >= 1

        print("PASS free reissue by machine")


if __name__ == "__main__":
    main()
