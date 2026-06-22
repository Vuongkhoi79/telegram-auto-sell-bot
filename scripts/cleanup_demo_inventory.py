from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_PRODUCT_CODES = {"CHATGPT", "GEMINI", "GROK"}


def cleanup_demo_inventory(database_path: Path, demo_codes: set[str] | None = None) -> int:
    codes = {code.upper() for code in (demo_codes or DEMO_PRODUCT_CODES)}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        placeholders = ",".join("?" for _ in codes)
        rows = connection.execute(
            f"""
            SELECT i.id FROM inventory_items AS i
            JOIN products AS p ON p.id = i.product_id
            WHERE UPPER(p.code) IN ({placeholders})
              AND i.status = 'available'
              AND (
                    TRIM(COALESCE(p.note, '')) = ''
                    OR LOWER(p.note) LIKE '%demo%'
                    OR LOWER(p.note) LIKE '%template%'
                    OR LOWER(p.note) LIKE '%seed%'
                    OR LOWER(p.note) LIKE '%test%'
                    OR LOWER(p.note) LIKE '%example%'
              )
            """,
            tuple(sorted(codes)),
        ).fetchall()
        for (item_id,) in rows:
            connection.execute(
                "UPDATE inventory_items SET status = 'disabled', disabled_at = ? WHERE id = ?",
                (now, item_id),
            )
            connection.execute(
                "INSERT INTO inventory_movements (id, inventory_item_id, action, source, created_at) VALUES (?, ?, 'disable', 'cleanup_demo_inventory', ?)",
                (str(uuid.uuid4()), item_id, now),
            )
    return len(rows)


def main() -> None:
    raw_path = os.environ.get("STORE_DB_PATH", str(ROOT / "database" / "store.db"))
    path = Path(raw_path)
    if not path.is_file():
        raise SystemExit(f"store.db not found: {path}")
    disabled = cleanup_demo_inventory(path)
    print(f"Disabled {disabled} demo/template available inventory item(s).")


if __name__ == "__main__":
    main()
