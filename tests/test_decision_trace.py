import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import db, Base, DecisionTrace
from storage.trace import DecisionTraceBuilder, GATE_ORDER, FEATURE_KEYS


@pytest.fixture(autouse=True)
async def setup_db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_traces.db"
    db._engine = create_async_engine(db_url, echo=False)
    db._session_factory = sessionmaker(
        db._engine, class_=AsyncSession, expire_on_commit=False
    )
    async with db._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await db._engine.dispose()


class TestDecisionTraceBuilder:
    def test_init(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        assert t.symbol == "BTC/USDT"
        assert t.timeframe == "1h"
        assert t.gates == {}
        assert t.final_stage is None

    def test_record(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.record("cooldown", True)
        t.record("portfolio_risk", True)
        assert t.gates["cooldown"] is True
        assert t.gates["portfolio_risk"] is True

    def test_passed(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.passed("cooldown")
        assert t.gates["cooldown"] is True
        assert t.final_stage is None

    def test_blocked(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.passed("cooldown")
        t.blocked("portfolio_risk", "max active signals reached")
        assert t.gates["cooldown"] is True
        assert t.gates["portfolio_risk"] is False
        assert t.final_stage == "portfolio_risk"

    def test_blocked_stores_bool_in_gates(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.blocked("news", "x" * 300)
        assert t.gates["news"] is False  # just the bool, reason stored separately

    def test_set_signal(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.set_signal("BUY", 6, 50000.0, 48500.0, 53000.0)
        assert t.final_stage == "signal_generated"

    def test_gate_order_matches_funnel(self):
        from scheduler.scanner import _FUNNEL_GATES
        # All gates in GATE_ORDER should be in _FUNNEL_GATES (except compression_block which is special)
        for gate in GATE_ORDER:
            assert gate in _FUNNEL_GATES, f"{gate} missing from _FUNNEL_GATES"


class TestDecisionTraceDB:
    @pytest.mark.asyncio
    async def test_save_minimal(self, setup_db):
        await db.init()
        trace = await db.save_decision_trace(
            symbol="BTC/USDT",
            timeframe="1h",
            gate_results={"cooldown": True, "portfolio_risk": True},
        )
        assert trace.id > 0
        assert trace.symbol == "BTC/USDT"
        assert trace.gate_cooldown is True
        assert trace.gate_portfolio_risk is True
        assert trace.gate_indicators is None
        assert trace.signal_generated is False

    @pytest.mark.asyncio
    async def test_save_full_signal(self, setup_db):
        await db.init()
        trace = await db.save_decision_trace(
            symbol="ETH/USDT",
            timeframe="4h",
            gate_results={g: True for g in GATE_ORDER},
            final_stage="signal_generated",
            signal_generated=True,
            signal_type="SELL",
            score=5,
            close_price=3000.0,
            sl=3100.0,
            tp=2800.0,
        )
        assert trace.signal_type == "SELL"
        assert trace.score == 5
        assert trace.signal_generated is True
        assert trace.gate_cooldown is True
        assert trace.gate_dedup is True

    @pytest.mark.asyncio
    async def test_save_blocked(self, setup_db):
        await db.init()
        gates = {g: True for g in GATE_ORDER[:5]}
        gates["confirm_tf"] = False
        trace = await db.save_decision_trace(
            symbol="SOL/USDT",
            timeframe="1h",
            gate_results=gates,
            final_stage="confirm_tf",
            blocked_reason="direction mismatch on 15m",
        )
        assert trace.gate_confirm_tf is False
        assert trace.final_stage == "confirm_tf"
        assert trace.blocked_reason == "direction mismatch on 15m"
        assert trace.signal_generated is False

    @pytest.mark.asyncio
    async def test_link_trace_to_signal(self, setup_db):
        await db.init()
        sig = await db.save_signal(
            "BTC/USDT", "1h", "BUY", 50000.0, 48000.0, 53000.0, 5, [],
        )
        trace = await db.save_decision_trace(
            symbol="BTC/USDT", timeframe="1h",
            gate_results={"cooldown": True},
            signal_generated=True,
        )
        await db.link_trace_to_signal(trace.id, sig.id)

        # Verify link
        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace.id)
            )
            row = result.scalar_one_or_none()
            assert row.signal_id == sig.id

    @pytest.mark.asyncio
    async def test_update_trace_outcome(self, setup_db):
        await db.init()
        trace = await db.save_decision_trace(
            symbol="BTC/USDT", timeframe="1h",
            gate_results={"cooldown": True},
            signal_generated=True,
        )
        await db.update_trace_outcome(trace.id, "HIT_TP", 3.5)

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace.id)
            )
            row = result.scalar_one_or_none()
            assert row.outcome == "HIT_TP"
            assert row.pnl_pct == 3.5


class TestGateStats:
    @pytest.mark.asyncio
    async def test_empty_returns_empty(self, setup_db):
        await db.init()
        stats = await db.get_trace_stats()
        assert stats == []

    @pytest.mark.asyncio
    async def test_basic_funnel(self, setup_db):
        """Simulate 10 candidates: 8 pass cooldown, 5 pass confirm_tf, 2 generate signals."""
        await db.init()
        for i in range(10):
            gates = {"cooldown": True, "portfolio_risk": True, "indicators": True}
            if i < 8:
                gates["confirm_tf"] = True
            else:
                gates["confirm_tf"] = False
            if i < 5:
                gates["signal_engine"] = True
            else:
                gates["signal_engine"] = False
            if i < 2:
                gates["dedup"] = True
            else:
                gates["dedup"] = False

            await db.save_decision_trace(
                symbol="BTC/USDT", timeframe="1h",
                gate_results=gates,
                signal_generated=(i < 2),
                final_stage="dedup" if i >= 2 else ("signal_generated" if i < 2 else "signal_engine"),
            )

        stats = await db.get_trace_stats("BTC/USDT")
        assert len(stats) > 0

        cooldown = next(s for s in stats if s["gate"] == "cooldown")
        assert cooldown["entered"] == 10
        assert cooldown["passed"] == 10

        confirm = next(s for s in stats if s["gate"] == "confirm_tf")
        assert confirm["entered"] == 10
        assert confirm["passed"] == 8
        assert confirm["dropped"] == 2

    @pytest.mark.asyncio
    async def test_stats_by_symbol(self, setup_db):
        await db.init()
        await db.save_decision_trace(
            symbol="BTC/USDT", timeframe="1h",
            gate_results={"cooldown": True},
            signal_generated=True,
        )
        await db.save_decision_trace(
            symbol="ETH/USDT", timeframe="1h",
            gate_results={"cooldown": True},
            signal_generated=True,
        )
        btc_stats = await db.get_trace_stats("BTC/USDT")
        assert len(btc_stats) > 0
        assert all(s["entered"] <= 1 for s in btc_stats)


class TestCounterfactual:
    @pytest.mark.asyncio
    async def test_disable_gate_that_blocks_nothing(self, setup_db):
        await db.init()
        for i in range(5):
            await db.save_decision_trace(
                symbol="BTC/USDT", timeframe="1h",
                gate_results={g: True for g in GATE_ORDER},
                signal_generated=True,
                signal_type="BUY", score=5, close_price=50000.0,
            )
        result = await db.get_counterfactual("cooldown", "BTC/USDT")
        assert result["would_add"] == 0
        assert result["original_count"] == 5

    @pytest.mark.asyncio
    async def test_disable_gate_that_blocks_some(self, setup_db):
        await db.init()
        # 3 pass all gates, 2 blocked by confirm_tf
        for i in range(5):
            gates = {g: True for g in GATE_ORDER}
            if i >= 3:
                gates["confirm_tf"] = False
            await db.save_decision_trace(
                symbol="BTC/USDT", timeframe="1h",
                gate_results=gates,
                signal_generated=(i < 3),
                signal_type="BUY" if i < 3 else None,
                score=5 if i < 3 else None,
                close_price=50000.0 if i < 3 else None,
            )
        result = await db.get_counterfactual("confirm_tf", "BTC/USDT")
        assert result["original_count"] == 3
        assert result["would_add"] == 2
        assert result["new_total"] == 5

    @pytest.mark.asyncio
    async def test_unknown_gate_returns_error(self, setup_db):
        await db.init()
        result = await db.get_counterfactual("nonexistent_gate")
        assert "error" in result


class TestDecisionTraceIntegration:
    """Integration test: DecisionTraceBuilder → DB save → query."""

    @pytest.mark.asyncio
    async def test_builder_to_db_flow(self, setup_db):
        await db.init()
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.passed("cooldown")
        t.passed("portfolio_risk")
        t.passed("indicators")
        t.blocked("confirm_tf", "direction mismatch on 15m")

        trace_id = await t.save(db)
        assert trace_id > 0

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.gate_cooldown is True
            assert row.gate_portfolio_risk is True
            assert row.gate_indicators is True
            assert row.gate_confirm_tf is False
            assert row.final_stage == "confirm_tf"
            assert row.blocked_reason == "direction mismatch on 15m"
            assert row.signal_generated is False

    @pytest.mark.asyncio
    async def test_builder_signal_flow(self, setup_db):
        await db.init()
        t = DecisionTraceBuilder("ETH/USDT", "4h")
        for gate in GATE_ORDER:
            t.passed(gate)
        t.set_signal("BUY", 6, 3000.0, 2900.0, 3300.0)

        trace_id = await t.save(db)
        assert trace_id > 0

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            assert row.signal_generated is True
            assert row.signal_type == "BUY"
            assert row.final_stage == "signal_generated"


class TestFeatureSnapshot:
    """Tests for feature snapshot fields in DecisionTrace."""

    def test_feature_keys_defined(self):
        assert len(FEATURE_KEYS) == 40  # 25 original + 6 Decision Intelligence + 9 B-011 V2 pipeline
        assert "adx" in FEATURE_KEYS
        assert "rsi" in FEATURE_KEYS
        assert "regime" in FEATURE_KEYS
        assert "rr_ratio" in FEATURE_KEYS
        # Decision Intelligence features
        assert "atr_pct" in FEATURE_KEYS
        assert "ema_slope_3" in FEATURE_KEYS
        assert "ema_slope_5" in FEATURE_KEYS
        assert "nearest_support_pct" in FEATURE_KEYS
        assert "nearest_resistance_pct" in FEATURE_KEYS
        assert "regime_confidence" in FEATURE_KEYS

    def test_builder_set_features_filters_unknown_keys(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.set_features({
            "adx": 42.1,
            "rsi": 58.3,
            "unknown_key": "should_be_filtered",
            "regime": "expansion",
        })
        assert t._features["adx"] == 42.1
        assert t._features["rsi"] == 58.3
        assert t._features["regime"] == "expansion"
        assert "unknown_key" not in t._features

    def test_builder_set_version(self):
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.set_version("2.4.0", '{"adx_min": 26}')
        assert t._strategy_version == "2.4.0"
        assert t._config_snapshot == '{"adx_min": 26}'

    @pytest.mark.asyncio
    async def test_features_saved_to_db(self, setup_db):
        await db.init()
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.passed("cooldown")
        t.passed("indicators")
        t.set_features({
            "adx": 42.1,
            "rsi": 58.3,
            "ema_short": 105000.0,
            "ema_long": 104200.0,
            "ema_spread_pct": 0.77,
            "macd_hist": 150.5,
            "supertrend_direction": 1,
            "volume_ratio": 2.3,
            "dmi_strength": 0.85,
            "ema_strength": 0.72,
            "signal_score": 6,
            "confidence": 72.5,
            "regime": "expansion",
            "direction": "BUY",
            "sl_source": "bos",
            "tp_distance_pct": 3.5,
            "sl_distance_pct": 1.2,
            "rr_ratio": 2.92,
            "has_bos": True,
            "has_sweep": False,
            "has_ob": True,
            "ob_distance_pct": 0.8,
            "context_score": 0.65,
            "btc_trend_strength": 1.02,
            "mtf_alignment_score": 2.0,
        })
        t.set_version("2.4.0", '{"adx_min": 26}')
        t.blocked("confirm_tf", "mismatch")

        trace_id = await t.save(db)
        assert trace_id > 0

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.adx == 42.1
            assert row.rsi == 58.3
            assert row.ema_short == 105000.0
            assert row.ema_long == 104200.0
            assert row.ema_spread_pct == 0.77
            assert row.macd_hist == 150.5
            assert row.supertrend_direction == 1
            assert row.volume_ratio == 2.3
            assert row.dmi_strength == 0.85
            assert row.ema_strength == 0.72
            assert row.signal_score == 6
            assert row.confidence == 72.5
            assert row.regime == "expansion"
            assert row.direction == "BUY"
            assert row.sl_source == "bos"
            assert row.tp_distance_pct == 3.5
            assert row.sl_distance_pct == 1.2
            assert row.rr_ratio == 2.92
            assert row.has_bos is True
            assert row.has_sweep is False
            assert row.has_ob is True
            assert row.ob_distance_pct == 0.8
            assert row.context_score == 0.65
            assert row.btc_trend_strength == 1.02
            assert row.mtf_alignment_score == 2.0
            assert row.strategy_version == "2.4.0"
            assert row.config_snapshot == '{"adx_min": 26}'

    @pytest.mark.asyncio
    async def test_features_none_when_not_set(self, setup_db):
        await db.init()
        t = DecisionTraceBuilder("BTC/USDT", "1h")
        t.passed("cooldown")
        t.blocked("indicators", "no data")

        trace_id = await t.save(db)

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            assert row.adx is None
            assert row.rsi is None
            assert row.regime is None
            assert row.strategy_version is None
            assert row.config_snapshot is None

    @pytest.mark.asyncio
    async def test_signal_with_features(self, setup_db):
        await db.init()
        t = DecisionTraceBuilder("ETH/USDT", "4h")
        for gate in GATE_ORDER:
            t.passed(gate)
        t.set_signal("SELL", 5, 3000.0, 3100.0, 2800.0)
        t.set_features({
            "adx": 35.0,
            "rsi": 42.1,
            "direction": "SELL",
            "regime": "expansion",
            "rr_ratio": 2.0,
            "signal_score": 5,
            "confidence": 65.0,
        })
        t.set_version("2.4.0")

        trace_id = await t.save(db, signal_id=1, candidate_id=1)

        from sqlalchemy import select
        async with db._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            assert row.signal_generated is True
            assert row.signal_type == "SELL"
            assert row.adx == 35.0
            assert row.direction == "SELL"
            assert row.rr_ratio == 2.0
            assert row.strategy_version == "2.4.0"
