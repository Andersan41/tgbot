"""
tests/test_ops.py — Тесты для операционных улучшений T3.

T3.2: error_channel_id в TelegramConfig + setup_error_sink.
T3.3: rate limiter (bot/rate_limit.py).
T3.4: Prometheus-метрики (monitoring/metrics.py).
"""
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Clear rate limiters between tests to avoid state leakage."""
    import bot.rate_limit as rl
    rl._limiters.clear()


# ── T3.2: error_channel_id ──────────────────────────────────────────

class TestErrorChannelId:
    def test_error_channel_id_default_empty(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ERROR_CHANNEL_ID", raising=False)
        import config.settings as settings
        importlib.reload(settings)
        assert settings.TelegramConfig().error_channel_id == ""

    def test_error_channel_id_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ERROR_CHANNEL_ID", "tg_error_ch_123")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.TelegramConfig().error_channel_id == "tg_error_ch_123"


class TestSetupErrorSink:
    def test_skips_when_no_error_channel(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ERROR_CHANNEL_ID", "")
        import config.settings as settings
        importlib.reload(settings)

        mock_bot = MagicMock()
        from config.logger import setup_error_sink
        setup_error_sink(mock_bot)
        # Не должно кидать, sink не добавлен
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_adds_sink_when_error_channel_set(self, monkeypatch):
        # Coroutine sink + enqueue=True requires a running event loop at
        # logger.add() time (main.py calls this inside async main()).
        monkeypatch.setenv("TELEGRAM_ERROR_CHANNEL_ID", "err_ch_42")
        import config.settings as settings
        importlib.reload(settings)

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        from loguru import logger
        from config.logger import setup_error_sink
        before = set(logger._core.handlers)  # handlers is {id: handler}
        try:
            setup_error_sink(mock_bot)
            added = set(logger._core.handlers) - before
            assert len(added) >= 1, "setup_error_sink must add a loguru sink"
        finally:
            # Do not leak a sink pointing at the mock into other tests
            for hid in set(logger._core.handlers) - before:
                logger.remove(hid)


# ── T3.3: rate limiter ──────────────────────────────────────────────

class TestRateLimiter:
    def test_get_limiter_returns_async_limiter(self):
        from bot.rate_limit import get_limiter
        from aiolimiter import AsyncLimiter
        limiter = get_limiter(12345)
        assert isinstance(limiter, AsyncLimiter)

    def test_same_user_gets_same_limiter(self):
        from bot.rate_limit import get_limiter
        l1 = get_limiter(12345)
        l2 = get_limiter(12345)
        assert l1 is l2

    def test_different_users_get_different_limiters(self):
        from bot.rate_limit import get_limiter
        l1 = get_limiter(111)
        l2 = get_limiter(222)
        assert l1 is not l2

    def test_limiter_config(self):
        from bot.rate_limit import get_limiter
        limiter = get_limiter(999)
        assert limiter.max_rate == 5
        assert limiter.time_period == 10

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_after_5(self):
        from bot.rate_limit import get_limiter
        limiter = get_limiter(100001)
        # aiolimiter's has_capacity() does NOT consume — acquire to drain
        # the 5-per-10s budget first.
        for _ in range(5):
            await limiter.acquire()
        # 6-й должен быть заблокирован
        assert not limiter.has_capacity()


# ── T3.4: Prometheus metrics ────────────────────────────────────────

class TestPrometheusMetrics:
    def test_metrics_module_importable(self):
        from monitoring import metrics
        assert hasattr(metrics, "signals_total")
        assert hasattr(metrics, "scan_duration_seconds")
        assert hasattr(metrics, "context_fetch_errors_total")

    def test_signals_total_is_counter(self):
        from monitoring.metrics import signals_total
        from prometheus_client import Counter
        assert isinstance(signals_total, Counter)

    def test_scan_duration_is_histogram(self):
        from monitoring.metrics import scan_duration_seconds
        from prometheus_client import Histogram
        assert isinstance(scan_duration_seconds, Histogram)

    def test_context_errors_is_counter(self):
        from monitoring.metrics import context_fetch_errors_total
        from prometheus_client import Counter
        assert isinstance(context_fetch_errors_total, Counter)

    def test_increment_signals_total(self):
        from monitoring.metrics import signals_total
        before = signals_total._metrics
        signals_total.labels(
            signal_type="BUY",
            symbol="BTC/USDT",
            timeframe="1h",
        ).inc()
        after = signals_total._metrics
        assert len(after) > len(before) or any(
            m._value.get() > 0 for m in after.values()
        )
