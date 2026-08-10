"""Safely update Office 2024 product warranty metadata to lifetime.

Dry-run is the default. Use --yes to apply the metadata-only update.
This script never modifies inventory credentials, orders, payments, or history.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DATABASE = Path("/var/data/store.db")
OFFICE_CODE = "OFFICE_2024_LIFETIME"
OFFICE_NAME = "Microsoft Office LTSC 2024 Professional Plus"
OFFICE_PRICE_VND = 198000
OFFICE_DURATION = "LIFETIME"
OFFICE_INTERNAL_WARRANTY_DAYS = 0


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def backup_database(database_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = database_path.with_name(f"{database_path.name}.bak_before_office_lifetime_warranty_{timestamp}")
    shutil.copy2(database_path, backup_path)
    return backup_path


def load_product(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, code, name, duration, price_vnd, warranty_days, active
        FROM products
        WHERE UPPER(code) = ?
        """,
        (OFFICE_CODE,),
    ).fetchone()


def inventory_counts(connection: sqlite3.Connection, product_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT status, COUNT(*) AS qty
        FROM inventory_items
        WHERE product_id = ?
        GROUP BY status
        ORDER BY status
        """,
        (product_id,),
    ).fetchall()


def print_state(connection: sqlite3.Connection, label: str) -> sqlite3.Row:
    product = load_product(connection)
    print(f"{label}:")
    if not product:
        raise SystemExit(f"Product not found: {OFFICE_CODE}")
    print(
        "  product="
        f"code={product['code']} name={product['name']} duration={product['duration']} "
        f"price_vnd={product['price_vnd']} warranty_days={product['warranty_days']} active={product['active']}"
    )
    rows = inventory_counts(connection, str(product["id"]))
    if rows:
        for row in rows:
            print(f"  inventory status={row['status']} qty={row['qty']}")
    else:
        print("  inventory: none")
    return product


def apply_update(connection: sqlite3.Connection) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        UPDATE products
        SET name = COALESCE(NULLIF(name, ''), ?),
            duration = ?,
            price_vnd = ?,
            warranty_days = ?,
            active = 1,
            updated_at = ?
        WHERE UPPER(code) = ?
        """,
        (
            OFFICE_NAME,
            OFFICE_DURATION,
            OFFICE_PRICE_VND,
            OFFICE_INTERNAL_WARRANTY_DAYS,
            now,
            OFFICE_CODE,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Office 2024 warranty metadata to lifetime display.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Path to store.db")
    parser.add_argument("--yes", action="store_true", help="Apply changes. Without this flag, dry-run only.")
    args = parser.parse_args()

    database_path = args.database
    if not database_path.is_file():
        raise SystemExit(f"Database not found: {database_path}")

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        before = print_state(connection, "BEFORE")
        print("target:")
        print(
            f"  code={OFFICE_CODE} duration={OFFICE_DURATION} price_vnd={OFFICE_PRICE_VND} "
            f"warranty_days={OFFICE_INTERNAL_WARRANTY_DAYS} display_warranty=Trọn đời"
        )
        if not args.yes:
            print("dry_run=true")
            print("No changes written. Re-run with --yes to apply.")
            return 0

        backup_path = backup_database(database_path)
        print(f"backup={backup_path}")
        try:
            with connection:
                apply_update(connection)
        except Exception:
            print("update_failed=true")
            raise
        after = print_state(connection, "AFTER")

    changed = (
        before["duration"] != after["duration"]
        or int(before["price_vnd"] or 0) != int(after["price_vnd"] or 0)
        or int(before["warranty_days"] or 0) != int(after["warranty_days"] or 0)
        or int(before["active"] or 0) != int(after["active"] or 0)
    )
    print(f"changed={str(changed).lower()}")
    print("updated_scope=products metadata only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
