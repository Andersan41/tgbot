"""
storage/database.py — SQLAlchemy модели и методы работы с БД
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, select, desc, ForeignKey, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from loguru import logger
from config.settings import config

Base = declarative_base()


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    signal_type = Column(String(10), nullable=False)  # BUY / SELL
    close_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    score = Column(Integer, default=0)
    reasons = Column(Text, nullable=True)
    confirmed = Column(Boolean, default=False)  # Подтверждён на 15M
    factor_fingerprint = Column(String(255), nullable=True, index=True)  # hash of factor combo (Task 6.1)
    confidence_v2_pct = Column(Float, nullable=True)  # abs(total_score) from confidence_v2
    confidence_v2_factors = Column(Text, nullable=True)  # JSON: [{name, weight, raw_score, weighted_score}, ...]
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sent_at = Column(DateTime, nullable=True)
    entry_candle_open = Column(DateTime, nullable=True)  # Open time of candle when signal was created

    # Execution snapshot fields
    entry_price_source = Column(String(20), nullable=True)  # CLOSE / OPEN / MID / BID / ASK
    entry_open = Column(Float, nullable=True)  # Open price of entry candle
    entry_mid = Column(Float, nullable=True)  # (high + low) / 2 of entry candle
    entry_bid = Column(Float, nullable=True)  # Bid at signal creation
    entry_ask = Column(Float, nullable=True)  # Ask at signal creation
    entry_spread = Column(Float, nullable=True)  # ask - bid
    entry_atr = Column(Float, nullable=True)  # ATR at signal creation
    entry_tick_size = Column(Float, nullable=True)  # Minimum price increment

    # Execution latency
    signal_detected_at = Column(DateTime, nullable=True)  # When pattern was detected
    telegram_sent_at = Column(DateTime, nullable=True)  # When message was sent
    exchange_notified_at = Column(DateTime, nullable=True)  # When exchange received order (placeholder)

    # Excursion tracking
    mfe_pct = Column(Float, nullable=True)  # Maximum Favorable Excursion %
    mae_pct = Column(Float, nullable=True)  # Maximum Adverse Excursion %


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ContextSnapshotModel(Base):
    __tablename__ = "context_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    verdict = Column(String(20))
    confidence = Column(Float)
    score = Column(Float)
    fear_greed = Column(Integer, nullable=True)
    funding_rate = Column(Float, nullable=True)
    long_short_ratio = Column(Float, nullable=True)
    open_interest_delta = Column(Float, nullable=True)
    news_sentiment = Column(Float, nullable=True)
    raw_json = Column(Text, nullable=True)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(
        Integer, ForeignKey("signals.id"), nullable=False, index=True
    )
    status = Column(String(20), nullable=False, default="OPEN")
    closed_at = Column(DateTime, nullable=True)
    close_price = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    risk_pct = Column(Float, nullable=True)
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SignalCandidate(Base):
    """Every signal_engine.evaluate() call — pass OR fail.

    Enables feature analysis, walk-forward validation, and probability calibration
    on the full candidate population, not just passed signals.
    """
    __tablename__ = "signal_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    # Outcome tracking (filled later by outcome_tracker)
    outcome = Column(String(20), nullable=True)  # HIT_TP / HIT_SL / EXPIRED
    pnl_pct = Column(Float, nullable=True)
    outcome_signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)

    # What the engine decided
    signal_type = Column(String(10), nullable=True)  # BUY / SELL / null (NO_SIGNAL)
    rejection_reason = Column(String(50), nullable=True)  # ENGINE_ADX_FLAT / null (passed)
    score = Column(Integer, nullable=True)

    # All 7 factor strengths (raw, -1.0 to 1.0)
    st_strength = Column(Float, nullable=True)
    ema_strength = Column(Float, nullable=True)
    macd_strength = Column(Float, nullable=True)
    rsi_strength = Column(Float, nullable=True)
    vol_strength = Column(Float, nullable=True)
    adx_strength = Column(Float, nullable=True)
    dmi_strength = Column(Float, nullable=True)

    # Weighted composite
    weighted_score = Column(Float, nullable=True)

    # Raw indicator values
    adx = Column(Float, nullable=True)
    rsi = Column(Float, nullable=True)
    ema_fast = Column(Float, nullable=True)
    ema_slow = Column(Float, nullable=True)
    ema_trend = Column(Float, nullable=True)
    macd_hist = Column(Float, nullable=True)
    dmi_plus = Column(Float, nullable=True)
    dmi_minus = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    volume_sma = Column(Float, nullable=True)
    supertrend_direction = Column(Integer, nullable=True)

    # Regime context
    regime = Column(String(20), nullable=True)
    regime_confidence = Column(Float, nullable=True)

    # Gate that stopped the candidate (null = passed all gates)
    blocked_gate = Column(String(50), nullable=True, index=True)

    # Metadata
    factor_fingerprint = Column(String(255), nullable=True, index=True)
    confidence_v2_pct = Column(Float, nullable=True)
    has_trigger = Column(Boolean, nullable=True)
    has_leading_trigger = Column(Boolean, nullable=True)
    mtf_aligned = Column(Boolean, nullable=True)


class DecisionTrace(Base):
    """One row per pipeline pass — full funnel decision trace.

    Records pass/fail of every gate for a single candidate evaluation.
    Enables funnel analysis, counterfactual reasoning, and ML training
    on the complete decision space (not just passed signals).
    """
    __tablename__ = "decision_traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    # Gate results (True=PASS, False=BLOCKED, None=NOT_RUN)
    gate_cooldown = Column(Boolean, nullable=True)
    gate_portfolio_risk = Column(Boolean, nullable=True)
    gate_btc_global_trend = Column(Boolean, nullable=True)
    gate_indicators = Column(Boolean, nullable=True)
    gate_confirm_tf = Column(Boolean, nullable=True)
    gate_signal_engine = Column(Boolean, nullable=True)
    gate_distance_filter = Column(Boolean, nullable=True)
    gate_tp_path = Column(Boolean, nullable=True)
    gate_mtf_alignment = Column(Boolean, nullable=True)
    gate_btc_correlation = Column(Boolean, nullable=True)
    gate_eth_correlation = Column(Boolean, nullable=True)
    gate_volatility = Column(Boolean, nullable=True)
    gate_context_timeout = Column(Boolean, nullable=True)
    gate_context_block = Column(Boolean, nullable=True)
    gate_context_min_verdict = Column(Boolean, nullable=True)
    gate_news = Column(Boolean, nullable=True)
    gate_sl_distance = Column(Boolean, nullable=True)
    gate_rr_guard = Column(Boolean, nullable=True)
    gate_no_trade_zones = Column(Boolean, nullable=True)
    gate_dynamic_risk = Column(Boolean, nullable=True)
    gate_confidence_v2 = Column(Boolean, nullable=True)
    gate_dedup = Column(Boolean, nullable=True)
    gate_compression_block = Column(Boolean, nullable=True)
    # Structural gates (Scenario Forensics)
    gate_structure_alignment = Column(Boolean, nullable=True)
    gate_sweep_required = Column(Boolean, nullable=True)
    gate_regime_block = Column(Boolean, nullable=True)

    # Pipeline outcome
    final_stage = Column(String(50), nullable=True)  # last gate reached
    blocked_reason = Column(String(200), nullable=True)
    signal_generated = Column(Boolean, default=False)

    # Signal details (only when signal_generated=True)
    signal_type = Column(String(10), nullable=True)  # BUY / SELL
    score = Column(Integer, nullable=True)
    close_price = Column(Float, nullable=True)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)

    # ── Feature Snapshot (25 fields) ─────────────────────────────────
    # Raw indicator values at signal time
    adx = Column(Float, nullable=True)
    rsi = Column(Float, nullable=True)
    ema_short = Column(Float, nullable=True)     # ema_fast
    ema_long = Column(Float, nullable=True)      # ema_slow
    ema_spread_pct = Column(Float, nullable=True) # (ema_fast - ema_slow) / ema_slow * 100
    macd_hist = Column(Float, nullable=True)
    supertrend_direction = Column(Integer, nullable=True)
    volume_ratio = Column(Float, nullable=True)   # volume / volume_sma

    # Factor strengths (raw -1.0 to 1.0)
    dmi_strength = Column(Float, nullable=True)
    ema_strength = Column(Float, nullable=True)

    # Score / confidence
    signal_score = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=True)     # confidence_v2 confidence_pct

    # Market context
    regime = Column(String(20), nullable=True)
    direction = Column(String(10), nullable=True)  # BUY / SELL

    # SL/TP metrics
    sl_source = Column(String(20), nullable=True)  # bos / atr / structural
    tp_distance_pct = Column(Float, nullable=True)  # (tp - entry) / entry * 100
    sl_distance_pct = Column(Float, nullable=True)  # (entry - sl) / entry * 100
    rr_ratio = Column(Float, nullable=True)         # reward / risk

    # Structure / liquidity
    has_bos = Column(Boolean, nullable=True)
    has_sweep = Column(Boolean, nullable=True)
    has_ob = Column(Boolean, nullable=True)
    ob_distance_pct = Column(Float, nullable=True)  # distance to nearest OB %

    # Context / multi-timeframe
    context_score = Column(Float, nullable=True)
    btc_trend_strength = Column(Float, nullable=True)  # btc_price / btc_ema200
    mtf_alignment_score = Column(Float, nullable=True)  # aligned_htf / required

    # ── Normalized features (v2.4.0 — Decision Intelligence) ────────
    atr_pct = Column(Float, nullable=True)              # atr / close * 100
    ema_slope_3 = Column(Float, nullable=True)           # EMA % change over 3 bars
    ema_slope_5 = Column(Float, nullable=True)           # EMA % change over 5 bars
    nearest_support_pct = Column(Float, nullable=True)   # distance to nearest support %
    nearest_resistance_pct = Column(Float, nullable=True) # distance to nearest resistance %
    regime_confidence = Column(Float, nullable=True)     # regime detection confidence
    gate_path = Column(Text, nullable=True)              # JSON: ordered gate outcome sequence

    # ── Strategy version + config snapshot ───────────────────────────
    strategy_version = Column(String(20), nullable=True)
    config_snapshot = Column(Text, nullable=True)  # JSON: key params

    # Linked signal for outcome tracking
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True, index=True)
    candidate_id = Column(Integer, ForeignKey("signal_candidates.id"), nullable=True, index=True)

    # Outcome tracking (filled later)
    outcome = Column(String(20), nullable=True)  # HIT_TP / HIT_SL / EXPIRED
    pnl_pct = Column(Float, nullable=True)

    # Execution snapshot (JSON)
    execution_snapshot = Column(Text, nullable=True)  # JSON: ExecutionSnapshot data

    # Hypothesis snapshot (JSON) — for ScenarioMemory tracking
    hypothesis_snapshot = Column(Text, nullable=True)  # JSON: Hypothesis data


class Database:
    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self._engine = create_async_engine(
            config.database_url,
            echo=False,
        )
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init(self):
        """Создаём таблицы при первом запуске + миграции"""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate()
        logger.info("Database initialized")

    async def _migrate(self):
        """Добавляем отсутствующие колонки в существующие таблицы."""
        async with self._engine.connect() as conn:
            # signals.factor_fingerprint (Task 6.1)
            result = await conn.execute(
                text("PRAGMA table_info(signals)")
            )
            columns = [row[1] for row in result.fetchall()]
            if "factor_fingerprint" not in columns:
                await conn.execute(
                    text("ALTER TABLE signals ADD COLUMN factor_fingerprint VARCHAR(255)")
                )
                await conn.commit()
                logger.info("Migration: added signals.factor_fingerprint")

            # signal_outcomes.risk_pct (portfolio risk gate)
            result = await conn.execute(
                text("PRAGMA table_info(signal_outcomes)")
            )
            outcome_columns = [row[1] for row in result.fetchall()]
            if "risk_pct" not in outcome_columns:
                await conn.execute(
                    text("ALTER TABLE signal_outcomes ADD COLUMN risk_pct FLOAT")
                )
                await conn.commit()
                logger.info("Migration: added signal_outcomes.risk_pct")

            # signals.confidence_v2_pct + confidence_v2_factors
            result = await conn.execute(
                text("PRAGMA table_info(signals)")
            )
            sig_columns = [row[1] for row in result.fetchall()]
            if "confidence_v2_pct" not in sig_columns:
                await conn.execute(
                    text("ALTER TABLE signals ADD COLUMN confidence_v2_pct FLOAT")
                )
                await conn.commit()
                logger.info("Migration: added signals.confidence_v2_pct")
            if "confidence_v2_factors" not in sig_columns:
                await conn.execute(
                    text("ALTER TABLE signals ADD COLUMN confidence_v2_factors TEXT")
                )
                await conn.commit()
                logger.info("Migration: added signals.confidence_v2_factors")

            # signal_candidates.outcome_signal_id → already exists via save_candidate
            # decision_traces table created by create_all above

            # ── Decision trace feature snapshot columns (v2.4.0) ───
            result = await conn.execute(
                text("PRAGMA table_info(decision_traces)")
            )
            trace_columns = [row[1] for row in result.fetchall()]
            trace_migrations = {
                "adx": "FLOAT", "rsi": "FLOAT", "ema_short": "FLOAT",
                "ema_long": "FLOAT", "ema_spread_pct": "FLOAT",
                "macd_hist": "FLOAT", "supertrend_direction": "INTEGER",
                "volume_ratio": "FLOAT", "dmi_strength": "FLOAT",
                "ema_strength": "FLOAT", "signal_score": "INTEGER",
                "confidence": "FLOAT", "regime": "VARCHAR(20)",
                "direction": "VARCHAR(10)", "sl_source": "VARCHAR(20)",
                "tp_distance_pct": "FLOAT", "sl_distance_pct": "FLOAT",
                "rr_ratio": "FLOAT", "has_bos": "BOOLEAN",
                "has_sweep": "BOOLEAN", "has_ob": "BOOLEAN",
                "ob_distance_pct": "FLOAT", "context_score": "FLOAT",
                "btc_trend_strength": "FLOAT", "mtf_alignment_score": "FLOAT",
                "strategy_version": "VARCHAR(20)", "config_snapshot": "TEXT",
                # Decision Intelligence (v2.4.0)
                "atr_pct": "FLOAT", "ema_slope_3": "FLOAT", "ema_slope_5": "FLOAT",
                "nearest_support_pct": "FLOAT", "nearest_resistance_pct": "FLOAT",
                "regime_confidence": "FLOAT", "gate_path": "TEXT",
                # Structural gates (Scenario Forensics)
                "gate_structure_alignment": "BOOLEAN",
                "gate_sweep_required": "BOOLEAN",
                "gate_regime_block": "BOOLEAN",
            }
            for col_name, col_type in trace_migrations.items():
                if col_name not in trace_columns:
                    await conn.execute(
                        text(f"ALTER TABLE decision_traces ADD COLUMN {col_name} {col_type}")
                    )
                    await conn.commit()
                    logger.info(f"Migration: added decision_traces.{col_name}")

    async def save_signal(
        self,
        symbol: str,
        timeframe: str,
        signal_type: str,
        close_price: float,
        sl: Optional[float],
        tp: Optional[float],
        score: int,
        reasons: List[str],
        confirmed: bool = False,
        factor_fingerprint: Optional[str] = None,
        confidence_v2_pct: Optional[float] = None,
        confidence_v2_factors: Optional[list] = None,
        entry_candle_open: Optional[datetime] = None,
        # Execution snapshot fields
        entry_price_source: Optional[str] = None,
        entry_open: Optional[float] = None,
        entry_mid: Optional[float] = None,
        entry_bid: Optional[float] = None,
        entry_ask: Optional[float] = None,
        entry_spread: Optional[float] = None,
        entry_atr: Optional[float] = None,
        entry_tick_size: Optional[float] = None,
        signal_detected_at: Optional[datetime] = None,
        telegram_sent_at: Optional[datetime] = None,
        exchange_notified_at: Optional[datetime] = None,
    ) -> Signal:
        async with self._session_factory() as session:
            factors_json = json.dumps(confidence_v2_factors) if confidence_v2_factors else None
            sig = Signal(
                symbol=symbol,
                timeframe=timeframe,
                signal_type=signal_type,
                close_price=close_price,
                sl=sl,
                tp=tp,
                score=score,
                reasons="\n".join(reasons),
                confirmed=confirmed,
                factor_fingerprint=factor_fingerprint,
                confidence_v2_pct=confidence_v2_pct,
                confidence_v2_factors=factors_json,
                sent_at=datetime.now(timezone.utc),
                entry_candle_open=entry_candle_open,
                entry_price_source=entry_price_source,
                entry_open=entry_open,
                entry_mid=entry_mid,
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                entry_spread=entry_spread,
                entry_atr=entry_atr,
                entry_tick_size=entry_tick_size,
                signal_detected_at=signal_detected_at,
                telegram_sent_at=telegram_sent_at,
                exchange_notified_at=exchange_notified_at,
            )
            session.add(sig)
            await session.commit()
            await session.refresh(sig)
            return sig

    async def save_candidate(
        self,
        symbol: str,
        timeframe: str,
        *,
        signal_type: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        score: Optional[int] = None,
        # Factor strengths
        st_strength: Optional[float] = None,
        ema_strength: Optional[float] = None,
        macd_strength: Optional[float] = None,
        rsi_strength: Optional[float] = None,
        vol_strength: Optional[float] = None,
        adx_strength: Optional[float] = None,
        dmi_strength: Optional[float] = None,
        weighted_score: Optional[float] = None,
        # Raw indicators
        adx: Optional[float] = None,
        rsi: Optional[float] = None,
        ema_fast: Optional[float] = None,
        ema_slow: Optional[float] = None,
        ema_trend: Optional[float] = None,
        macd_hist: Optional[float] = None,
        dmi_plus: Optional[float] = None,
        dmi_minus: Optional[float] = None,
        atr: Optional[float] = None,
        close_price: Optional[float] = None,
        volume: Optional[float] = None,
        volume_sma: Optional[float] = None,
        supertrend_direction: Optional[int] = None,
        # Regime
        regime: Optional[str] = None,
        regime_confidence: Optional[float] = None,
        # Gate / metadata
        blocked_gate: Optional[str] = None,
        factor_fingerprint: Optional[str] = None,
        confidence_v2_pct: Optional[float] = None,
        has_trigger: Optional[bool] = None,
        has_leading_trigger: Optional[bool] = None,
        mtf_aligned: Optional[bool] = None,
    ) -> SignalCandidate:
        """Log every evaluate() call — pass or fail."""
        async with self._session_factory() as session:
            cand = SignalCandidate(
                symbol=symbol,
                timeframe=timeframe,
                signal_type=signal_type,
                rejection_reason=rejection_reason,
                score=score,
                st_strength=st_strength,
                ema_strength=ema_strength,
                macd_strength=macd_strength,
                rsi_strength=rsi_strength,
                vol_strength=vol_strength,
                adx_strength=adx_strength,
                dmi_strength=dmi_strength,
                weighted_score=weighted_score,
                adx=adx,
                rsi=rsi,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                ema_trend=ema_trend,
                macd_hist=macd_hist,
                dmi_plus=dmi_plus,
                dmi_minus=dmi_minus,
                atr=atr,
                close_price=close_price,
                volume=volume,
                volume_sma=volume_sma,
                supertrend_direction=supertrend_direction,
                regime=regime,
                regime_confidence=regime_confidence,
                blocked_gate=blocked_gate,
                factor_fingerprint=factor_fingerprint,
                confidence_v2_pct=confidence_v2_pct,
                has_trigger=has_trigger,
                has_leading_trigger=has_leading_trigger,
                mtf_aligned=mtf_aligned,
            )
            session.add(cand)
            await session.commit()
            await session.refresh(cand)
            return cand

    async def update_candidate_outcome(
        self, candidate_id: int, outcome: str, pnl_pct: float, signal_id: Optional[int] = None
    ) -> None:
        """Link an outcome (HIT_TP/HIT_SL/EXPIRED) to a candidate row."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalCandidate).where(SignalCandidate.id == candidate_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.outcome = outcome
                row.pnl_pct = pnl_pct
                row.outcome_signal_id = signal_id
                await session.commit()

    async def link_latest_candidate(
        self, symbol: str, timeframe: str, signal_id: int, factor_fingerprint: str
    ) -> None:
        """Link the most recent candidate (no signal_id yet) to a saved signal.

        Called after save_signal() in scanner to connect the candidate row
        with its outcome tracking.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalCandidate)
                .where(
                    SignalCandidate.symbol == symbol,
                    SignalCandidate.timeframe == timeframe,
                    SignalCandidate.outcome_signal_id.is_(None),
                    SignalCandidate.signal_type.isnot(None),
                )
                .order_by(SignalCandidate.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.outcome_signal_id = signal_id
                row.factor_fingerprint = factor_fingerprint
                await session.commit()

    async def update_candidate_outcome_by_signal(
        self, signal_id: int, outcome: str, pnl_pct: float
    ) -> None:
        """Update candidate outcome by linked signal_id (called by outcome_tracker)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalCandidate).where(SignalCandidate.outcome_signal_id == signal_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.outcome = outcome
                row.pnl_pct = pnl_pct
                await session.commit()

    async def get_last_signal(self, symbol: str, timeframe: str) -> Optional[Signal]:
        """Последний сигнал по символу и таймфрейму"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Signal)
                .where(Signal.symbol == symbol, Signal.timeframe == timeframe)
                .order_by(desc(Signal.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_recent_signals(self, limit: int = 10) -> List[Signal]:
        """Последние N сигналов"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Signal).order_by(desc(Signal.created_at)).limit(limit)
            )
            return list(result.scalars().all())

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BotSetting).where(BotSetting.key == key)
            )
            row = result.scalar_one_or_none()
            return row.value if row else default

    async def set_setting(self, key: str, value: str):
        async with self._session_factory() as session:
            result = await session.execute(
                select(BotSetting).where(BotSetting.key == key)
            )
            row = result.scalar_one_or_none()
            if row:
                row.value = value
                row.updated_at = datetime.now(timezone.utc)
            else:
                session.add(BotSetting(key=key, value=value))
            await session.commit()

    async def get_cooldown(
        self, symbol: str, timeframe: str
    ) -> Optional[datetime]:
        key = f"cooldown:{symbol}:{timeframe}"
        val = await self.get_setting(key, "")
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    async def set_cooldown(
        self, symbol: str, timeframe: str, ts: datetime
    ) -> None:
        key = f"cooldown:{symbol}:{timeframe}"
        await self.set_setting(key, ts.isoformat())

    async def get_dynamic_symbols(self) -> Optional[list[str]]:
        """None — динамический список не задан, использовать env SYMBOLS."""
        val = await self.get_setting("dynamic_symbols", "")
        if not val:
            return None
        return [s.strip() for s in val.split(",") if s.strip()]

    async def set_dynamic_symbols(self, symbols: list[str]) -> None:
        await self.set_setting("dynamic_symbols", ",".join(symbols))

    async def get_disabled_symbols(self) -> Optional[list[str]]:
        """None — список отключённых символов не задан."""
        val = await self.get_setting("disabled_symbols", "")
        if not val:
            return None
        return [s.strip() for s in val.split(",") if s.strip()]

    async def set_disabled_symbols(self, symbols: list[str]) -> None:
        await self.set_setting("disabled_symbols", ",".join(symbols))

    async def save_context_snapshot(
        self,
        symbol: str,
        signal_id: Optional[int],
        verdict: str,
        confidence: float,
        score: float,
        fear_greed: Optional[int] = None,
        funding_rate: Optional[float] = None,
        long_short_ratio: Optional[float] = None,
        open_interest_delta: Optional[float] = None,
        news_sentiment: Optional[float] = None,
        raw_json: Optional[str] = None,
    ) -> ContextSnapshotModel:
        async with self._session_factory() as session:
            snap = ContextSnapshotModel(
                symbol=symbol,
                signal_id=signal_id,
                verdict=verdict,
                confidence=confidence,
                score=score,
                fear_greed=fear_greed,
                funding_rate=funding_rate,
                long_short_ratio=long_short_ratio,
                open_interest_delta=open_interest_delta,
                news_sentiment=news_sentiment,
                raw_json=raw_json,
            )
            session.add(snap)
            await session.commit()
            await session.refresh(snap)
            return snap

    async def save_decision_trace(
        self,
        symbol: str,
        timeframe: str,
        *,
        gate_results: dict[str, Optional[bool]],
        final_stage: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        signal_generated: bool = False,
        signal_type: Optional[str] = None,
        score: Optional[int] = None,
        close_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        signal_id: Optional[int] = None,
        candidate_id: Optional[int] = None,
        # Feature snapshot
        features: Optional[dict] = None,
        strategy_version: Optional[str] = None,
        config_snapshot: Optional[str] = None,
        gate_path: Optional[str] = None,
        # Execution snapshot
        execution_snapshot: Optional[dict] = None,
        # Hypothesis snapshot
        hypothesis_snapshot: Optional[dict] = None,
    ) -> DecisionTrace:
        """Save a complete decision trace for one pipeline pass."""
        f = features or {}
        async with self._session_factory() as session:
            trace = DecisionTrace(
                symbol=symbol,
                timeframe=timeframe,
                gate_cooldown=gate_results.get("cooldown"),
                gate_portfolio_risk=gate_results.get("portfolio_risk"),
                gate_btc_global_trend=gate_results.get("btc_global_trend"),
                gate_indicators=gate_results.get("indicators"),
                gate_confirm_tf=gate_results.get("confirm_tf"),
                gate_signal_engine=gate_results.get("signal_engine"),
                gate_distance_filter=gate_results.get("distance_filter"),
                gate_tp_path=gate_results.get("tp_path"),
                gate_mtf_alignment=gate_results.get("mtf_alignment"),
                gate_btc_correlation=gate_results.get("btc_correlation"),
                gate_eth_correlation=gate_results.get("eth_correlation"),
                gate_volatility=gate_results.get("volatility"),
                gate_context_timeout=gate_results.get("context_timeout"),
                gate_context_block=gate_results.get("context_block"),
                gate_context_min_verdict=gate_results.get("context_min_verdict"),
                gate_news=gate_results.get("news"),
                gate_sl_distance=gate_results.get("sl_distance"),
                gate_rr_guard=gate_results.get("rr_guard"),
                gate_no_trade_zones=gate_results.get("no_trade_zones"),
                gate_dynamic_risk=gate_results.get("dynamic_risk"),
                gate_confidence_v2=gate_results.get("confidence_v2"),
                gate_dedup=gate_results.get("dedup"),
                gate_compression_block=gate_results.get("compression_block"),
                # Structural gates (Scenario Forensics)
                gate_structure_alignment=gate_results.get("structure_alignment"),
                gate_sweep_required=gate_results.get("sweep_required"),
                gate_regime_block=gate_results.get("regime_block"),
                final_stage=final_stage,
                blocked_reason=blocked_reason,
                signal_generated=signal_generated,
                signal_type=signal_type,
                score=score,
                close_price=close_price,
                sl=sl,
                tp=tp,
                signal_id=signal_id,
                candidate_id=candidate_id,
                # Feature snapshot
                adx=f.get("adx"),
                rsi=f.get("rsi"),
                ema_short=f.get("ema_short"),
                ema_long=f.get("ema_long"),
                ema_spread_pct=f.get("ema_spread_pct"),
                macd_hist=f.get("macd_hist"),
                supertrend_direction=f.get("supertrend_direction"),
                volume_ratio=f.get("volume_ratio"),
                dmi_strength=f.get("dmi_strength"),
                ema_strength=f.get("ema_strength"),
                signal_score=f.get("signal_score"),
                confidence=f.get("confidence"),
                regime=f.get("regime"),
                direction=f.get("direction"),
                sl_source=f.get("sl_source"),
                tp_distance_pct=f.get("tp_distance_pct"),
                sl_distance_pct=f.get("sl_distance_pct"),
                rr_ratio=f.get("rr_ratio"),
                has_bos=f.get("has_bos"),
                has_sweep=f.get("has_sweep"),
                has_ob=f.get("has_ob"),
                ob_distance_pct=f.get("ob_distance_pct"),
                context_score=f.get("context_score"),
                btc_trend_strength=f.get("btc_trend_strength"),
                mtf_alignment_score=f.get("mtf_alignment_score"),
                # Decision Intelligence features
                atr_pct=f.get("atr_pct"),
                ema_slope_3=f.get("ema_slope_3"),
                ema_slope_5=f.get("ema_slope_5"),
                nearest_support_pct=f.get("nearest_support_pct"),
                nearest_resistance_pct=f.get("nearest_resistance_pct"),
                regime_confidence=f.get("regime_confidence"),
                gate_path=gate_path or f.get("gate_path"),
                # Strategy version + config
                strategy_version=strategy_version,
                config_snapshot=config_snapshot,
                # Execution snapshot
                execution_snapshot=json.dumps(execution_snapshot) if execution_snapshot else None,
                # Hypothesis snapshot
                hypothesis_snapshot=json.dumps(hypothesis_snapshot) if hypothesis_snapshot else None,
            )
            session.add(trace)
            await session.commit()
            await session.refresh(trace)
            return trace

    async def link_trace_to_signal(
        self, trace_id: int, signal_id: int
    ) -> None:
        """Link a decision trace to a saved signal (replaces link_latest_candidate)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.signal_id = signal_id
                await session.commit()

    async def link_trace_to_candidate(
        self, trace_id: int, candidate_id: int
    ) -> None:
        """Link a decision trace to a saved candidate."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.candidate_id = candidate_id
                await session.commit()

    async def update_trace_outcome(
        self, trace_id: int, outcome: str, pnl_pct: float
    ) -> None:
        """Update outcome for a decision trace (called by outcome_tracker)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.id == trace_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.outcome = outcome
                row.pnl_pct = pnl_pct
                await session.commit()

    async def get_trace_by_signal_id(self, signal_id: int) -> Optional[DecisionTrace]:
        """Get decision trace by signal_id (for ScenarioMemory tracking)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DecisionTrace).where(DecisionTrace.signal_id == signal_id)
            )
            return result.scalar_one_or_none()

    async def get_trace_stats(self, symbol: Optional[str] = None) -> list[dict]:
        """Aggregate gate pass/drop counts for funnel analysis.

        Returns a list of dicts, one per gate, ordered by pipeline position:
        {gate, entered, passed, dropped, wr_downstream, pf_downstream}
        """
        from sqlalchemy import case, func

        async with self._session_factory() as session:
            base_query = select(DecisionTrace)
            if symbol:
                base_query = base_query.where(DecisionTrace.symbol == symbol)

            result = await session.execute(base_query)
            traces = list(result.scalars().all())

        if not traces:
            return []

        gate_order = [
            "cooldown", "portfolio_risk", "btc_global_trend", "indicators",
            "confirm_tf", "signal_engine", "distance_filter", "tp_path",
            "mtf_alignment", "btc_correlation", "eth_correlation", "volatility",
            "context_timeout", "context_block", "context_min_verdict",
            "news", "sl_distance", "rr_guard", "no_trade_zones",
            "dynamic_risk", "confidence_v2", "dedup",
        ]
        gate_col_map = {
            "cooldown": "gate_cooldown",
            "portfolio_risk": "gate_portfolio_risk",
            "btc_global_trend": "gate_btc_global_trend",
            "indicators": "gate_indicators",
            "confirm_tf": "gate_confirm_tf",
            "signal_engine": "gate_signal_engine",
            "distance_filter": "gate_distance_filter",
            "tp_path": "gate_tp_path",
            "mtf_alignment": "gate_mtf_alignment",
            "btc_correlation": "gate_btc_correlation",
            "eth_correlation": "gate_eth_correlation",
            "volatility": "gate_volatility",
            "context_timeout": "gate_context_timeout",
            "context_block": "gate_context_block",
            "context_min_verdict": "gate_context_min_verdict",
            "news": "gate_news",
            "sl_distance": "gate_sl_distance",
            "rr_guard": "gate_rr_guard",
            "no_trade_zones": "gate_no_trade_zones",
            "dynamic_risk": "gate_dynamic_risk",
            "confidence_v2": "gate_confidence_v2",
            "dedup": "gate_dedup",
        }

        stats = []
        for gate in gate_order:
            col = gate_col_map[gate]
            entered = sum(1 for t in traces if getattr(t, col, None) is not None)
            passed = sum(1 for t in traces if getattr(t, col, None) is True)
            dropped = entered - passed

            # Downstream: signals that passed this gate and also passed ALL later gates
            gate_idx = gate_order.index(gate)
            downstream_signals = []
            for t in traces:
                if getattr(t, col, None) is not True:
                    continue
                all_later_pass = True
                for later_gate in gate_order[gate_idx + 1:]:
                    later_col = gate_col_map[later_gate]
                    later_val = getattr(t, later_col, None)
                    if later_val is False:
                        all_later_pass = False
                        break
                if all_later_pass and t.signal_generated:
                    downstream_signals.append(t)

            wins = sum(1 for t in downstream_signals if t.outcome == "HIT_TP")
            total_closed = sum(
                1 for t in downstream_signals
                if t.outcome in ("HIT_TP", "HIT_SL")
            )
            wr = round(wins / total_closed * 100, 1) if total_closed > 0 else None

            pnls = [t.pnl_pct for t in downstream_signals if t.pnl_pct is not None]
            pf = None
            if pnls:
                gross_profit = sum(p for p in pnls if p > 0)
                gross_loss = abs(sum(p for p in pnls if p < 0))
                pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

            stats.append({
                "gate": gate,
                "entered": entered,
                "passed": passed,
                "dropped": dropped,
                "wr_downstream": wr,
                "pf_downstream": pf,
            })

        return stats

    async def get_counterfactual(
        self, disable_gate: str, symbol: Optional[str] = None
    ) -> dict:
        """Counterfactual: how many signals would we get if we disabled a gate?

        Returns: {original_count, would_add, new_total, wr_change, pf_change}
        """
        gate_col_map = {
            "cooldown": "gate_cooldown",
            "portfolio_risk": "gate_portfolio_risk",
            "btc_global_trend": "gate_btc_global_trend",
            "indicators": "gate_indicators",
            "confirm_tf": "gate_confirm_tf",
            "signal_engine": "gate_signal_engine",
            "distance_filter": "gate_distance_filter",
            "tp_path": "gate_tp_path",
            "mtf_alignment": "gate_mtf_alignment",
            "btc_correlation": "gate_btc_correlation",
            "eth_correlation": "gate_eth_correlation",
            "volatility": "gate_volatility",
            "context_timeout": "gate_context_timeout",
            "context_block": "gate_context_block",
            "context_min_verdict": "gate_context_min_verdict",
            "news": "gate_news",
            "sl_distance": "gate_sl_distance",
            "rr_guard": "gate_rr_guard",
            "no_trade_zones": "gate_no_trade_zones",
            "dynamic_risk": "gate_dynamic_risk",
            "confidence_v2": "gate_confidence_v2",
            "dedup": "gate_dedup",
        }
        gate_order = list(gate_col_map.keys())

        if disable_gate not in gate_col_map:
            return {"error": f"Unknown gate: {disable_gate}"}

        disable_col = gate_col_map[disable_gate]
        disable_idx = gate_order.index(disable_gate)

        async with self._session_factory() as session:
            base_query = select(DecisionTrace)
            if symbol:
                base_query = base_query.where(DecisionTrace.symbol == symbol)
            result = await session.execute(base_query)
            traces = list(result.scalars().all())

        if not traces:
            return {"error": "no data"}

        # Current signals: those that passed ALL gates
        current_signals = []
        would_add = []

        for t in traces:
            # Check if the candidate would have passed if this gate was removed
            would_pass_without_gate = True
            blocked_by_disabled_gate = False

            for i, gate in enumerate(gate_order):
                col = gate_col_map[gate]
                val = getattr(t, col, None)
                if gate == disable_gate:
                    if val is False:
                        blocked_by_disabled_gate = True
                    continue  # skip the disabled gate
                if val is False:
                    would_pass_without_gate = False
                    break

            if t.signal_generated:
                current_signals.append(t)
            elif blocked_by_disabled_gate and would_pass_without_gate:
                would_add.append(t)

        # Stats
        current_wins = sum(1 for t in current_signals if t.outcome == "HIT_TP")
        current_losses = sum(1 for t in current_signals if t.outcome == "HIT_SL")
        current_pnls = [t.pnl_pct for t in current_signals if t.pnl_pct is not None]

        add_wins = sum(1 for t in would_add if t.outcome == "HIT_TP")
        add_losses = sum(1 for t in would_add if t.outcome == "HIT_SL")
        add_pnls = [t.pnl_pct for t in would_add if t.pnl_pct is not None]

        # Projected totals
        total_wins = current_wins + add_wins
        total_losses = current_losses + add_losses
        total_closed = total_wins + total_losses
        new_wr = round(total_wins / total_closed * 100, 1) if total_closed > 0 else None

        all_pnls = current_pnls + add_pnls
        gross_profit = sum(p for p in all_pnls if p > 0)
        gross_loss = abs(sum(p for p in all_pnls if p < 0))
        new_pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

        cur_wr = None
        if current_signals:
            cur_closed = current_wins + current_losses
            cur_wr = round(current_wins / cur_closed * 100, 1) if cur_closed > 0 else None

        return {
            "gate_disabled": disable_gate,
            "original_count": len(current_signals),
            "would_add": len(would_add),
            "new_total": len(current_signals) + len(would_add),
            "wr_original": cur_wr,
            "wr_new": new_wr,
            "wr_change": round(new_wr - cur_wr, 1) if new_wr is not None and cur_wr is not None else None,
            "pf_new": new_pf,
        }

    async def get_signal(self, signal_id: int) -> Optional[Signal]:
        """Получить сигнал по ID (нужен outcome_tracker)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )
            return result.scalar_one_or_none()

    async def create_outcome(self, signal_id: int, risk_pct: float = 0.0) -> "SignalOutcome":
        async with self._session_factory() as session:
            outcome = SignalOutcome(signal_id=signal_id, status="OPEN", risk_pct=risk_pct)
            session.add(outcome)
            await session.commit()
            await session.refresh(outcome)
            return outcome

    async def get_open_outcomes(self) -> list["SignalOutcome"]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome).where(SignalOutcome.status == "OPEN")
            )
            return list(result.scalars().all())

    async def get_active_signals_count(self) -> int:
        """Количество активных (незакрытых) сигналов."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome).where(SignalOutcome.status == "OPEN")
            )
            return len(list(result.scalars().all()))

    async def get_active_signals_count_by_symbol(self, symbol: str) -> int:
        """Количество активных сигналов по конкретному символу."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome)
                .join(Signal, SignalOutcome.signal_id == Signal.id)
                .where(SignalOutcome.status == "OPEN", Signal.symbol == symbol)
            )
            return len(list(result.scalars().all()))

    async def get_portfolio_risk_sum(self) -> float:
        """Суммарный risk_pct всех активных позиций."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome).where(SignalOutcome.status == "OPEN")
            )
            outcomes = list(result.scalars().all())
            return sum(o.risk_pct or 0.0 for o in outcomes)

    async def get_outcomes_since(self, since: datetime) -> list["SignalOutcome"]:
        """Get all closed outcomes since a given time, ordered by closed_at desc."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome)
                .where(
                    SignalOutcome.status != "OPEN",
                    SignalOutcome.closed_at >= since,
                )
                .order_by(SignalOutcome.closed_at.desc())
            )
            return list(result.scalars().all())

    async def close_outcome(
        self, outcome_id: int, status: str, close_price: float, pnl_pct: float
    ) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome).where(SignalOutcome.id == outcome_id)
            )
            row = result.scalar_one()
            row.status = status
            row.close_price = close_price
            row.pnl_pct = pnl_pct
            row.closed_at = datetime.now(timezone.utc)
            row.checked_at = datetime.now(timezone.utc)
            await session.commit()

    async def update_signal_excursion(
        self, signal_id: int, mfe_pct: float, mae_pct: float
    ) -> None:
        """Update MFE/MAE for a signal after closure."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )
            row = result.scalar_one()
            row.mfe_pct = mfe_pct
            row.mae_pct = mae_pct
            await session.commit()

    async def update_signal_execution_latency(
        self,
        signal_id: int,
        telegram_sent_at: Optional[datetime] = None,
        exchange_notified_at: Optional[datetime] = None,
    ) -> None:
        """Update execution latency timestamps."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )
            row = result.scalar_one()
            if telegram_sent_at is not None:
                row.telegram_sent_at = telegram_sent_at
            if exchange_notified_at is not None:
                row.exchange_notified_at = exchange_notified_at
            await session.commit()

    async def touch_outcome_checked(self, outcome_id: int) -> None:
        """Update checked_at for an open outcome without closing it."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome).where(SignalOutcome.id == outcome_id)
            )
            row = result.scalar_one()
            row.checked_at = datetime.now(timezone.utc)
            await session.commit()

    async def get_outcome_stats(self) -> dict:
        async with self._session_factory() as session:
            closed = await session.execute(
                select(SignalOutcome).where(SignalOutcome.status != "OPEN")
            )
            closed_rows = list(closed.scalars().all())
            opened = await session.execute(
                select(SignalOutcome).where(SignalOutcome.status == "OPEN")
            )
            opened_rows = list(opened.scalars().all())
        pnls = [r.pnl_pct for r in closed_rows if r.pnl_pct is not None]
        return {
            "closed": len(closed_rows),
            "open": len(opened_rows),
            "wins": sum(1 for r in closed_rows if r.status == "HIT_TP"),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "best_pnl": max(pnls) if pnls else 0.0,
            "worst_pnl": min(pnls) if pnls else 0.0,
        }

    async def get_open_trades_with_signals(self) -> list[dict]:
        """Get all open outcomes joined with signal details for the web dashboard."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(SignalOutcome, Signal)
                .join(Signal, SignalOutcome.signal_id == Signal.id)
                .where(SignalOutcome.status == "OPEN")
                .order_by(Signal.created_at.desc())
            )
            rows = result.all()
            trades = []
            for outcome, signal in rows:
                trades.append({
                    "id": signal.id,
                    "symbol": signal.symbol,
                    "timeframe": signal.timeframe,
                    "signal_type": signal.signal_type,
                    "entry": signal.close_price,
                    "sl": signal.sl,
                    "tp": signal.tp,
                    "sent_at": (signal.sent_at or signal.created_at).isoformat() if (signal.sent_at or signal.created_at) else None,
                    "outcome_id": outcome.id,
                })
            return trades

    async def get_historical_winrate(
        self, factor_fingerprint: str, min_samples: int = 5
    ) -> Optional[float]:
        """Return historical winrate (0-100) for a given factor fingerprint.

        Returns None if not enough samples (less than min_samples).
        Winrate = HIT_TP / (HIT_TP + HIT_SL) * 100
        """
        async with self._session_factory() as session:
            # Find all signals with this fingerprint that have closed outcomes
            result = await session.execute(
                select(Signal.id, Signal.signal_type)
                .where(Signal.factor_fingerprint == factor_fingerprint)
            )
            signal_rows = result.all()
            if not signal_rows:
                return None

            signal_ids = [row[0] for row in signal_rows]
            outcomes = await session.execute(
                select(SignalOutcome)
                .where(
                    SignalOutcome.signal_id.in_(signal_ids),
                    SignalOutcome.status.in_(["HIT_TP", "HIT_SL"]),
                )
            )
            closed = list(outcomes.scalars().all())

            if len(closed) < min_samples:
                return None

            wins = sum(1 for o in closed if o.status == "HIT_TP")
            return round(wins / len(closed) * 100, 1)


db = Database()
