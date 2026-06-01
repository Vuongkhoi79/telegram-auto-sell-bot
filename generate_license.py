import argparse
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from license_manager import (
    PERMANENT_EXPIRE_DATE,
    build_license_package,
    utc_now_iso,
)


APP_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_PATH = APP_DIR / "private_key.pem"
PUBLIC_KEY_PATH = APP_DIR / "public_key.pem"


def init_keys():
    if PRIVATE_KEY_PATH.exists() or PUBLIC_KEY_PATH.exists():
        print("Key files already exist.")
        return

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    PRIVATE_KEY_PATH.write_bytes(private_pem)
    PUBLIC_KEY_PATH.write_bytes(public_pem)
    print(f"Created {PRIVATE_KEY_PATH}")
    print(f"Created {PUBLIC_KEY_PATH}")
    print("Keep private_key.pem only on admin machine.")


def load_private_key():
    if not PRIVATE_KEY_PATH.exists():
        raise SystemExit("Missing private_key.pem. Run: python generate_license.py --init-keys")
    return serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)


def generate_license(
    machine_id: str,
    customer: str,
    days: int,
    license_type: str = "free_90d",
    expire_date: str | None = None,
) -> dict:
    private_key = load_private_key()
    return build_license_package(
        private_key,
        machine_id,
        customer,
        license_type=license_type,
        days=days,
        expire_date=expire_date,
        created_at_utc=utc_now_iso(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-keys", action="store_true")
    parser.add_argument("--machine-id")
    parser.add_argument("--customer", default="Customer")
    parser.add_argument("--license-type", choices=["free_90d", "permanent"], default="free_90d")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--expire-date")
    parser.add_argument("--output", default="customer_license.json")
    args = parser.parse_args()

    if args.init_keys:
        init_keys()
        return
    if not args.machine_id:
        raise SystemExit("--machine-id is required")

    expire_date = args.expire_date
    if args.license_type == "permanent" and not expire_date:
        expire_date = PERMANENT_EXPIRE_DATE
    license_data = generate_license(args.machine_id, args.customer, args.days, args.license_type, expire_date)
    output_path = APP_DIR / args.output
    output_path.write_text(json.dumps(license_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Created license: {output_path}")
    print(f"Machine ID: {license_data['payload']['machine_id']}")
    print(f"Expire date: {license_data['payload']['expire_date']}")


if __name__ == "__main__":
    main()
