from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


class FakeUser:
    id = 987654
    username = "new_partner"
    full_name = "New Partner"


def main() -> None:
    original_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    original_orders_path = bot.ORDERS_DB_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        bot.BUSINESS_PARTNERS_PATH = tmp_path / "business_partners.json"
        bot.ORDERS_DB_PATH = tmp_path / "orders_db.json"
        legacy_approved_at = "2026-06-01T00:00:00+00:00"
        legacy_data = {
            "partners": [
                {
                    "telegram_user_id": 123456,
                    "username": "legacy",
                    "full_name": "Legacy Partner",
                    "partner_code": "DTKD123456",
                    "referral_code": "AIDAILY123456",
                    "short_code": "AIDAILY123456",
                    "status": "approved",
                    "approved_at": legacy_approved_at,
                    "metrics_snapshot": {"withdrawable_balance": 999999999},
                }
            ],
            "referrals": [],
            "order_refs": [],
            "commissions": [
                {
                    "commission_id": "COM-LEGACY-1",
                    "order_id": "LEGACY-1",
                    "partner_telegram_user_id": 123456,
                    "partner_code": "DTKD123456",
                    "referral_code": "AIDAILY123456",
                    "amount": 200000,
                    "status": "approved",
                }
            ],
            "withdrawals": [
                {
                    "withdrawal_id": "WDR-LEGACY-1",
                    "partner_telegram_user_id": 123456,
                    "partner_code": "DTKD123456",
                    "amount": 50000,
                    "status": "paid",
                }
            ],
        }
        bot.BUSINESS_PARTNERS_PATH.write_text(json.dumps(legacy_data, ensure_ascii=False), encoding="utf-8")

        data = bot._load_business_partners()
        legacy = data["partners"][0]
        assert legacy["partner_id"] == "PTR0000123456"
        assert legacy["email"] is None
        assert legacy["region"] is None
        assert legacy["leader_partner_id"] is None
        assert legacy["parent_partner_id"] is None
        assert legacy["kpi_policy_id"] is None
        assert legacy["rank_policy_id"] is None
        assert legacy["bonus_policy_id"] is None
        assert legacy["approved_at"] == legacy_approved_at
        assert legacy["partner_code"] == "DTKD123456"
        assert legacy["referral_code"] == "AIDAILY123456"
        assert bot.find_partner_by_referral_code("AIDAILY123456")["partner_id"] == "PTR0000123456"
        assert bot.find_partner_by_id("PTR0000123456")["telegram_user_id"] == 123456

        metrics = bot._partner_metrics(legacy)
        assert metrics["available_commission"] == 150000
        snapshot = bot.update_partner_metrics_snapshot("PTR0000123456")
        assert snapshot is not None
        assert snapshot["withdrawable_balance"] == 150000

        new_partner = bot._ensure_business_partner(FakeUser(), {"phone": "0900000000"})
        assert new_partner["partner_id"] == "PTR0000987654"
        assert new_partner["email"] is None
        assert new_partner["metrics_snapshot"]["total_revenue"] == 0

    bot.BUSINESS_PARTNERS_PATH = original_business_partners_path
    bot.ORDERS_DB_PATH = original_orders_path
    print("DTKD_PARTNER_PROFILE_TEST=PASS")


if __name__ == "__main__":
    main()
