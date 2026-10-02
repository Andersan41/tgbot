import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestModuleStructure:
    """Проверяем что все модули существуют в ожидаемых местах"""

    def test_config_module_exists(self):
        from config import settings
        from config import logger as config_logger
        assert settings is not None
        assert config_logger is not None

    def test_data_module_exists(self):
        from data import exchange_client
        assert exchange_client is not None

    def test_indicators_module_exists(self):
        from indicators import engine
        assert engine is not None

    def test_strategy_module_exists(self):
        import importlib
        mod = importlib.import_module("strategy.signal_engine")
        assert mod is not None

    def test_scheduler_module_exists(self):
        from scheduler import scanner
        from scheduler import tasks
        assert scanner is not None
        assert tasks is not None

    def test_context_module_exists(self):
        from context import analyzer
        from context import scorer
        from context import fetcher
        assert analyzer is not None
        assert scorer is not None
        assert fetcher is not None

    def test_bot_module_exists(self):
        from bot import handlers
        from bot import notifier
        from bot import menu
        assert handlers is not None
        assert notifier is not None
        assert menu is not None

    def test_storage_module_exists(self):
        from storage import database
        assert database is not None


class TestSingletons:
    """Проверяем что ключевые компоненты — синглтоны"""

    def test_config_singleton(self):
        from config.settings import config as config1
        from config import settings
        config2 = settings.config
        assert config1 is config2

    @staticmethod
    def _fresh_import(prefix: str):
        """Re-import module with `prefix` fresh, restoring the original after.

        Deleting from sys.modules without restoring poisons every other test:
        they patch the new module while holding bindings to the old one.
        """
        import contextlib
        import sys

        @contextlib.contextmanager
        def _ctx():
            saved = {m: sys.modules.pop(m) for m in list(sys.modules)
                     if m.startswith(prefix)}
            try:
                yield
            finally:
                sys.modules.update(saved)

        return _ctx()

    def test_exchange_client_singleton(self):
        import sys
        with self._fresh_import("data.exchange_client"):
            from data.exchange_client import exchange_client as ec1
            import data.exchange_client as ec_mod
            ec2 = ec_mod.exchange_client
            assert ec1 is ec2

    def test_indicator_engine_singleton(self):
        with self._fresh_import("indicators.engine"):
            from indicators.engine import indicator_engine as ie1
            import indicators.engine as ie_mod
            ie2 = ie_mod.indicator_engine
            assert ie1 is ie2

    def test_signal_engine_types_exist(self):
        from strategy.signal_engine import SignalType, SignalResult, _calculate_sl_tp
        assert SignalType.BUY == "BUY"
        assert SignalType.SELL == "SELL"
        assert hasattr(SignalResult, "is_actionable")
        assert callable(_calculate_sl_tp)

    def test_db_singleton(self):
        with self._fresh_import("storage.database"):
            from storage.database import db as db1
            import storage.database as db_mod
            db2 = db_mod.db
            assert db1 is db2

    def test_context_engine_singleton(self):
        with self._fresh_import("context.analyzer"):
            from context.analyzer import context_engine as ce1
            import context.analyzer as ce_mod
            ce2 = ce_mod.context_engine
            assert ce1 is ce2


class TestConfigDataclass:
    """Проверяем что config — dataclass с ожидаемыми полями"""

    def test_config_is_appconfig(self):
        from config.settings import AppConfig, config
        assert isinstance(config, AppConfig)

    def test_config_has_telegram(self):
        from config.settings import config
        assert hasattr(config, "telegram")
        assert config.telegram is not None

    def test_config_has_exchange(self):
        from config.settings import config
        assert hasattr(config, "exchange")
        assert config.exchange is not None

    def test_config_has_trading(self):
        from config.settings import config
        assert hasattr(config, "trading")
        assert config.trading is not None

    def test_config_has_database_url(self):
        from config.settings import config
        assert hasattr(config, "database_url")
        assert isinstance(config.database_url, str)

    def test_config_has_log_settings(self):
        from config.settings import config
        assert hasattr(config, "log_level")
        assert hasattr(config, "log_file")

    def test_config_has_cooldown(self):
        from config.settings import config
        assert hasattr(config, "signal_cooldown_minutes")
        assert isinstance(config.signal_cooldown_minutes, int)

    def test_config_has_context_settings(self):
        from config.settings import config
        assert hasattr(config, "context_enabled")
        assert hasattr(config, "context_min_verdict")
        assert hasattr(config, "coingecko_symbol_map")


class TestPipelineStages:
    """
    Проверяем что реализован полный пайплайн:
    Fetch → Calculate → Evaluate → Confirm → Enrich → Save → Notify
    """

    def test_fetch_stage_exchange_client(self):
        """Fetch: exchange_client.fetch_ohlcv"""
        from data.exchange_client import ExchangeClient
        assert hasattr(ExchangeClient, "fetch_ohlcv")
        assert hasattr(ExchangeClient, "connect")
        assert hasattr(ExchangeClient, "close")

    def test_calculate_stage_indicator_engine(self):
        """Calculate: indicator_engine.calculate"""
        from indicators.engine import IndicatorEngine, IndicatorValues
        assert hasattr(IndicatorEngine, "calculate")
        assert hasattr(IndicatorValues, "ema_bullish_cross")
        assert hasattr(IndicatorValues, "ema_bearish_alignment")

    def test_evaluate_stage_signal_engine(self):
        """Evaluate: signal_engine.evaluate"""
        from strategy.signal_engine import SignalType, SignalResult
        assert SignalType.BUY == "BUY"
        assert SignalType.SELL == "SELL"
        assert SignalType.NO_SIGNAL == "NO_SIGNAL"
        assert hasattr(SignalResult, "is_actionable")
        assert hasattr(SignalResult, "format_message")

    def test_confirm_stage_scanner(self):
        """Confirm: scanner uses ICT pattern engine"""
        from scheduler.scanner import scan_symbol_v2
        import inspect
        source = inspect.getsource(scan_symbol_v2)
        assert "pattern_engine" in source.lower()

    def test_enrich_stage_context(self):
        """Enrich: context_engine.get_snapshot"""
        from context.analyzer import ContextEngine, ContextSnapshot
        assert hasattr(ContextEngine, "get_snapshot")
        assert hasattr(ContextSnapshot, "to_json")

    def test_save_stage_database(self):
        """Save: db.save_signal"""
        from storage.database import Database, Signal
        assert hasattr(Database, "save_signal")
        assert hasattr(Database, "get_recent_signals")
        assert hasattr(Signal, "symbol")
        assert hasattr(Signal, "signal_type")
        assert hasattr(Signal, "sl")
        assert hasattr(Signal, "tp")

    def test_notify_stage_notifier(self):
        """Notify: send_signal в Telegram"""
        from bot.notifier import send_signal, send_error_alert
        import inspect
        assert inspect.iscoroutinefunction(send_signal)
        assert inspect.iscoroutinefunction(send_error_alert)


class TestAsyncPatterns:
    """Проверяем что I/O операции асинхронные"""

    def test_exchange_client_methods_are_async(self):
        from data.exchange_client import ExchangeClient
        import inspect
        assert inspect.iscoroutinefunction(ExchangeClient.connect)
        assert inspect.iscoroutinefunction(ExchangeClient.close)
        assert inspect.iscoroutinefunction(ExchangeClient.fetch_ohlcv)
        assert inspect.iscoroutinefunction(ExchangeClient.fetch_all_symbols)

    def test_database_methods_are_async(self):
        from storage.database import Database
        import inspect
        assert inspect.iscoroutinefunction(Database.init)
        assert inspect.iscoroutinefunction(Database.save_signal)
        assert inspect.iscoroutinefunction(Database.get_recent_signals)
        assert inspect.iscoroutinefunction(Database.get_last_signal)

    def test_scanner_functions_are_async(self):
        from scheduler.scanner import scan_symbol_v2, run_scan_cycle
        import inspect
        assert inspect.iscoroutinefunction(scan_symbol_v2)
        assert inspect.iscoroutinefunction(run_scan_cycle)

    def test_bot_handlers_are_async(self):
        from bot.handlers import cmd_start, cmd_help, cmd_status
        import inspect
        assert inspect.iscoroutinefunction(cmd_start)
        assert inspect.iscoroutinefunction(cmd_help)
        assert inspect.iscoroutinefunction(cmd_status)


class TestMainEntry:
    """Проверяем main.py как точку входа"""

    def test_main_function_exists(self):
        from main import main
        import inspect
        assert inspect.iscoroutinefunction(main)

    def test_main_imports_all_components(self):
        """main.py импортирует все ключевые модули"""
        import inspect
        from main import main
        source = inspect.getsource(main)
        assert "db.init" in source
        assert "exchange_client.connect" in source
        assert "register_handlers" in source
        assert "TaskScheduler" in source

    def test_main_asyncio_run(self):
        import inspect
        import main
        source = inspect.getsource(main)
        assert "asyncio.run" in source


class TestIndicatorValuesProperties:
    """Проверяем вычисляемые свойства IndicatorValues"""

    def test_ema_bullish_cross_property(self):
        from indicators.engine import IndicatorValues
        iv = IndicatorValues(
            symbol="BTC/USDT", timeframe="1h",
            close=100, high=105, low=95, volume=1000,
            ema_fast=100, ema_slow=99, ema_trend=98,
            ema_fast_prev=98, ema_slow_prev=99,
            rsi=55, macd=1, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=-0.5,
            adx=30, dmi_plus=30, dmi_minus=15,
            atr=2, supertrend=95, supertrend_direction=1,
            volume_sma=800,
        )
        assert iv.ema_bullish_cross is True

    def test_ema_bearish_cross_property(self):
        from indicators.engine import IndicatorValues
        iv = IndicatorValues(
            symbol="BTC/USDT", timeframe="1h",
            close=100, high=105, low=95, volume=1000,
            ema_fast=99, ema_slow=100, ema_trend=101,
            ema_fast_prev=100, ema_slow_prev=99,
            rsi=45, macd=-1, macd_signal=-0.5, macd_hist=-0.5, macd_hist_prev=0.5,
            adx=30, dmi_plus=15, dmi_minus=30,
            atr=2, supertrend=105, supertrend_direction=-1,
            volume_sma=800,
        )
        assert iv.ema_bearish_cross is True

    def test_ema_bullish_alignment(self):
        from indicators.engine import IndicatorValues
        iv = IndicatorValues(
            symbol="BTC/USDT", timeframe="1h",
            close=100, high=105, low=95, volume=1000,
            ema_fast=102, ema_slow=100, ema_trend=98,
            ema_fast_prev=100, ema_slow_prev=98,
            rsi=55, macd=1, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=0,
            adx=30, dmi_plus=30, dmi_minus=15,
            atr=2, supertrend=95, supertrend_direction=1,
            volume_sma=800,
        )
        assert iv.ema_bullish_alignment is True

    def test_trend_is_strong(self):
        from indicators.engine import IndicatorValues
        from config.settings import config
        iv = IndicatorValues(
            symbol="BTC/USDT", timeframe="1h",
            close=100, high=105, low=95, volume=1000,
            ema_fast=100, ema_slow=99, ema_trend=98,
            ema_fast_prev=99, ema_slow_prev=98,
            rsi=55, macd=1, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=0,
            adx=25, dmi_plus=30, dmi_minus=15,
            atr=2, supertrend=95, supertrend_direction=1,
            volume_sma=800,
        )
        assert iv.trend_is_strong is True

    def test_volume_above_avg(self):
        from indicators.engine import IndicatorValues
        iv = IndicatorValues(
            symbol="BTC/USDT", timeframe="1h",
            close=100, high=105, low=95, volume=2000,
            ema_fast=100, ema_slow=99, ema_trend=98,
            ema_fast_prev=99, ema_slow_prev=98,
            rsi=55, macd=1, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=0,
            adx=30, dmi_plus=30, dmi_minus=15,
            atr=2, supertrend=95, supertrend_direction=1,
            volume_sma=800,
        )
        assert iv.volume_above_avg is True


class TestSignalResultFormatting:
    """Проверяем форматирование сообщений SignalResult"""

    def test_buy_format_message(self):
        from strategy.signal_engine import SignalResult, SignalType
        result = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000, entry_price=50000, sl=49000, tp=53000,
            reasons=["Supertrend: восходящий тренд", "EMA alignment bullish"],
            score=5,
        )
        msg = result.format_message()
        assert "BUY" in msg
        assert "ПОКУПКА" in msg
        assert "BTC/USDT" in msg
        assert "49000" in msg
        assert "53000" in msg
        assert "5" in msg

    def test_sell_format_message(self):
        from strategy.signal_engine import SignalResult, SignalType
        result = SignalResult(
            signal=SignalType.SELL, symbol="ETH/USDT", timeframe="4h",
            close=3000, entry_price=3000, sl=3100, tp=2800,
            reasons=["Supertrend: нисходящий тренд"],
            score=4,
        )
        msg = result.format_message()
        assert "SELL" in msg
        assert "ПРОДАЖА" in msg
        assert "ETH/USDT" in msg

    def test_no_signal_format_message(self):
        from strategy.signal_engine import SignalResult, SignalType
        result = SignalResult(
            signal=SignalType.NO_SIGNAL, symbol="SOL/USDT", timeframe="1h",
            close=100,
            reasons=["ADX=15.0 < 20.0 (флэт, сигналы игнорируются)"],
            score=0,
        )
        msg = result.format_message()
        assert "NO_SIGNAL" in msg

    def test_risk_reward_display(self):
        from strategy.signal_engine import SignalResult, SignalType
        result = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000, sl=49000, tp=53000,
            reasons=[], score=5,
        )
        msg = result.format_message()
        assert "RR: 1:" in msg  # current format: "RR: 1:3.0"
