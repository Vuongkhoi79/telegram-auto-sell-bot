from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from license_service import LicenseService
from telegram_license_bot import cmd_start, on_text_machine_id


MACHINE_ID = "BFEBFBFF000406E30033080000AA4234C4C4544005847108057B3C04F523732"


def make_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.texts: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.texts.append({"text": text, "reply_markup": reply_markup})

    async def reply_document(self, document=None, **kwargs):
        self.documents.append({"filename": getattr(document, "filename", "")})


class MachineIdEntryFlowTest(unittest.TestCase):
    def _make_context(self, license_service: LicenseService, args: list[str] | None = None):
        return SimpleNamespace(
            args=args or [],
            application=SimpleNamespace(bot_data={"license_service": license_service}),
        )

    def _make_update(self, message: FakeMessage):
        user = SimpleNamespace(id=123456, username="tester", full_name="Test User")
        return SimpleNamespace(effective_user=user, effective_message=message)

    def _assert_license_payload(self, license_path: Path) -> None:
        payload = json.loads(license_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["payload"]["machine_id"], MACHINE_ID)

    def test_start_with_machine_id_sends_license(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
                license_service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )
                update = self._make_update(FakeMessage())
                context = self._make_context(license_service, [MACHINE_ID])

                asyncio.run(cmd_start(update, context))

                self.assertEqual(len(update.effective_message.documents), 1)
                self.assertTrue(update.effective_message.texts)
                record = license_service.db.latest_license_by_machine(MACHINE_ID)
                self.assertIsNotNone(record)
                license_file = Path(record["license_file"])
                self.assertTrue(license_file.exists())
                self._assert_license_payload(license_file)
        finally:
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key

    def test_plain_machine_id_text_sends_license(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
                license_service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )
                message = FakeMessage(MACHINE_ID)
                update = self._make_update(message)
                context = self._make_context(license_service)

                asyncio.run(on_text_machine_id(update, context))

                self.assertEqual(len(message.documents), 1)
                self.assertTrue(message.texts)
                record = license_service.db.latest_license_by_machine(MACHINE_ID)
                self.assertIsNotNone(record)
                license_file = Path(record["license_file"])
                self.assertTrue(license_file.exists())
                self._assert_license_payload(license_file)
        finally:
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key

    def test_start_without_machine_id_shows_menu_only(self) -> None:
        previous_private_key = os.environ.get("PRIVATE_KEY_PEM")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                os.environ["PRIVATE_KEY_PEM"] = make_private_key_pem()
                license_service = LicenseService(
                    private_key_path=root / "unused.pem",
                    db_path=root / "licenses_db.json",
                    output_dir=root / "issued_licenses",
                )
                message = FakeMessage()
                update = self._make_update(message)
                context = self._make_context(license_service, [])

                asyncio.run(cmd_start(update, context))

                self.assertEqual(len(message.documents), 0)
                self.assertTrue(message.texts)
        finally:
            if previous_private_key is None:
                os.environ.pop("PRIVATE_KEY_PEM", None)
            else:
                os.environ["PRIVATE_KEY_PEM"] = previous_private_key


if __name__ == "__main__":
    unittest.main()
