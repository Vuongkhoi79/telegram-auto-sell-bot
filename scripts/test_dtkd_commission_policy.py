from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


def assert_affiliate_rate(monthly_revenue: int) -> None:
    policy = bot._dtkd_commission_rate_for_monthly_revenue(monthly_revenue)
    assert float(policy["rate"]) == 0.10, (monthly_revenue, policy)
    assert policy["name"] == "affiliate_10pct"


def assert_policy_engine_defaults() -> None:
    original_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    original_orders_path = bot.ORDERS_DB_PATH
    try:
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
                        "commission_policy_id": "DTKD_AFFILIATE_10PCT",
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
                        "gross_amount": 5_000_000,
                        "total_amount": 5_000_000,
                        "payable_amount": 5_000_000,
                        "net_revenue": 5_000_000,
                        "commission_amount": 500_000,
                        "rate": 0.10,
                        "commission_status": "pending_reconcile",
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
                        "status": "withdrawable",
                    }
                ],
                "withdrawals": [],
            }
            bot.BUSINESS_PARTNERS_PATH.write_text(json.dumps(seed_data, ensure_ascii=False), encoding="utf-8")

            data = bot._load_business_partners()
            policy = bot._dtkd_business_policy(data["partners"][0])
            config = policy["config"]
            assert policy["policy_id"] == "POL-DTKD-AFFILIATE-V1"
            assert policy["policy_type"] == "dtkd_affiliate"
            assert float(config["commission_rate"]) == 0.10
            assert int(config["reconcile_delay_hours"]) == 24
            assert str(config["commission_initial_status"]) == "pending_reconcile"
            assert str(config["commission_ready_status"]) == "withdrawable"
            assert "commission_tiers" not in config
            assert "fund_cap_rate" not in config
            assert bot._dtkd_withdraw_min_vnd(data["partners"][0]) == 100000

            order = {
                "order_id": "ORD-POLICY-NEW",
                "telegram_user_id": 222,
                "total": 5_000_000,
                "payment_status": "paid",
                "order_status": "delivered",
                "paid_at": "2026-07-07T00:00:00+00:00",
                "delivered_at": "2026-07-07T00:00:00+00:00",
                "created_at": "2026-07-07T00:00:00+00:00",
            }
            bot._record_partner_commission(order)
            updated = bot._load_business_partners()
            old_commission = next(item for item in updated["commissions"] if item["commission_id"] == "COM-OLD")
            new_commission = next(item for item in updated["commissions"] if item["commission_id"] == "COM-ORD-POLICY-NEW")
            assert old_commission["amount"] == 12345
            assert old_commission["status"] == "withdrawable"
            assert new_commission["amount"] == 500_000
            assert new_commission["commission_amount"] == 500_000
            assert new_commission["rate"] == 0.10
            assert new_commission["status"] == "withdrawable"
            assert new_commission["policy_snapshot"]["policy_id"] == "POL-DTKD-AFFILIATE-V1"
            assert new_commission["policy_snapshot"]["withdrawal_min_vnd"] == 100000
            assert new_commission["payable_amount"] == 5_000_000
            assert new_commission["net_revenue"] == 5_000_000
            assert new_commission["commission_deduction_mode"] == "affiliate"
    finally:
        bot.BUSINESS_PARTNERS_PATH = original_business_partners_path
        bot.ORDERS_DB_PATH = original_orders_path


def assert_monthly_campaign_configurable() -> None:
    original_business_partners_path = bot.BUSINESS_PARTNERS_PATH
    original_orders_path = bot.ORDERS_DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            bot.BUSINESS_PARTNERS_PATH = tmp_path / "business_partners.json"
            bot.ORDERS_DB_PATH = tmp_path / "orders_db.json"
            data = bot._load_business_partners()
            policy = data["business_policies"][0]
            policy["config"]["monthly_campaigns"] = [
                {
                    "month_key": "2026-08",
                    "thresholds": [
                        {"min_revenue": 20_000_000, "bonus_vnd": 300_000},
                        {"min_revenue": 50_000_000, "bonus_vnd": 1_000_000},
                        {"min_revenue": 100_000_000, "bonus_vnd": 5_000_000},
                    ],
                }
            ]
            data["partners"] = [
                {
                    "telegram_user_id": 111,
                    "partner_id": "PTR0000000111",
                    "partner_code": "DTKD000111",
                    "referral_code": "AIDAILY111",
                    "status": "approved",
                }
            ]
            data["order_refs"] = [
                {
                    "order_id": "ORD-CAMPAIGN",
                    "partner_id": "PTR0000000111",
                    "partner_telegram_user_id": 111,
                    "partner_code": "DTKD000111",
                    "referral_code": "AIDAILY111",
                    "gross_amount": 50_000_000,
                }
            ]
            bot._save_business_partners(data)
            bot.ORDERS_DB_PATH.write_text(
                json.dumps(
                    [
                        {
                            "order_id": "ORD-CAMPAIGN",
                            "telegram_user_id": 222,
                            "total": 50_000_000,
                            "payment_status": "paid",
                            "order_status": "delivered",
                            "paid_at": "2026-08-15T00:00:00+00:00",
                            "created_at": "2026-08-15T00:00:00+00:00",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            partner = bot._load_business_partners()["partners"][0]
            bonus = bot._dtkd_monthly_bonus_for_partner(partner, "2026-08")
            assert bonus["monthly_bonus_vnd"] == 1_000_000
            assert bonus["monthly_bonus_status"] == "pending_bonus_payment"
    finally:
        bot.BUSINESS_PARTNERS_PATH = original_business_partners_path
        bot.ORDERS_DB_PATH = original_orders_path


def main() -> None:
    assert_affiliate_rate(0)
    assert_affiliate_rate(20_000_000)
    assert_affiliate_rate(100_000_000)
    assert_policy_engine_defaults()
    assert_monthly_campaign_configurable()
    print("DTKD_COMMISSION_POLICY_TEST=PASS")


if __name__ == "__main__":
    main()
