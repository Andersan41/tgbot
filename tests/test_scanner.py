"""
tests/test_scanner.py — Tests for scan_symbol_v2 (ICT Core pipeline).

Old pipeline tests (signal_engine.evaluate, indicator_engine, confirm TF, etc.)
were removed when the indicator-centric gates were deleted.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler.scanner import scan_symbol_v2, run_scan_cycle, _is_cooldown_active, _set_cooldown


@pytest.fixture(autouse=True)
def _mock_portfolio_risk(monkeypatch):
    """Auto-mock portfolio risk gate methods so existing tests don't hit the real DB."""
    from storage.database import db as _db
    monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(return_value=0))
    monkeypatch.setattr(_db, "get_portfolio_risk_sum", AsyncMock(return_value=0.0))


@pytest.fixture
def mock_cooldown(monkeypatch):
    """In-memory cooldown store."""
    store: dict = {}

    async def fake_get(symbol, timeframe):
        return store.get((symbol, timeframe))

    async def fake_set(symbol, timeframe, ts):
        store[(symbol, timeframe)] = ts

    monkeypatch.setattr("scheduler.scanner.db.get_cooldown", fake_get)
    monkeypatch.setattr("scheduler.scanner.db.set_cooldown", fake_set)
    return store


class TestCooldown:
    @pytest.mark.asyncio
    async def test_no_cooldown_initially(self, mock_cooldown):
        active, _ = await _is_cooldown_active("BTC/USDT", "1h")
        assert active is False

    @pytest.mark.asyncio
    async def test_cooldown_blocks_repeat(self, mock_cooldown):
        await _set_cooldown("BTC/USDT", "1h")
        active, _ = await _is_cooldown_active("BTC/USDT", "1h")
        assert active is True

    @pytest.mark.asyncio
    async def test_cooldown_other_symbol_independent(self, mock_cooldown):
        await _set_cooldown("BTC/USDT", "1h")
        active, _ = await _is_cooldown_active("ETH/USDT", "1h")
        assert active is False

    @pytest.mark.asyncio
    async def test_cooldown_different_timeframe_independent(self, mock_cooldown):
        await _set_cooldown("BTC/USDT", "1h")
        active, _ = await _is_cooldown_active("BTC/USDT", "4h")
        assert active is False


class TestScanSymbolV2:
    @pytest.mark.asyncio
    async def test_returns_none_when_cooldown(self, mock_cooldown):
        await _set_cooldown("BTC/USDT", "1h")
        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_indicator_data(self):
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv = AsyncMock(return_value=None)
        with patch("scheduler.scanner.exchange_client", mock_exchange):
            result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_pattern_not_detected(self, mock_cooldown):
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv = AsyncMock()
        mock_ind = MagicMock(atr=600.0, close=50000.0)
        mock_ind_engine = MagicMock()
        mock_ind_engine.calculate.return_value = mock_ind

        from strategy.pattern_engine import ICTSetup
        no_setup = ICTSetup(detected=False, rejection_reason="no valid setup (need reversal or continuation)")

        with (
            patch("scheduler.scanner.exchange_client", mock_exchange),
            patch("scheduler.scanner._get_indicators", AsyncMock(return_value=(mock_ind, MagicMock()))),
            patch("strategy.pattern_engine.pattern_engine", MagicMock(detect=MagicMock(return_value=no_setup))),
        ):
            result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
            assert result is None

    @pytest.mark.asyncio
    async def test_full_successful_scan(self, mock_cooldown, monkeypatch):
        from strategy.pattern_engine import ICTSetup
        from market_structure.structure import StructureState

        setup_mock = ICTSetup(
            detected=True, direction="buy", setup_type="continuation",
            has_bos=True, bos_type="bullish", bos_level=49500.0,
            has_ob=True, ob_type="bullish", ob_distance_pct=0.5,
            has_sweep=True, sweep_type="bearish",
            components_found=["BOS", "OB"],
        )
        risk_mock = MagicMock(should_trade=True, risk_pct=1.0, rr_ratio=3.0, rejection_reason=None)

        df_mock = pd.DataFrame({
            "open": [50000.0] * 20,
            "high": [50500.0] * 20,
            "low": [49500.0] * 20,
            "close": [50200.0] * 20,
            "volume": [1000.0] * 20,
        })
        ind_mock = MagicMock(atr=600.0, close=50000.0)

        monkeypatch.setattr("scheduler.scanner._is_cooldown_active", AsyncMock(return_value=(False, 0)))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(ind_mock, df_mock)))
        monkeypatch.setattr("scheduler.scanner._detect_regime", MagicMock(return_value=None))
        _mock_sweep = MagicMock(is_valid=True, sweep_type="bearish", reclaim_candles=2)
        monkeypatch.setattr("liquidity.sweep.detect_sweeps", MagicMock(return_value=[_mock_sweep]))
        monkeypatch.setattr("liquidity.order_blocks.detect_order_blocks", MagicMock(return_value=[]))
        monkeypatch.setattr("market_structure.structure.analyze_structure",
                            MagicMock(return_value=StructureState(trend="bullish")))
        monkeypatch.setattr("liquidity.fvg.detect_fvg", MagicMock(return_value=[]))
        monkeypatch.setattr("liquidity.candle_quality.analyze_last_candle",
                            MagicMock(return_value=MagicMock(is_displacement=True, body_atr_ratio=1.5)))
        monkeypatch.setattr("strategy.pattern_engine.pattern_engine",
                            MagicMock(detect=MagicMock(return_value=setup_mock)))
        monkeypatch.setattr("risk.engine.risk_engine",
                            MagicMock(evaluate=MagicMock(return_value=risk_mock)))
        monkeypatch.setattr("scheduler.scanner.exchange_client",
                            MagicMock(get_tick_size=MagicMock(return_value=0.01),
                                      fetch_ticker_full=AsyncMock(return_value={"bid": 50000.0, "ask": 50001.0})))
        monkeypatch.setattr("scheduler.scanner.db", MagicMock(
            save_signal=AsyncMock(return_value=MagicMock(id=1)),
            set_cooldown=AsyncMock(),
            get_active_signals_count=AsyncMock(return_value=0),
            get_portfolio_risk_sum=AsyncMock(return_value=0.0),
            get_last_signal=AsyncMock(return_value=None),
            create_outcome=AsyncMock(),
        ))

        cb = AsyncMock()
        result = await scan_symbol_v2("BTC/USDT", "1h", cb)
        assert result is not None
        assert result.signal.value == "BUY"
        assert result.symbol == "BTC/USDT"
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_signal(self, mock_cooldown, monkeypatch):
        from strategy.pattern_engine import ICTSetup
        from market_structure.structure import StructureState

        setup_mock = ICTSetup(
            detected=True, direction="buy", setup_type="continuation",
            has_bos=True, bos_type="bullish", bos_level=49500.0,
            has_sweep=True, sweep_type="bearish",
            components_found=["BOS"],
        )
        risk_mock = MagicMock(should_trade=True, risk_pct=1.0, rr_ratio=3.0, rejection_reason=None)

        df_mock = pd.DataFrame({
            "open": [50000.0] * 20,
            "high": [50500.0] * 20,
            "low": [49500.0] * 20,
            "close": [50200.0] * 20,
            "volume": [1000.0] * 20,
        })
        ind_mock = MagicMock(atr=600.0, close=50000.0)

        mock_db = MagicMock(
            save_signal=AsyncMock(return_value=MagicMock(id=1)),
            set_cooldown=AsyncMock(),
            get_active_signals_count=AsyncMock(return_value=0),
            get_portfolio_risk_sum=AsyncMock(return_value=0.0),
            get_last_signal=AsyncMock(return_value=None),
            create_outcome=AsyncMock(),
        )

        monkeypatch.setattr("scheduler.scanner._is_cooldown_active", AsyncMock(return_value=(False, 0)))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(ind_mock, df_mock)))
        monkeypatch.setattr("scheduler.scanner._detect_regime", MagicMock(return_value=None))
        _mock_sweep = MagicMock(is_valid=True, sweep_type="bearish", reclaim_candles=2)
        monkeypatch.setattr("liquidity.sweep.detect_sweeps", MagicMock(return_value=[_mock_sweep]))
        monkeypatch.setattr("liquidity.order_blocks.detect_order_blocks", MagicMock(return_value=[]))
        monkeypatch.setattr("market_structure.structure.analyze_structure",
                            MagicMock(return_value=StructureState(trend="bullish")))
        monkeypatch.setattr("liquidity.fvg.detect_fvg", MagicMock(return_value=[]))
        monkeypatch.setattr("liquidity.candle_quality.analyze_last_candle",
                            MagicMock(return_value=MagicMock(is_displacement=True, body_atr_ratio=1.5)))
        monkeypatch.setattr("strategy.pattern_engine.pattern_engine",
                            MagicMock(detect=MagicMock(return_value=setup_mock)))
        monkeypatch.setattr("risk.engine.risk_engine",
                            MagicMock(evaluate=MagicMock(return_value=risk_mock)))
        monkeypatch.setattr("scheduler.scanner.exchange_client",
                            MagicMock(get_tick_size=MagicMock(return_value=0.01),
                                      fetch_ticker_full=AsyncMock(return_value={"bid": 50000.0, "ask": 50001.0})))
        monkeypatch.setattr("scheduler.scanner.db", mock_db)

        await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        mock_db.set_cooldown.assert_awaited_once()


class TestPortfolioRiskGate:
    @pytest.mark.asyncio
    async def test_blocks_when_max_active_signals_reached(self, mock_cooldown, monkeypatch):
        from storage.database import db as _db
        monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(return_value=10))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))

        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_when_portfolio_risk_exceeded(self, mock_cooldown, monkeypatch):
        from storage.database import db as _db
        monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(return_value=1))
        monkeypatch.setattr(_db, "get_portfolio_risk_sum", AsyncMock(return_value=15.0))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))

        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None


