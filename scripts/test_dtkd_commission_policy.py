from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


def assert_rate(monthly_revenue: int, expected_rate: float) -> None:
    tier = bot._dtkd_commission_rate_for_monthly_revenue(monthly_revenue)
    actual_rate = float(tier["rate"])
    assert actual_rate == expected_rate, (monthly_revenue, actual_rate, expected_rate)
    assert actual_rate <= bot.DTKD_PARTNER_FUND_CAP_RATE, (monthly_revenue, actual_rate)


def assert_policy_engine_defaults() -> None:
    original_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    original_orders_path = bot.ORDERS_DB_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        bot.BUSINESS_PARTNERS_PATH = tmp_path / "business_partners.json"
        bot.ORDERS_DB_PATH = tmp_path / "orders_db.json"
        seed_data = {
            "partners": [
                {
                    "telegram_user_id": 111,
                    "partner_id": "PTR0000000111",
                    "partner_code": "DTKD000111",
                    "referral_code": "AIDAILY111",
                    "status": "approved",
                    "approved_at": "2026-07-01T00:00:00+00:00",
                    "commission_policy_id": "DTKD_TRIAL_3M_REVENUE_TIERS",
                }
            ],
            "referrals": [],
            "order_refs": [
                {
                    "order_id": "ORD-POLICY-NEW",
                    "customer_telegram_user_id": 222,
                    "partner_id": "PTR0000000111",
                    "partner_telegram_user_id": 111,
                    "partner_code": "DTKD000111",
                    "referral_code": "AIDAILY111",
                }
            ],
            "commissions": [
                {
                    "commission_id": "COM-OLD",
                    "order_id": "ORD-OLD",
                    "partner_id": "PTR0000000111",
                    "partner_telegram_user_id": 111,
                    "partner_code": "DTKD000111",
                    "referral_code": "AIDAILY111",
                    "amount": 12345,
                    "status": "approved",
                }
            ],
            "withdrawals": [],
        }
        bot.BUSINESS_PARTNERS_PATH.write_text(json.dumps(seed_data, ensure_ascii=False), encoding="utf-8")

        data = bot._load_business_partners()
        policies = data["business_policies"]
        assert policies, "policy defaults should be seeded"
        policy = bot._dtkd_business_policy(data["partners"][0])
        config = policy["config"]
        assert float(config["fund_cap_rate"]) == 0.32
        assert int(config["trial_months"]) == 3
        assert str(config["commission_initial_status"]) == "pending_reconcile"
        assert bot._dtkd_withdraw_min_vnd(data["partners"][0]) == 100000

        order = {
            "order_id": "ORD-POLICY-NEW",
            "telegram_user_id": 222,
            "total": 5_000_000,
            "payment_status": "paid",
            "order_status": "delivered",
            "paid_at": "2026-07-07T00:00:00+00:00",
            "created_at": "2026-07-07T00:00:00+00:00",
        }
        bot._record_partner_commission(order)
        updated = bot._load_business_partners()
        old_commission = next(item for item in updated["commissions"] if item["commission_id"] == "COM-OLD")
        new_commission = next(item for item in updated["commissions"] if item["commission_id"] == "COM-ORD-POLICY-NEW")
        assert old_commission["amount"] == 12345
        assert old_commission["status"] == "approved"
        assert new_commission["amount"] == 1_000_000
        assert new_commission["rate"] == 0.20
        assert new_commission["fund_cap_rate"] == 0.32
        assert new_commission["status"] == "pending_reconcile"
        assert new_commission["policy_snapshot"]["policy_id"] == "POL-DTKD-COMMISSION-WITHDRAWAL-V1"
        assert new_commission["policy_snapshot"]["withdrawal_min_vnd"] == 100000

    bot.BUSINESS_PARTNERS_PATH = original_business_partners_path
    bot.ORDERS_DB_PATH = original_orders_path


def main() -> None:
    assert_rate(5_000_000, 0.20)
    assert_rate(20_000_000, 0.22)
    assert_rate(50_000_000, 0.24)
    assert_rate(100_000_000, 0.26)
    assert_rate(200_000_000, 0.28)
    assert_policy_engine_defaults()
    print("DTKD_COMMISSION_POLICY_TEST=PASS")


if __name__ == "__main__":
    main()
