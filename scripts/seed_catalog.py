from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_license_bot import _initialize_store_db

CATALOG = [
    ("VEO3", "VEO3 ULTRA"), ("CHATGPT", "CHATGPT"), ("GEMINI", "GEMINI AI"), ("GROK", "GROK SUPER"),
    ("KLING", "KLING"), ("CAPCUT", "CAPCUT PRO"), ("HIGGSFIELD", "HIGGSFIELD PL..."),
    ("OPENART", "OPENART AI"), ("ELEVENLABS", "Elevenlabs"), ("SUNO", "SUNO AI"),
    ("CANVA", "CANVA"), ("HEYGEN", "HEYGEN AI"), ("CLAUDE", "CLAUDE AI"),
    ("GAMMA", "GAMMA AI"), ("CURSOR", "Cursor AI"), ("ADOBE", "ADOBE"),
    ("VIEWMAX", "viewmax"), ("ARTLIST", "ARTLIST"), ("KREA", "KREA AI"),
    ("DREAMINA", "Dreamina"), ("GMAIL", "GMAIL"),
]

def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "database" / "store.db"
    _initialize_store_db(path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as connection:
        for order, (key, label) in enumerate(CATALOG, start=1):
            code = f"CATALOG-{key}"
            connection.execute(
                """INSERT INTO products (id, code, name, active, delivery_type, created_at, updated_at, category_key, menu_order, show_in_menu, product_group, price_vnd)
                   VALUES (?, ?, ?, 1, 'account', ?, ?, ?, ?, 1, 'account', 0)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name, active=1, category_key=excluded.category_key, menu_order=excluded.menu_order, show_in_menu=1, product_group='account', updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), code, label, now, now, key, order),
            )
    print(f"Seeded {len(CATALOG)} catalog categories into {path}")

if __name__ == "__main__":
    main()
