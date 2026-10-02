"""
tests/test_daily_report.py — Daily report R-multiples.

R = pnl_pct / entry-to-SL distance (%). The report must expose average R,
total R and PF in R for the day, the all-time block and the per-TF breakdown.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.daily_report import (
    _r_multiple,
    _r_stats,
    format_daily_report,
)


def _signal(price=100.0, sl=95.0, symbol="BTC/USDT", timeframe="4h"):
    return SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        signal_type="BUY",
        close_price=price,
        sl=sl,
        tp=115.0,
        sent_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _outcome(pnl_pct, status="HIT_TP", close_price=105.0, risk_pct=0.6):
    return SimpleNamespace(
        status=status,
        pnl_pct=pnl_pct,
        close_price=close_price,
        risk_pct=risk_pct,
        closed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        checked_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


class TestRMultiple:
    def test_pnl_over_sl_distance(self):
        # +5% PnL with a 5% wide SL -> exactly 1R
        assert _r_multiple(_outcome(5.0), _signal()) == pytest.approx(1.0)

    def test_loss_is_negative_r(self):
        assert _r_multiple(_outcome(-2.5), _signal()) == pytest.approx(-0.5)

    def test_unknown_pnl_excluded(self):
        assert _r_multiple(_outcome(None), _signal()) is None

    def test_missing_sl_excluded(self):
        assert _r_multiple(_outcome(5.0), _signal(sl=None)) is None

    def test_zero_width_sl_excluded(self):
        assert _r_multiple(_outcome(5.0), _signal(sl=100.0)) is None


class TestRStats:
    def test_aggregates_avg_total_pf(self):
        rows = [
            (_outcome(5.0), _signal()),    # +1.0R
            (_outcome(-2.5), _signal()),   # -0.5R
        ]
        stats = _r_stats(rows)
        assert stats["count"] == 2
        assert stats["total"] == pytest.approx(0.5)
        assert stats["avg"] == pytest.approx(0.25)
        assert stats["pf"] == pytest.approx(2.0)

    def test_empty_rows(self):
        assert _r_stats([]) == {"count": 0, "avg": None, "total": None, "pf": None}

    def test_rows_without_r(self):
        assert _r_stats([(_outcome(None), _signal())])["count"] == 0


class TestFormatDailyReport:
    def _data(self):
        rows = [
            (_outcome(5.0), _signal()),
            (_outcome(-2.5), _signal()),
        ]
        return {
            "date": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "closed_today": rows,
            "open_trades": [],
            "all_closed": rows,
        }

    def test_report_contains_r_metrics(self):
        report = format_daily_report(self._data())
        assert "| Средний R | +0.25R |" in report
        assert "| Суммарный R за день | +0.50R |" in report
        assert "| Profit Factor (в R) | 2.00 |" in report
        # detail table carries an R column with per-trade values
        assert "| R |" in report
        assert "+1.00R" in report
        assert "-0.50R" in report

    def test_all_time_block_contains_r_metrics(self):
        report = format_daily_report(self._data())
        assert "| Суммарный R | +0.50R |" in report
        assert "| Средний R |" in report
        # per-TF breakdown has an R column
        assert "| TF | Сделок | Винрейт | Средний PnL | Средний R | PF (цена) | PF (R) |" in report

    def test_r_rows_omitted_when_no_r(self):
        data = self._data()
        data["closed_today"] = [(_outcome(None), _signal())]
        data["all_closed"] = [(_outcome(None), _signal())]
        report = format_daily_report(data)
        # summary rows are conditional on having usable R values
        assert "| Суммарный R за день |" not in report
        assert "| Суммарный R |" not in report
        assert "| Profit Factor (в R) |" not in report


class TestAdxBucket:
    def test_boundaries(self):
        from analytics.daily_report import _adx_bucket
        assert _adx_bucket(None) == "—"
        assert _adx_bucket(19.9) == "<20"
        assert _adx_bucket(20.0) == "20-25"
        assert _adx_bucket(24.9) == "20-25"
        assert _adx_bucket(25.0) == "25-30"
        assert _adx_bucket(30.0) == ">=30"


class TestWrByFilterSlices:
    def _data_with_traces(self):
        rows = [
            (_outcome(5.0), _signal(symbol="BTC/USDT", timeframe="1h")),
            (_outcome(-2.5), _signal(symbol="ETH/USDT", timeframe="4h")),
            (_outcome(3.0), _signal(symbol="BTC/USDT", timeframe="1h")),
        ]
        return {
            "date": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "closed_today": rows,
            "open_trades": [],
            "all_closed": rows,
            "trace_by_signal": {
                None: {"regime": "trend", "adx": 27.0, "setup_type": "reversal"},
            },
            "gate_blocks": {"pattern_engine": 900, "cooldown": 120},
            "candidates_passed": 166,
        }

    def test_slice_tables_render(self):
        report = format_daily_report(self._data_with_traces())
        assert "### WR по фильтрам" in report
        for title in ("**Символ**", "**Направление**", "**Setup type**",
                      "**Режим рынка**", "**ADX-бакет**"):
            assert title in report
        # BTC has 2 of 3 trades and appears with its WR
        assert "| BTC/USDT | 2 |" in report
        assert "| Значение | Сделок | Винрейт | Средний PnL | Средний R | PF |" in report

    def test_trace_features_drive_setup_regime_adx(self):
        # signal.id is None here, so trace_by_signal[None] is a hit
        report = format_daily_report(self._data_with_traces())
        assert "| reversal | 3 |" in report
        assert "| trend | 3 |" in report
        assert "| 25-30 | 3 |" in report

    def test_gate_block_table_renders(self):
        report = format_daily_report(self._data_with_traces())
        assert "## Заблокировано гейтами (все кандидаты)" in report
        assert "| pattern_engine | 900 | 88.2% |" in report
        assert "| cooldown | 120 | 11.8% |" in report
        assert "Прошло кандидатов: 166" in report

    def test_gate_block_table_absent_without_data(self):
        data = self._data_with_traces()
        data.pop("gate_blocks")
        report = format_daily_report(data)
        assert "Заблокировано гейтами" not in report

    def test_slices_absent_without_closed_trades(self):
        data = self._data_with_traces()
        data["all_closed"] = []
        data["closed_today"] = []
        report = format_daily_report(data)
        assert "WR по фильтрам" not in report
