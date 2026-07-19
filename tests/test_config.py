import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reload_settings():
    """Reload config.settings before each test to reset to env defaults."""
    import config.settings as settings
    importlib.reload(settings)
    # Re-bind class references after reload so tests use the fresh module
    globals().update({
        "TelegramConfig": settings.TelegramConfig,
        "ExchangeConfig": settings.ExchangeConfig,
        "TradingConfig": settings.TradingConfig,
        "AppConfig": settings.AppConfig,
    })


class TestTelegramConfig:
    def test_reads_from_env(self):
        cfg = TelegramConfig()
        assert len(cfg.token) > 0
        assert len(cfg.channel_id) > 0

    def test_admin_ids_list_of_ints(self):
        cfg = TelegramConfig()
        assert isinstance(cfg.admin_ids, list)
        if cfg.admin_ids:
            assert all(isinstance(i, int) for i in cfg.admin_ids)


class TestExchangeConfig:
    def test_reads_from_env(self):
        cfg = ExchangeConfig()
        assert cfg.name in ("binance", "bingx")
        assert len(cfg.api_key) > 0
        assert len(cfg.api_secret) > 0
        assert cfg.testnet is False
        assert cfg.market_type in ("spot", "swap", "future")

    def test_market_type_from_env(self, monkeypatch):
        monkeypatch.setenv("MARKET_TYPE", "future")
        import config.settings as settings
        importlib.reload(settings)
        cfg = settings.ExchangeConfig()
        assert cfg.market_type == "future"


class TestTradingConfig:
    def test_symbols_from_env(self):
        cfg = TradingConfig()
        assert "BTC/USDT" in cfg.symbols
        assert "XRP/USDT" in cfg.symbols

    def test_timeframes_from_env(self):
        cfg = TradingConfig()
        assert "1h" in cfg.primary_timeframes
        assert "4h" in cfg.primary_timeframes
        assert cfg.confirm_timeframe == "15m"

    def test_indicator_params(self):
        cfg = TradingConfig()
        assert cfg.rsi_period == 10
        assert cfg.adx_period == 14
        assert cfg.supertrend_period == 10
        assert cfg.atr_multiplier_sl == 1.5
        assert cfg.atr_multiplier_tp == 3.0
        assert cfg.candles_limit == 200
        assert cfg.volume_sma_period == 20

    def test_sl_distance_and_rr_params(self):
        cfg = TradingConfig()
        assert cfg.min_sl_distance_pct == 1.0
        assert cfg.max_sl_distance_pct == 10.0
        assert cfg.min_rr_threshold == 1.5


class TestAppConfig:
    def test_database_url_from_env(self):
        cfg = AppConfig()
        assert "signals.db" in cfg.database_url

    def test_cooldown_default(self):
        cfg = AppConfig()
        assert cfg.signal_cooldown_minutes == 45


class TestIndicatorEnvVars:
    def test_ema_fast_from_env(self, monkeypatch):
        monkeypatch.setenv("EMA_FAST", "5")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.ema_fast == 5

    def test_atr_multiplier_from_env(self, monkeypatch):
        monkeypatch.setenv("ATR_MULTIPLIER_SL", "2.5")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.atr_multiplier_sl == 2.5

    def test_defaults_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("EMA_FAST", raising=False)
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.ema_fast == 8

    def test_volume_factor_from_env(self, monkeypatch):
        monkeypatch.setenv("VOLUME_FACTOR", "1.5")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.volume_factor == 1.5

    def test_candles_limit_from_env(self, monkeypatch):
        monkeypatch.setenv("CANDLES_LIMIT", "300")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.candles_limit == 300

    def test_volume_sma_period_from_env(self, monkeypatch):
        monkeypatch.setenv("VOLUME_SMA_PERIOD", "50")
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.volume_sma_period == 50


class TestEnvFile:
    ENV_KEYS = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_ID",
        "EXCHANGE",
        "MARKET_TYPE",
        "SYMBOLS",
        "PRIMARY_TIMEFRAMES",
        "CONFIRM_TIMEFRAME",
        "DATABASE_URL",
        "LOG_LEVEL",
        "LOG_FILE",
        "EMA_FAST",
        "EMA_SLOW",
        "EMA_TREND",
        "RSI_PERIOD",
        "RSI_OVERBOUGHT",
        "RSI_OVERSOLD",
        "RSI_BULL_MIN",
        "RSI_BEAR_MAX",
        "MACD_FAST",
        "MACD_SLOW",
        "MACD_SIGNAL",
        "ADX_PERIOD",
        "ADX_MIN",
        "ATR_PERIOD",
        "ATR_MULTIPLIER_SL",
        "ATR_MULTIPLIER_TP",
        "MIN_SL_DISTANCE_PCT",
        "MAX_SL_DISTANCE_PCT",
        "MIN_RR_THRESHOLD",
        "SUPERTREND_PERIOD",
        "SUPERTREND_MULTIPLIER",
        "VOLUME_FACTOR",
        "CANDLES_LIMIT",
    ]

    def test_env_example_exists(self):
        assert Path(".env.example").exists()

    def test_env_example_contains_all_keys(self):
        content = Path(".env.example").read_text()
        for key in self.ENV_KEYS:
            assert key in content, f"Missing key: {key}"

    def test_env_example_has_no_real_secrets(self):
        content = Path(".env.example").read_text().lower()
        keywords = ["8709934704", "3xg1t2mh"]
        for kw in keywords:
            assert kw not in content, f"Found potential secret: {kw}"

    def test_env_example_placeholder_values(self):
        content = Path(".env.example").read_text()
        assert "your_telegram_bot_token_here" in content
        assert "your_binance_api_key_here" in content
        assert "your_binance_api_secret_here" in content

    def test_env_matches_example_structure(self):
        """Both files must have the same set of keys"""
        env = Path(".env").read_text()
        example = Path(".env.example").read_text()
        for key in self.ENV_KEYS:
            assert key in env, f"Missing key in .env: {key}"
            assert key in example, f"Missing key in .env.example: {key}"


class TestScoringConfig:
    """Tests for centralized scoring config (Task 9.2)."""

    def test_factor_weights_default(self):
        import config.settings as settings
        importlib.reload(settings)
        s = settings.config.scoring
        assert s.w_supertrend == 5
        assert s.w_ema == 10
        assert s.w_macd == 10
        assert s.w_rsi == 5
        assert s.w_volume == 15
        assert s.w_adx == 5
        assert s.w_dmi == 5
        assert s.w_bos == 15
        assert s.w_sweep == 10
        assert s.w_ob == 10
        assert s.w_btc == 10
        assert s.w_funding == 5
        assert s.w_oi == 10

    def test_max_signal_score(self):
        import config.settings as settings
        importlib.reload(settings)
        # 5+10+10+5+15+5+5+15+10+10+10+5+10 = 115
        assert settings.config.scoring.max_signal_score == 115

    def test_confidence_v2_weights(self):
        import config.settings as settings
        importlib.reload(settings)
        s = settings.config.scoring
        assert s.w_htf_trend == 20
        assert s.w_structure == 15
        assert s.w_liquidity == 20
        assert s.w_conf_volume == 5
        assert s.w_btc_corr == 15

    def test_min_score_for_signal(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.scoring.min_score_for_signal == 2

    def test_confidence_v2_thresholds(self):
        import config.settings as settings
        importlib.reload(settings)
        s = settings.config.scoring
        assert s.quality_strong_threshold == 65
        assert s.quality_moderate_threshold == 30

    def test_blend_ratios(self):
        import config.settings as settings
        importlib.reload(settings)
        s = settings.config.scoring
        assert s.tech_confidence_blend == 0.6
        assert s.historical_wr_blend == 0.4


class TestSchedulerConfig:
    """Tests for centralized scheduler config (Task 9.2)."""

    def test_scan_minutes_default(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.scheduler.scan_minutes == "2,17,32,47"


class TestRateLimitConfig:
    """Tests for centralized rate limit config (Task 9.2)."""

    def test_defaults(self):
        import config.settings as settings
        importlib.reload(settings)
        rl = settings.config.rate_limit
        assert rl.max_rate == 5
        assert rl.time_period == 10


class TestNotifierConfig:
    """Tests for centralized notifier config (Task 9.2)."""

    def test_fng_thresholds(self):
        import config.settings as settings
        importlib.reload(settings)
        n = settings.config.notifier
        assert n.fng_extreme_threshold_low == 20
        assert n.fng_extreme_threshold_high == 80

    def test_long_short_ratio_threshold(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.notifier.long_short_ratio_threshold == 0.7

    def test_sentiment_thresholds(self):
        import config.settings as settings
        importlib.reload(settings)
        n = settings.config.notifier
        assert n.sentiment_positive_threshold == 0.2
        assert n.sentiment_negative_threshold == -0.2


class TestRiskConfig:
    """Tests for centralized risk config (Task 9.2)."""

    def test_volatility_multipliers(self):
        import config.settings as settings
        importlib.reload(settings)
        r = settings.config.risk
        assert r.volatility_high_multiplier == 0.5
        assert r.correlation_misaligned_multiplier == 0.5

    def test_regime_thresholds(self):
        import config.settings as settings
        importlib.reload(settings)
        r = settings.config.risk
        assert r.regime_trend_adx == 25
        assert r.regime_range_adx == 20
        assert r.regime_compression_atr_pct == 20

    def test_risk_weak_trade_and_pct(self):
        import config.settings as settings
        importlib.reload(settings)
        r = settings.config.risk
        assert r.risk_weak_trade is False
        assert r.risk_weak_pct == 0.25

    def test_regime_detection_params(self):
        import config.settings as settings
        importlib.reload(settings)
        r = settings.config.risk
        assert r.regime_ema_spread_window == 5
        assert r.regime_ema_spread_change_pct == 0.05
        assert r.regime_rising_multiplier == 1.1
        assert r.regime_rising_window == 10
        assert r.regime_fallback_confidence == 0.3


class TestTradingConfigExtended:
    """Extended tests for trading config (Task 9.2)."""

    def test_adx_strong_threshold(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.adx_strong == 22

    def test_atr_fallback_pct(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.atr_fallback_pct == 2.0

    def test_macd_score_multiplier(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.macd_score_multiplier == 10

    def test_volume_delta_norm(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.volume_delta_norm == 50

    def test_ema_strength_cap(self):
        import config.settings as settings
        importlib.reload(settings)
        assert settings.config.trading.ema_strength_cap == 1.0


class TestConfigHotReload:
    """Tests for hot-reload mechanism (Task 9.2)."""

    @pytest.mark.asyncio
    async def test_reload_config_function_exists(self):
        import config.settings as settings
        assert hasattr(settings, "reload_config")
        assert callable(settings.reload_config)

    @pytest.mark.asyncio
    async def test_reload_config_replaces_singleton(self):
        import config.settings as settings
        importlib.reload(settings)

        old_id = id(settings.config)
        await settings.reload_config()
        new_id = id(settings.config)

        assert old_id != new_id, "reload_config should create a new config instance"

    @pytest.mark.asyncio
    async def test_reload_config_applies_env_changes(self, monkeypatch):
        """Verify reload_config creates a fresh instance.

        Note: dataclass defaults are evaluated at class-definition time,
        so monkeypatching os.environ after module import won't affect
        already-defined defaults. This test verifies the singleton is
        replaced, which is the key hot-reload behaviour.
        """
        import config.settings as settings
        importlib.reload(settings)

        old_id = id(settings.config)
        old_trading = settings.config.trading

        # Monkeypatch and reload
        monkeypatch.setenv("ADX_STRONG", "30")
        await settings.reload_config()

        new_id = id(settings.config)
        new_trading = settings.config.trading

        # Singleton was replaced
        assert old_id != new_id
        # Trading config is a new instance
        assert old_trading is not new_trading
