import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Prevent config.logger from running setup_logger() during import
import config.logger as _cfg_logger
_cfg_logger.setup_logger = lambda: None

# Import main with the real config ONCE, at collection time. The tests below
# re-import it inside patch("config.settings.config"), which is the only way
# for main.config to bind to the mock — but every dependency (storage.database
# builds its SQLAlchemy engine at import time) has to be cached already,
# otherwise the engine is built from a MagicMock and explodes.
import main  # noqa: F401,E402


@pytest.fixture(autouse=True)
def fresh_main():
    """Remove main from cache so each test gets a fresh import"""
    if "main" in sys.modules:
        del sys.modules["main"]
    yield


def _base_cfg(mock_cfg):
    mock_cfg.telegram.token = ""
    mock_cfg.telegram.channel_id = ""
    mock_cfg.telegram.admin_ids = []
    mock_cfg.exchange.name = "binance"
    mock_cfg.trading.symbols = ["BTC/USDT"]
    mock_cfg.trading.primary_timeframes = ["1h"]
    mock_cfg.trading.confirm_timeframe = "15m"
    mock_cfg.trading.candles_limit = 200
    mock_cfg.signal_cooldown_minutes = 60


class TestMainStartup:
    @pytest.mark.asyncio
    async def test_validates_token_presence(self):
        with (
            patch("config.settings.config") as mock_cfg,
            patch("main.send_error_alert", AsyncMock()),
            patch("main.logger"),
            # acquire_lock writes .trading_bot.lock with the pytest PID — the
            # next test would then see "bot already running" and exit early.
            patch("main.acquire_lock"),
        ):
            _base_cfg(mock_cfg)

            from main import main
            with pytest.raises(SystemExit) as exc:
                await main()
            assert exc.value.code == 1

    @pytest.mark.asyncio
    async def test_empty_token_logs_error(self):
        with (
            patch("config.settings.config") as mock_cfg,
            patch("main.logger") as mock_logger,
            patch("main.send_error_alert", AsyncMock()),
            patch("main.acquire_lock"),
        ):
            _base_cfg(mock_cfg)

            from main import main
            with pytest.raises(SystemExit):
                await main()
            mock_logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_startup_sequence(self):
        with (
            patch("config.settings.config") as mock_cfg,
            patch("main.acquire_lock"),
            patch("main.release_lock"),
            patch("main.db.init", AsyncMock()),
            patch("main.exchange_client.connect", AsyncMock()),
            patch("main.refresh_runtime_symbols", AsyncMock()),
            patch("main.start_web_server", AsyncMock(return_value=None)),
            patch("main.context_fetcher") as mock_fetcher,
            patch("config.settings.reload_filter_toggles", AsyncMock()),
            patch("config.logger.setup_error_sink"),
            patch("main.Application.builder") as mock_builder,
            patch("main.register_handlers"),
            patch("main.TaskScheduler") as mock_scheduler_cls,
            patch("main.send_error_alert", AsyncMock()),
            patch("main.logger"),
            # The real tracker loop would hit data/signals.db and the live
            # exchange from inside this test's event loop.
            patch("scheduler.outcome_tracker.outcome_tracker_loop", AsyncMock()),
        ):
            _base_cfg(mock_cfg)
            mock_cfg.telegram.token = "valid:token"
            mock_cfg.telegram.channel_id = "-1000000"
            mock_fetcher.close = AsyncMock()

            mock_updater = MagicMock()
            # KeyboardInterrupt is what Ctrl+C looks like inside main(): it is
            # caught around the polling loop and triggers a clean shutdown.
            # Never patch asyncio.sleep globally — that leaks a KeyboardInterrupt
            # into the rest of the pytest session.
            mock_updater.start_polling = AsyncMock(side_effect=KeyboardInterrupt)
            mock_updater.stop = AsyncMock()
            mock_app = MagicMock()
            mock_app.initialize = AsyncMock()
            mock_app.start = AsyncMock()
            mock_app.updater = mock_updater
            mock_app.stop = AsyncMock()
            mock_app.shutdown = AsyncMock()
            # Application.builder().token(...).request(...).build()
            mock_builder.return_value.token.return_value.request.return_value.build.return_value = mock_app

            mock_scheduler = MagicMock()
            mock_scheduler_cls.return_value = mock_scheduler

            from main import main
            await main()

            mock_updater.start_polling.assert_awaited()
            mock_scheduler.start.assert_called_once()
            mock_scheduler.stop.assert_called_once()
            mock_fetcher.close.assert_awaited()
