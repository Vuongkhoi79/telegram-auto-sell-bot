from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


PRODUCT_CODE = "CAPCUT_7D"
NEW_PASSWORD = "1234567"
EXPECTED_COUNT = 12
PRODUCTION_DB = Path("/var/data/store.db")


def _database_path(value: str | None) -> Path:
    raw = value or os.environ.get("STORE_DB_PATH") or str(PRODUCTION_DB)
    return Path(raw).expanduser()


def _require_columns(connection: sqlite3.Connection, table: str, columns: set[str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    missing = columns - existing
    if missing:
        raise RuntimeError(f"{table} missing required columns: {', '.join(sorted(missing))}")


def _replace_password(secret_value: str, new_password: str) -> str:
    parts = str(secret_value or "").split("|")
    if len(parts) < 2 or not parts[0].strip():
        raise ValueError("credential is not in email|password format")
    parts[1] = new_password
    return "|".join(parts)


def update_password(
    database: Path,
    *,
    product_code: str,
    new_password: str,
    expected_count: int,
    confirm: bool,
    allow_non_production_db: bool,
) -> int:
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    if database.as_posix() != PRODUCTION_DB.as_posix() and not allow_non_production_db:
        raise RuntimeError(
            f"Refusing to update non-production database: {database}. "
            f"Expected {PRODUCTION_DB} or pass --allow-non-production-db for a controlled local check."
        )
    if product_code.upper() != PRODUCT_CODE:
        raise RuntimeError(f"This repair script is locked to product_code={PRODUCT_CODE}")
    if new_password != NEW_PASSWORD:
        raise RuntimeError(f"This repair script is locked to password={NEW_PASSWORD}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_columns(connection, "products", {"id", "code", "active"})
        _require_columns(
            connection,
            "inventory_items",
            {
                "id",
                "product_id",
                "secret_value",
                "status",
                "reserved_order_id",
                "delivered_order_id",
                "reserved_at",
                "delivered_at",
            },
        )

        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT
                i.id,
                i.secret_value,
                i.status,
                i.reserved_order_id,
                i.delivered_order_id,
                i.reserved_at,
                i.delivered_at,
                p.code AS product_code
            FROM inventory_items AS i
            JOIN products AS p ON p.id = i.product_id
            WHERE UPPER(p.code) = ?
              AND p.active = 1
            ORDER BY i.created_at, i.id
            """,
            (product_code.upper(),),
        ).fetchall()

        affected = len(rows)
        print(f"Affected accounts before update: {affected}")
        print(f"Product: {product_code.upper()}")

        if affected != expected_count:
            connection.rollback()
            raise RuntimeError(
                f"Abort: expected exactly {expected_count} {product_code.upper()} accounts, found {affected}. No rows updated."
            )
        if not confirm:
            connection.rollback()
            raise RuntimeError("Abort: missing --confirm-update. No rows updated.")

        updated = 0
        for row in rows:
            before_parts = str(row["secret_value"] or "").split("|")
            new_secret = _replace_password(row["secret_value"], new_password)
            after_parts = new_secret.split("|")
            if before_parts[0] != after_parts[0]:
                raise RuntimeError(f"Refusing to change email for inventory_item id={row['id']}")
            if before_parts[2:] != after_parts[2:]:
                raise RuntimeError(f"Refusing to change 2FA/recovery fields for inventory_item id={row['id']}")

            cursor = connection.execute(
                """
                UPDATE inventory_items
                SET secret_value = ?
                WHERE id = ?
                  AND product_id IN (SELECT id FROM products WHERE UPPER(code) = ? AND active = 1)
                """,
                (new_secret, row["id"], product_code.upper()),
            )
            updated += int(cursor.rowcount or 0)

        after_rows = connection.execute(
            """
            SELECT
                i.id,
                i.secret_value,
                i.status,
                i.reserved_order_id,
                i.delivered_order_id,
                i.reserved_at,
                i.delivered_at
            FROM inventory_items AS i
            JOIN products AS p ON p.id = i.product_id
            WHERE UPPER(p.code) = ?
              AND p.active = 1
            ORDER BY i.created_at, i.id
            """,
            (product_code.upper(),),
        ).fetchall()
        if len(after_rows) != expected_count:
            raise RuntimeError("Post-update count changed unexpectedly; rolling back.")

        before_by_id = {row["id"]: row for row in rows}
        for row in after_rows:
            before = before_by_id[row["id"]]
            before_secret_parts = str(before["secret_value"] or "").split("|")
            after_secret_parts = str(row["secret_value"] or "").split("|")
            if len(after_secret_parts) < 2 or after_secret_parts[1] != new_password:
                raise RuntimeError(f"Password verification failed for inventory_item id={row['id']}")
            if before_secret_parts[0] != after_secret_parts[0] or before_secret_parts[2:] != after_secret_parts[2:]:
                raise RuntimeError(f"Credential fields other than password changed for inventory_item id={row['id']}")
            for field in ("status", "reserved_order_id", "delivered_order_id", "reserved_at", "delivered_at"):
                if before[field] != row[field]:
                    raise RuntimeError(f"Inventory field changed unexpectedly: {field} for id={row['id']}")

        if updated != expected_count:
            raise RuntimeError(f"Updated row count mismatch: expected {expected_count}, got {updated}")

        connection.commit()
        print(f"Updated: {updated}")
        print("Failed: 0")
        print(f"Product: {product_code.upper()}")
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely update only CAPCUT_7D inventory passwords in the production SQLite store."
    )
    parser.add_argument("--database", default=None, help="Path to store.db. Defaults to STORE_DB_PATH or /var/data/store.db.")
    parser.add_argument("--confirm-update", action="store_true", help="Required to apply the update.")
    parser.add_argument(
        "--allow-non-production-db",
        action="store_true",
        help="Allow running against a non-/var/data/store.db path for controlled local checks.",
    )
    args = parser.parse_args(argv)

    try:
        update_password(
            _database_path(args.database),
            product_code=PRODUCT_CODE,
            new_password=NEW_PASSWORD,
            expected_count=EXPECTED_COUNT,
            confirm=args.confirm_update,
            allow_non_production_db=args.allow_non_production_db,
        )
        return 0
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
