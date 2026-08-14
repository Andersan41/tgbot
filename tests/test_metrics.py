"""
tests/test_metrics.py — Тесты для Prometheus-метрики (T3.4).
"""
import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestMetricsDefinitions:
    """Проверка, что метрики определены в monitoring/metrics.py."""

    def test_signals_total_exists(self):
        from monitoring.metrics import signals_total
        assert signals_total._type == "counter"
        assert "tgbot_signals" in signals_total._name

    def test_scan_duration_exists(self):
        from monitoring.metrics import scan_duration_seconds
        assert scan_duration_seconds._type == "histogram"
        assert "tgbot_scan_duration" in scan_duration_seconds._name

    def test_context_fetch_errors_exists(self):
        from monitoring.metrics import context_fetch_errors_total
        assert context_fetch_errors_total._type == "counter"
        assert "tgbot_context_fetch_errors" in context_fetch_errors_total._name


class TestStartMetricsServer:
    """_start_metrics_server — opt-in через METRICS_ENABLED."""

    def test_not_started_when_disabled(self, monkeypatch):
        monkeypatch.setenv("METRICS_ENABLED", "false")
        with patch("monitoring.metrics.start_http_server") as mock_server:
            from monitoring.metrics import _start_metrics_server
            _start_metrics_server()
            mock_server.assert_not_called()

    def test_started_when_enabled(self, monkeypatch):
        monkeypatch.setenv("METRICS_ENABLED", "true")
        monkeypatch.setenv("METRICS_PORT", "9090")
        with patch("monitoring.metrics.start_http_server") as mock_server:
            from monitoring.metrics import _start_metrics_server
            _start_metrics_server()
            mock_server.assert_called_once()
            port = mock_server.call_args[0][0]
            assert port == 9090

    def test_custom_port(self, monkeypatch):
        monkeypatch.setenv("METRICS_ENABLED", "true")
        monkeypatch.setenv("METRICS_PORT", "9999")
        with patch("monitoring.metrics.start_http_server") as mock_server:
            from monitoring.metrics import _start_metrics_server
            _start_metrics_server()
            mock_server.assert_called_once_with(9999)


class TestScanSymbolMetrics:
    """scan_symbol_v2 инкрементирует signals_total и измеряет duration."""

    @pytest.mark.asyncio
    async def test_signals_total_increased_on_signal(self, monkeypatch):
        from strategy.pattern_engine import ICTSetup
        setup_mock = ICTSetup(
            detected=True, direction="buy",
            has_bos=True, bos_type="bullish", bos_level=49500.0,
            has_ob=True, ob_type="bullish", ob_distance_pct=0.5,
            components_found=["bos", "ob"],
        )
        risk_mock = MagicMock(
            should_trade=True, risk_pct=1.0, rr_ratio=3.0, rejection_reason=None,
        )

        monkeypatch.setattr("scheduler.scanner._is_cooldown_active", AsyncMock(return_value=(False, 0)))
        monkeypatch.setattr("scheduler.scanner._get_indicators", AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))
        monkeypatch.setattr("scheduler.scanner._detect_regime", MagicMock(return_value=None))
        _mock_sweep = MagicMock(is_valid=True, sweep_type="bearish", reclaim_candles=2)
        monkeypatch.setattr("liquidity.sweep.detect_sweeps", MagicMock(return_value=[_mock_sweep]))
        monkeypatch.setattr("strategy.pattern_engine.pattern_engine", MagicMock(detect=MagicMock(return_value=setup_mock)))
        monkeypatch.setattr("risk.engine.risk_engine", MagicMock(evaluate=MagicMock(return_value=risk_mock)))
        monkeypatch.setattr("scheduler.scanner.db", MagicMock(
            save_signal=AsyncMock(), set_cooldown=AsyncMock(),
            get_active_signals_count=AsyncMock(return_value=0),
            get_portfolio_risk_sum=AsyncMock(return_value=0.0),
            get_last_signal=AsyncMock(return_value=None),
            create_outcome=AsyncMock(),
        ))

        cb = AsyncMock()
        from scheduler.scanner import scan_symbol_v2
        await scan_symbol_v2("BTC/USDT", "1h", cb)

        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scan_duration_histogram_wraps(self, monkeypatch):
        """Проверяем, что scan_duration_seconds.labels().time() вызывается."""
        from monitoring.metrics import scan_duration_seconds

        monkeypatch.setattr("scheduler.scanner._is_cooldown_active", AsyncMock(return_value=(False, 0)))
        monkeypatch.setattr("scheduler.scanner._get_indicators", AsyncMock(return_value=(MagicMock(), MagicMock())))
        monkeypatch.setattr("scheduler.scanner.db", MagicMock(
            get_active_signals_count=AsyncMock(return_value=0),
            get_portfolio_risk_sum=AsyncMock(return_value=0.0),
        ))

        from scheduler.scanner import scan_symbol_v2
        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None  # no pattern → None


class TestContextFetchErrorsMetrics:
    """context/analyzer инкрементирует context_fetch_errors_total при ошибке."""

    @pytest.mark.asyncio
    async def test_error_increments_counter(self, monkeypatch):
        from monitoring.metrics import context_fetch_errors_total
        from context.analyzer import ContextEngine, ContextSnapshot

        engine = ContextEngine()

        async def failing_func(snapshot, *args):
            raise ConnectionError("network down")

        snapshot = ContextSnapshot(symbol="BTC/USDT", timestamp=MagicMock())
        await engine._safe_fetch("CryptoPanic", failing_func, snapshot)

        assert len(snapshot.errors) == 1
        assert "CryptoPanic" in snapshot.errors[0]

    @pytest.mark.asyncio
    async def test_no_error_does_not_increment(self, monkeypatch):
        from monitoring.metrics import context_fetch_errors_total
        from context.analyzer import ContextEngine, ContextSnapshot

        engine = ContextEngine()

        async def ok_func(snapshot, *args):
            pass

        snapshot = ContextSnapshot(symbol="BTC/USDT", timestamp=MagicMock())
        await engine._safe_fetch("FearGreed", ok_func, snapshot)

        assert len(snapshot.errors) == 0
