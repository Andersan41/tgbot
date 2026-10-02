"""
tests/test_budget_alert.py — Portfolio budget occupancy alert.

When used risk crosses BUDGET_ALERT_THRESHOLD of the cap, admins get ONE
notification; it re-arms only after usage drops below BUDGET_ALERT_RELEASE.
Under pytest the Telegram send is skipped (DB fixtures carry real values),
so tests drop PYTEST_CURRENT_TEST to observe the send itself.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import scanner


@pytest.fixture(autouse=True)
def _rearm_budget_alert():
    scanner._budget_alert_armed = True
    yield
    scanner._budget_alert_armed = True


@pytest.mark.asyncio
async def test_fires_once_per_fill_cycle(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    max_risk = scanner.config.max_portfolio_risk_pct
    with patch("bot.notifier.send_admin_alert", new_callable=AsyncMock) as send:
        # Cross the threshold repeatedly — only the first edge sends.
        for _ in range(5):
            await scanner._maybe_alert_budget(max_risk, max_risk, active_count=5)
        assert send.await_count == 1
        assert "Бюджет портфеля заня" in send.await_args.args[0]

        # Released below half — alert re-arms.
        await scanner._maybe_alert_budget(max_risk * 0.4, max_risk, active_count=2)
        assert send.await_count == 1

        # Filled again — second edge sends.
        await scanner._maybe_alert_budget(max_risk, max_risk, active_count=5)
        assert send.await_count == 2


@pytest.mark.asyncio
async def test_no_alert_below_threshold(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    max_risk = scanner.config.max_portfolio_risk_pct
    with patch("bot.notifier.send_admin_alert", new_callable=AsyncMock) as send:
        await scanner._maybe_alert_budget(max_risk * 0.8, max_risk, active_count=3)
        send.assert_not_awaited()
        assert scanner._budget_alert_armed is True


@pytest.mark.asyncio
async def test_send_skipped_under_pytest():
    # PYTEST_CURRENT_TEST is set by the runner itself
    with patch("bot.notifier.send_admin_alert", new_callable=AsyncMock) as send:
        await scanner._maybe_alert_budget(
            scanner.config.max_portfolio_risk_pct, scanner.config.max_portfolio_risk_pct
        )
        send.assert_not_awaited()
    # state still tracks the transition so live sends stay one-per-cycle
    assert scanner._budget_alert_armed is False


@pytest.mark.asyncio
async def test_disabled_when_cap_is_zero():
    with patch("bot.notifier.send_admin_alert", new_callable=AsyncMock) as send:
        await scanner._maybe_alert_budget(5.0, 0.0)
        send.assert_not_awaited()
    assert scanner._budget_alert_armed is True
