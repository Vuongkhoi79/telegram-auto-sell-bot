from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import license_manager
from license_manager import PERMANENT_EXPIRE_DATE
from license_service import LIFETIME_PLAN, YEAR_365_PLAN, LicenseService


MACHINE_ID = "BFEBFBFF000406E30033080000AA4234C4C4544005847108057B3C04F523732"


def make_key_pair() -> tuple[str, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class PaidLicensePlansTest(unittest.TestCase):
    def test_year_365_license_payload(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        previous_public_key_path = license_manager.PUBLIC_KEY_PATH
        previous_get_machine_id = license_manager.get_machine_id
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                private_pem, public_pem = make_key_pair()
                os.environ["PRIVATE_KEY_PEM"] = private_pem
                public_key_path = root / "public_key.pem"
                public_key_path.write_bytes(public_pem)
                license_manager.PUBLIC_KEY_PATH = public_key_path
                license_manager.get_machine_id = lambda: MACHINE_ID
                service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )

                result = service.issue_paid_license(
                    123456,
                    "Test User",
                    MACHINE_ID,
                    plan=YEAR_365_PLAN,
                    customer="Test User",
                    order_id="TEST-YEAR",
                    payment_status="admin_grant",
                )

                self.assertTrue(result.ok)
                data = json.loads(Path(result.license_path).read_text(encoding="utf-8"))
                payload = data["payload"]
                self.assertEqual(payload["machine_id"], MACHINE_ID)
                self.assertEqual(payload["license_type"], "paid_365d")
                self.assertEqual(payload["plan"], YEAR_365_PLAN)
                self.assertEqual(payload["duration_days"], 365)
                self.assertEqual(payload["price_vnd"], 450000)
                self.assertFalse(payload["lifetime"])
                self.assertEqual(payload["expire_date"], (date.today() + timedelta(days=365)).isoformat())
                self.assertEqual(service.verify_license_file(result.license_path), (True, "License hop le.", payload["expire_date"]))
        finally:
            license_manager.PUBLIC_KEY_PATH = previous_public_key_path
            license_manager.get_machine_id = previous_get_machine_id
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key

    def test_lifetime_license_payload_uses_permanent_compatibility(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        previous_public_key_path = license_manager.PUBLIC_KEY_PATH
        previous_get_machine_id = license_manager.get_machine_id
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                private_pem, public_pem = make_key_pair()
                os.environ["PRIVATE_KEY_PEM"] = private_pem
                public_key_path = root / "public_key.pem"
                public_key_path.write_bytes(public_pem)
                license_manager.PUBLIC_KEY_PATH = public_key_path
                license_manager.get_machine_id = lambda: MACHINE_ID
                service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )

                result = service.issue_paid_license(
                    123456,
                    "Test User",
                    MACHINE_ID,
                    plan=LIFETIME_PLAN,
                    customer="Test User",
                    order_id="TEST-LIFETIME",
                    payment_status="admin_grant",
                )

                self.assertTrue(result.ok)
                data = json.loads(Path(result.license_path).read_text(encoding="utf-8"))
                payload = data["payload"]
                self.assertEqual(payload["machine_id"], MACHINE_ID)
                self.assertEqual(payload["license_type"], "permanent")
                self.assertEqual(payload["plan"], LIFETIME_PLAN)
                self.assertIsNone(payload["duration_days"])
                self.assertEqual(payload["price_vnd"], 990000)
                self.assertTrue(payload["lifetime"])
                self.assertEqual(payload["expire_date"], PERMANENT_EXPIRE_DATE)
                self.assertEqual(service.verify_license_file(result.license_path), (True, "License hop le.", payload["expire_date"]))
        finally:
            license_manager.PUBLIC_KEY_PATH = previous_public_key_path
            license_manager.get_machine_id = previous_get_machine_id
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
