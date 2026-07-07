from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import telegram_license_bot as bot


def assert_rate(monthly_revenue: int, expected_rate: float) -> None:
    tier = bot._dtkd_commission_rate_for_monthly_revenue(monthly_revenue)
    actual_rate = float(tier["rate"])
    assert actual_rate == expected_rate, (monthly_revenue, actual_rate, expected_rate)
    assert actual_rate <= bot.DTKD_PARTNER_FUND_CAP_RATE, (monthly_revenue, actual_rate)


def main() -> None:
    assert_rate(5_000_000, 0.20)
    assert_rate(20_000_000, 0.22)
    assert_rate(50_000_000, 0.24)
    assert_rate(100_000_000, 0.26)
    assert_rate(200_000_000, 0.28)
    print("DTKD_COMMISSION_POLICY_TEST=PASS")


if __name__ == "__main__":
    main()
