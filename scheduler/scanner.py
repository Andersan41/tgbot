"""
scheduler/scanner.py — Основной сканер рынка.

ICT Core pipeline: Pattern Engine → Feature Builder → Probability Engine → Risk Engine
"""
import asyncio
import collections
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger
from config.settings import config, get_active_symbols, VERSION, build_config_snapshot
from data.exchange_client import exchange_client
from indicators.engine import IndicatorValues
from strategy.signal_engine import SignalResult, SignalType
from storage.database import db
from context.analyzer import context_engine
from context.scorer import context_scorer, ContextVerdict
from monitoring.metrics import scan_duration_seconds, signals_total
from market_structure.structure import check_mtf_alignment, get_htf_directional_bias
from market_structure.htf_bias import get_htf_bias, HTFBias, extract_structure_dict
from market_structure.htf_bias_v2 import get_htf_bias_v2, HTFBiasResult
from risk.market_regime import RegimeDetector, MarketRegime
from scheduler.circuit_breaker import is_circuit_breaker_active, check_recent_losses
from storage.trace import DecisionTraceBuilder, ExecutionSnapshot

# ── Shadow mode imports ──
from strategy.market_phase_engine import MarketPhaseEngine
from strategy.scenario_engine import ScenarioEngine
from strategy.trade_thesis import TradeThesisManager
from strategy.scenario_memory import scenario_memory

# Shadow mode singletons
_market_phase_engine = MarketPhaseEngine()
_scenario_engine = ScenarioEngine()
_thesis_manager = TradeThesisManager()

# ── Timeframe-dependent cooldown ───────────────────────────────────────
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}


def _smt_to_score(smt_result) -> float:
    """Convert SMTResult to numeric score for Probability Engine.

    Returns: -1.0 (bearish SMT) to 1.0 (bullish SMT), 0.0 for None/neutral.
    """
    if smt_result is None:
        return 0.0
    if smt_result.direction == "bullish":
        return 1.0
    elif smt_result.direction == "bearish":
        return -1.0
    return 0.0

# ── EMA Spread History for Regime Detection ────────────────────────────
# Stores rolling EMA spread values per symbol/timeframe across scan cycles.
# Used by _detect_regime() to compute ema_spread_trend (rising/falling/stable).
_ema_spread_history: dict[str, list[float]] = {}


def get_cooldown_minutes(timeframe: str, base_minutes: int, multiplier: float) -> int:
    """Effective cooldown = max(base_minutes, timeframe_minutes × multiplier)."""
    tf_minutes = _TF_MINUTES.get(timeframe, 60)
    return max(base_minutes, int(tf_minutes * multiplier))


# ── Signal Funnel Logging ──────────────────────────────────────────────
_FUNNEL_GATES = [
    "cooldown", "portfolio_risk", "indicators", "pattern_engine",
    "structure_alignment", "sweep_required", "regime_block",
    "sl_tp", "risk_engine", "dedup",
]


class _FunnelCounter:
    """Tracks per-scan-cycle funnel statistics."""
    def __init__(self):
        self.entered = 0
        self.passed = 0
        self.blocked_by = collections.Counter()

    def log_gate(self, symbol: str, tf: str, gate: str, status: str, detail: str = ""):
        tag = f"[FUNNEL] {symbol} {tf}"
        if status == "PASS":
            logger.debug(f"{tag} → {gate}: PASS")
        elif status == "BLOCKED":
            reason = f" ({detail})" if detail else ""
            logger.bind(tags="signal_block").info(f"{tag} → {gate}: BLOCKED{reason}")
            self.blocked_by[gate] += 1
        elif status == "ENTER":
            self.entered += 1

    def log_summary(self):
        if self.entered == 0:
            return
        parts = [f"entered={self.entered}", f"sent={self.passed}"]
        for gate, count in self.blocked_by.most_common():
            parts.append(f"{gate}={count}")
        logger.info(f"[FUNNEL SUMMARY] {', '.join(parts)}")


_current_funnel = _FunnelCounter()


# ── Dynamic Thesis caches (per symbol/timeframe) ─────────────────────
# These persist across scan cycles so the graph updates in-place
# instead of rebuilding from scratch every time.
_dynamic_graphs: dict = {}         # key = f"{symbol}_{timeframe}" → LiquidityGraph
_dynamic_theses: dict = {}         # key = f"{symbol}_{timeframe}" → DynamicTradeThesis
_dynamic_bar_counters: dict = {}   # key = f"{symbol}_{timeframe}" → int (bar count)


# ── Helpers ────────────────────────────────────────────────────────────

async def _is_cooldown_active(symbol: str, timeframe: str) -> tuple[bool, int]:
    """Check if cooldown is active for symbol+timeframe."""
    last = await db.get_cooldown(symbol, timeframe)
    if last is None:
        return False, 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - last
    effective = get_cooldown_minutes(
        timeframe, config.signal_cooldown_minutes, config.signal_cooldown_tf_multiplier
    )
    return delta < timedelta(minutes=effective), effective


async def _set_cooldown(symbol: str, timeframe: str) -> None:
    await db.set_cooldown(symbol, timeframe, datetime.now(timezone.utc))


async def _get_indicators(symbol: str, timeframe: str):
    df = await exchange_client.fetch_ohlcv(
        symbol,
        timeframe,
        limit=config.trading.candles_limit,
    )

    if df is None:
        logger.warning(f"OHLCV unavailable for {symbol} {timeframe}")
        return None

    if hasattr(df, "empty") and getattr(df, "empty", False) is True:
        logger.warning(f"Empty dataframe for {symbol} {timeframe}")
        return None

    from indicators.engine import indicator_engine
    ind = indicator_engine.calculate(df, symbol, timeframe)

    if ind is None:
        logger.warning(f"Indicator calculation failed for {symbol} {timeframe}")
        return None

    return ind, df


def _detect_regime(ind: IndicatorValues, df, symbol: str = "", timeframe: str = "") -> Optional[MarketRegime]:
    """Detect market regime from indicator values and OHLCV data.

    Maintains a rolling EMA spread history per symbol/timeframe across scan cycles
    to accurately detect rising/falling EMA spread trends.
    """
    try:
        adx = float(ind.adx) if ind.adx is not None else 20.0
        current_atr = float(ind.atr) if ind.atr is not None else 0.0
        current_volume = float(ind.volume) if ind.volume is not None else 0.0

        if len(df) >= 10:
            atr_history = []
            for _, row in df.tail(config.risk.regime_atr_lookback).iterrows():
                high_low = row['high'] - row['low']
                atr_history.append(float(high_low))
        else:
            atr_history = [current_atr] * 10

        ema_fast = float(ind.ema_fast) if ind.ema_fast is not None else 0.0
        ema_slow = float(ind.ema_slow) if ind.ema_slow is not None else 0.0
        current_spread = abs(ema_fast - ema_slow) if ema_slow > 0 else 0.0

        # Rolling EMA spread history across scan cycles
        history_key = f"{symbol}_{timeframe}" if symbol and timeframe else "_global"
        if history_key not in _ema_spread_history:
            _ema_spread_history[history_key] = []
        _ema_spread_history[history_key].append(current_spread)
        # Keep last N values based on config window
        max_history = max(config.risk.regime_ema_spread_window * 2, 10)
        _ema_spread_history[history_key] = _ema_spread_history[history_key][-max_history:]
        ema_spread_history = list(_ema_spread_history[history_key])

        if len(df) >= 10:
            volume_history = [float(row['volume']) for _, row in df.tail(20).iterrows()]
        else:
            volume_history = [current_volume] * 10

        detector = RegimeDetector(
            adx=adx,
            atr_history=atr_history,
            ema_spread_history=ema_spread_history,
            volume_history=volume_history,
            current_atr=current_atr,
            current_volume=current_volume,
        )
        return detector.detect()
    except Exception as e:
        logger.warning(f"Regime detection failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
#  ICT Core Pipeline
# ══════════════════════════════════════════════════════════════════════

async def scan_symbol_v2(symbol: str, timeframe: str, notify_callback, blocked_callback=None) -> Optional[SignalResult]:
    """ICT Core pipeline: Pattern Engine → Feature Builder → Probability Engine → Risk Engine.

    Hard gates: Cooldown, Portfolio Risk, Data Integrity, R:R, SL limits, Dedup.
    All indicator-based filtering removed.
    """
    from strategy.pattern_engine import pattern_engine
    from strategy.feature_builder import feature_builder
    from strategy.probability_engine import probability_engine
    from risk.engine import risk_engine, PortfolioState
    from strategy.signal_engine import _calculate_sl_tp

    with scan_duration_seconds.labels(timeframe=timeframe).time():
        _current_funnel.log_gate(symbol, timeframe, "start", "ENTER")
        trace = DecisionTraceBuilder(symbol, timeframe)

        # ═══ Phase 0: Hard Gates (capital protection) ═══

        # 0.1 Cooldown
        cooldown_active, cooldown_minutes = await _is_cooldown_active(symbol, timeframe)
        if cooldown_active:
            _current_funnel.log_gate(symbol, timeframe, "cooldown", "BLOCKED",
                                     f"required {cooldown_minutes}m")
            trace.blocked("cooldown", f"cooldown {cooldown_minutes}m active")
            await trace.save(db)
            return None
        trace.passed("cooldown")
        _current_funnel.log_gate(symbol, timeframe, "cooldown", "PASS")

        # 0.2 Portfolio risk
        max_sigs = config.max_active_signals
        max_risk = config.max_portfolio_risk_pct
        active_count = await db.get_active_signals_count()
        if active_count >= max_sigs:
            reason = f"max active signals ({active_count}/{max_sigs})"
            _current_funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
            trace.blocked("portfolio_risk", reason)
            await trace.save(db)
            return None
        portfolio_risk = await db.get_portfolio_risk_sum()
        if portfolio_risk >= max_risk:
            reason = f"portfolio risk {portfolio_risk:.1f}% >= {max_risk}%"
            _current_funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
            trace.blocked("portfolio_risk", reason)
            await trace.save(db)
            return None
        trace.passed("portfolio_risk")
        _current_funnel.log_gate(symbol, timeframe, "portfolio_risk", "PASS")

        # 0.3 Fetch OHLCV + Indicators
        ind_result = await _get_indicators(symbol, timeframe)
        if ind_result is None:
            _current_funnel.log_gate(symbol, timeframe, "indicators", "BLOCKED", "unavailable")
            trace.blocked("indicators", "OHLCV/indicator unavailable")
            await trace.save(db)
            return None
        ind, df = ind_result
        trace.passed("indicators")
        _current_funnel.log_gate(symbol, timeframe, "indicators", "PASS")

        # ═══ Phase 1: Pattern Engine (ICT setup detection) ═══

        _df_clean = df.dropna(subset=["open", "high", "low", "close", "volume"])
        sweeps = []
        order_blocks = []
        structure = None
        fvgs = []
        candle_quality = None

        try:
            if len(_df_clean) >= 10:
                from liquidity.sweep import detect_sweeps
                from liquidity.order_blocks import detect_order_blocks
                from market_structure.structure import analyze_structure
                from liquidity.fvg import detect_fvg
                from liquidity.candle_quality import analyze_last_candle

                sweeps = detect_sweeps(_df_clean, lookback=50)
                order_blocks = detect_order_blocks(_df_clean, lookback=100)
                candle_quality = analyze_last_candle(_df_clean, atr_value=ind.atr)
                fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 100))

                # Compute displacement_atr and reclaim for MSS classification
                _disp_atr = 0.0
                _reclaim = 0
                if candle_quality and ind.atr and ind.atr > 0:
                    _disp_atr = candle_quality.body_atr_ratio if hasattr(candle_quality, 'body_atr_ratio') else 0.0
                if sweeps:
                    _valid_sw = [s for s in sweeps if s.is_valid]
                    if _valid_sw:
                        _reclaim = _valid_sw[0].reclaim_candles

                structure = analyze_structure(
                    _df_clean, lookback=50,
                    sweeps=sweeps,
                    displacement_atr=_disp_atr,
                    reclaim_bars=_reclaim,
                    atr_value=ind.atr if ind.atr else 0.0,
                )
        except Exception as e:
            logger.warning(f"Pattern analysis failed for {symbol} {timeframe}: {e}")

        setup = pattern_engine.detect(
            sweeps=sweeps,
            order_blocks=order_blocks,
            structure=structure,
            fvgs=fvgs,
            candle_quality=candle_quality,
            current_price=ind.close,
            atr=ind.atr if ind.atr else 0.0,
        )

        if not setup.detected:
            _current_funnel.log_gate(symbol, timeframe, "pattern_engine", "BLOCKED",
                                     setup.rejection_reason or "no setup")
            trace.blocked("pattern_engine", setup.rejection_reason or "no ICT setup")
            trace.set_features({"components": setup.components_count})
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            logger.debug(f"No ICT setup: {symbol} {timeframe} — {setup.rejection_reason}")
            return None

        _current_funnel.log_gate(symbol, timeframe, "pattern_engine", "PASS",
                                 f"direction={setup.direction} components={setup.components_found}")
        trace.passed("pattern_engine")

        # ═══ Phase 1.4: Setup-Type-Specific Gates ═══
        # Reversal: sweep + displacement + MSS (all hard gates)
        # Continuation: BOS + trend alignment (all hard gates)
        # Entry armed (OB/FVG proximity) — soft, log only

        # Detect regime (used later for analytics)
        _regime_for_gates = _detect_regime(ind, df, symbol, timeframe)

        if setup.setup_type == "reversal":
            # ── Reversal Gates ──
            if not setup.has_sweep:
                reason = "reversal: no sweep"
                _current_funnel.log_gate(symbol, timeframe, "sweep_required", "BLOCKED", reason)
                trace.blocked("sweep_required", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            trace.passed("sweep_required")
            _current_funnel.log_gate(symbol, timeframe, "sweep_required", "PASS")

            # Displacement is informational — MSS already validates displacement between
            # sweep and CHoCH. Requiring the CURRENT candle to be displacement is redundant.
            trace.passed("displacement_gate")
            _current_funnel.log_gate(symbol, timeframe, "displacement_gate", "PASS")

            if not setup.has_mss:
                reason = "reversal: no MSS (strong CHoCH)"
                _current_funnel.log_gate(symbol, timeframe, "mss_gate", "BLOCKED", reason)
                trace.blocked("mss_gate", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            trace.passed("mss_gate")
            _current_funnel.log_gate(symbol, timeframe, "mss_gate", "PASS",
                                     f"mss_score={setup.mss_score:.0f}")

        elif setup.setup_type == "continuation":
            # ── Continuation Gates ──
            if not setup.has_bos:
                reason = "continuation: no BOS"
                _current_funnel.log_gate(symbol, timeframe, "bos_gate", "BLOCKED", reason)
                trace.blocked("bos_gate", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            trace.passed("bos_gate")
            _current_funnel.log_gate(symbol, timeframe, "bos_gate", "PASS",
                                     f"bos_type={setup.bos_type}")

        # ── Entry Armed (OB/FVG zone) ──
        # Default: soft (log only) — matches current behavior, entry is taken at close.
        # config.require_entry_zone=True makes it a hard gate: only emit when price is
        # actually inside the OB/FVG zone, so the bot stops chasing entries mid-move.
        if not setup.entry_armed:
            if config.require_entry_zone:
                reason = "price not in OB/FVG entry zone"
                _current_funnel.log_gate(symbol, timeframe, "entry_zone", "BLOCKED", reason)
                trace.blocked("entry_zone", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            logger.debug(
                f"Entry not armed: {symbol} {timeframe} — "
                f"price not in OB/FVG zone (signal will fire but entry may be suboptimal)"
            )
        else:
            _current_funnel.log_gate(symbol, timeframe, "entry_armed", "PASS")

        # ═══ Phase 1.44: SMT Divergence (soft feature — no blocking) ═══
        _smt_result = None
        if config.derivatives.smt_enabled:
            try:
                from derivatives.smt_divergence import fetch_smt_divergence
                _smt_result = await fetch_smt_divergence(symbol)
            except Exception as e:
                logger.debug(f"SMT divergence check failed for {symbol}: {e}")
        _smt_detail = _smt_result.detail if _smt_result else "SMT disabled or no data"
        trace.record("smt_divergence", True)
        logger.debug(f"SMT {symbol}: {_smt_detail}")

        # ═══ Phase 1.45: HTF Bias + Premium/Discount Zones ═══

        try:
            df_1d = await exchange_client.fetch_ohlcv(symbol, "1d", limit=60)
            df_4h = await exchange_client.fetch_ohlcv(symbol, "4h", limit=60)
        except Exception:
            df_1d = None
            df_4h = None

        _htf_bias_penalty = 1.0
        _htf_result = None
        _zone_type = None
        _fib_level = None
        _zone_quality_multiplier = 1.0

        if config.htf_bias_v2:
            # ── HTF Bias V2: W1 → D1 → H4 → H1 ──
            try:
                df_1w = await exchange_client.fetch_ohlcv(symbol, "1w", limit=60)
            except Exception:
                df_1w = None
            try:
                df_1h = await exchange_client.fetch_ohlcv(symbol, "1h", limit=60)
            except Exception:
                df_1h = None

            _htf_result = get_htf_bias_v2(df_1w, df_1d, df_4h, df_1h)
            htf_bias_str = _htf_result.direction

            if htf_bias_str == 'bullish':
                _bias_enum = HTFBias.BULLISH
            elif htf_bias_str == 'bearish':
                _bias_enum = HTFBias.BEARISH
            else:
                _bias_enum = HTFBias.NEUTRAL

            if _bias_enum != HTFBias.NEUTRAL:
                direction_map = {"buy": HTFBias.BULLISH, "sell": HTFBias.BEARISH}

                if setup.setup_type == "continuation":
                    setup_bias = direction_map.get(setup.direction)
                    if setup_bias != _bias_enum:
                        # Continuation opposes HTF bias. config.htf_hard_gate (default True) blocks
                        # it outright; when disabled, keep the signal but apply a P(TP) penalty and
                        # let the Probability/Risk layer decide.
                        if config.htf_hard_gate:
                            reason = f"HTF bias gate: continuation {setup.direction} vs HTF {htf_bias_str}"
                            _current_funnel.log_gate(symbol, timeframe, "htf_bias", "BLOCKED", reason)
                            trace.blocked("htf_bias", reason)
                            trace.set_version(VERSION, build_config_snapshot())
                            await trace.save(db)
                            return None
                        _htf_bias_penalty = config.htf_bias_continuation_penalty
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"continuation {setup.direction} vs HTF {htf_bias_str} — penalty {_htf_bias_penalty}",
                        )
                        trace.record("htf_bias", True)
                    else:
                        trace.passed("htf_bias")
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"continuation {setup.direction} aligned with HTF {htf_bias_str}",
                        )

                elif setup.setup_type == "reversal":
                    setup_bias = direction_map.get(setup.direction)
                    if setup_bias != _bias_enum:
                        _htf_bias_penalty = config.htf_bias_continuation_penalty
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"reversal mismatch penalty {_htf_bias_penalty}",
                        )
                        trace.record("htf_bias", True)
                    else:
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"reversal aligned with HTF {htf_bias_str}",
                        )
                        trace.passed("htf_bias")
            else:
                _current_funnel.log_gate(
                    symbol, timeframe, "htf_bias", "PASS", "HTF neutral — no bias applied",
                )
                trace.passed("htf_bias")

        else:
            # ── Fallback: HTF Bias V1 ──
            _struct_1d = extract_structure_dict(structure) if structure else None
            _struct_4h = None
            if df_4h is not None and len(df_4h) >= 60:
                try:
                    from market_structure.structure import analyze_structure as _analyze_4h
                    _htf_struct = _analyze_4h(df_4h, lookback=50, atr_value=ind.atr if ind.atr else 0.0)
                    _struct_4h = extract_structure_dict(_htf_struct)
                except Exception:
                    pass

            _bias_enum = get_htf_bias(df_1d, df_4h, _struct_1d, _struct_4h)
            htf_bias_str = _bias_enum.value

            if _bias_enum != HTFBias.NEUTRAL:
                direction_map = {"buy": HTFBias.BULLISH, "sell": HTFBias.BEARISH}

                if setup.setup_type == "continuation":
                    setup_bias = direction_map.get(setup.direction)
                    if setup_bias != _bias_enum:
                        # Continuation opposes HTF bias. config.htf_hard_gate (default True) blocks
                        # it outright; when disabled, keep the signal but apply a P(TP) penalty and
                        # let the Probability/Risk layer decide.
                        if config.htf_hard_gate:
                            reason = f"HTF bias gate: continuation {setup.direction} vs HTF {_bias_enum.value}"
                            _current_funnel.log_gate(symbol, timeframe, "htf_bias", "BLOCKED", reason)
                            trace.blocked("htf_bias", reason)
                            trace.set_version(VERSION, build_config_snapshot())
                            await trace.save(db)
                            return None
                        _htf_bias_penalty = config.htf_bias_continuation_penalty
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"continuation {setup.direction} vs HTF {_bias_enum.value} — penalty {_htf_bias_penalty}",
                        )
                        trace.record("htf_bias", True)
                    else:
                        trace.passed("htf_bias")
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"continuation {setup.direction} aligned with HTF {_bias_enum.value}",
                        )

                elif setup.setup_type == "reversal":
                    setup_bias = direction_map.get(setup.direction)
                    if setup_bias != _bias_enum:
                        _htf_bias_penalty = config.htf_bias_continuation_penalty
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"reversal mismatch penalty {_htf_bias_penalty}",
                        )
                        trace.record("htf_bias", True)
                    else:
                        _current_funnel.log_gate(
                            symbol, timeframe, "htf_bias", "PASS",
                            f"reversal aligned with HTF {_bias_enum.value}",
                        )
                        trace.passed("htf_bias")
            else:
                _current_funnel.log_gate(
                    symbol, timeframe, "htf_bias", "PASS", "HTF neutral — no bias applied",
                )
                trace.passed("htf_bias")

        # ═══ Premium/Discount Zone Detection ═══
        if config.premium_discount and df is not None and len(df) > 0:
            try:
                _swing_high = None
                _swing_low = None
                if structure and structure.recent_highs and structure.recent_lows:
                    _swing_high = max(structure.recent_highs)
                    _swing_low = min(structure.recent_lows)
                if _swing_high is None or _swing_low is None or _swing_high <= _swing_low:
                    _lookback = min(50, len(df))
                    _swing_high = float(df['high'].tail(_lookback).max())
                    _swing_low = float(df['low'].tail(_lookback).min())

                from market_structure.premium_discount import (
                    classify_zone as _classify_zone,
                    get_entry_zone_quality as _get_zone_quality,
                )
                zone_result = _classify_zone(df, htf_bias_str, _swing_high, _swing_low)
                _zone_type = zone_result.zone_type.value
                _fib_level = zone_result.fib_level
                _zone_quality_multiplier = _get_zone_quality(
                    zone_result, htf_bias_str, setup.setup_type,
                )
            except Exception as e:
                logger.debug(f"Zone classification failed for {symbol} {timeframe}: {e}")

        # ═══ Phase 1.5: Build Trade Plan (ICT-based) ═══

        from strategy.trade_engine import trade_engine

        trade_plan = trade_engine.build_trade_plan(
            ind=ind,
            direction=setup.direction,
            structure=structure,
            order_blocks=order_blocks,
            sweeps=sweeps,
            fvgs=fvgs,
            df=_df_clean,
            timeframe=timeframe,
        )

        sl = trade_plan.sl
        tp = trade_plan.tp
        sl_source = trade_plan.sl_source

        if sl is None or tp is None:
            _current_funnel.log_gate(symbol, timeframe, "sl_tp", "BLOCKED", "calculation failed")
            trace.blocked("sl_tp", "SL/TP calculation failed")
            await trace.save(db)
            return None

        entry_price = ind.close

        # ═══ Phase 1.55: Market Phase Detection (SHADOW MODE) ═══

        _phase_assessment = None
        try:
            _phase_assessment = _market_phase_engine.assess(
                adx=ind.adx if ind.adx else 0.0,
                atr_current=ind.atr if ind.atr else 0.0,
                atr_avg=getattr(ind, 'atr_avg', ind.atr) if ind.atr else 0.0,
                ema_fast=ind.ema_fast if ind.ema_fast else 0.0,
                ema_slow=ind.ema_slow if ind.ema_slow else 0.0,
                ema_trend=ind.ema_trend if hasattr(ind, 'ema_trend') and ind.ema_trend else 0.0,
                ema_fast_prev=0.0,
                ema_slow_prev=0.0,
                close=ind.close,
                high=ind.high,
                low=ind.low,
                has_bos=setup.bos_type is not None,
                bos_direction=setup.bos_type,
                has_choch=setup.has_mss,
                has_displacement=setup.has_displacement if hasattr(setup, 'has_displacement') else False,
                displacement_count=1 if setup.has_displacement else 0,
                volume_ratio=1.0,
                range_pct=0.0,
                bars_in_range=20,
            )
            logger.info(
                f"[SHADOW] Phase: {symbol} {timeframe} | "
                f"phase={_phase_assessment.phase.value} "
                f"conf={_phase_assessment.confidence:.2f} "
                f"dur={_phase_assessment.duration_bars}bars"
            )
        except Exception as e:
            logger.debug(f"[SHADOW] Phase detection failed for {symbol} {timeframe}: {e}")

        # ═══ Phase 1.6: Market Thesis Engine (SHADOW MODE) ═══
        # Dynamic approach: cache graph and thesis per symbol/timeframe.
        # Graph updates in-place on each candle close instead of rebuilding.
        # DynamicTradeThesis tracks competing BUY/SELL with stability.

        _thesis_score = 0.0
        _thesis_stability = 0.0

        try:
            from strategy.market_thesis_engine import (
                market_thesis_engine, LiquidityGraph, DynamicTradeThesis,
            )
            from liquidity.equal_levels import detect_equal_levels
            from liquidity.external import detect_external_liquidity

            _cache_key = f"{symbol}_{timeframe}"

            # Latest candle data for update_on_candle
            _last_row = _df_clean.iloc[-1] if len(_df_clean) > 0 else None
            _candle_data = {}
            if _last_row is not None:
                _candle_data = {
                    "open": float(_last_row["open"]),
                    "high": float(_last_row["high"]),
                    "low": float(_last_row["low"]),
                    "close": float(_last_row["close"]),
                    "volume": float(_last_row["volume"]),
                }

            if _cache_key in _dynamic_graphs:
                # ── UPDATE existing graph in-place ──
                _liq_graph = _dynamic_graphs[_cache_key]
                _thesis = _dynamic_theses.get(_cache_key)

                if _candle_data:
                    _liq_graph.update_on_candle(
                        _candle_data, entry_price, new_bar=True,
                    )

                if _thesis is not None:
                    _thesis.update(
                        _liq_graph, entry_price, _candle_data,
                        atr=ind.atr if ind.atr else 0,
                    )
                    _thesis_stability = _thesis.scenario_stability

                    # Use best scenario from dynamic thesis
                    _best = _thesis.best_scenario
                    if _best and _best.is_active:
                        _thesis_score = _best.score

                        logger.info(
                            f"[SHADOW] Dynamic Thesis: {symbol} {timeframe} | "
                            f"BUY={_thesis.buy_scenario.probability:.2f} "
                            f"SELL={_thesis.sell_scenario.probability:.2f} | "
                            f"ambiguous={_thesis.is_ambiguous} | "
                            f"stability={_thesis_stability:.2f} | "
                            f"graph_v{_liq_graph.graph_version}"
                        )

                        if _thesis.is_ambiguous:
                            logger.debug(
                                f"[SHADOW] Ambiguous thesis for {symbol} {timeframe} "
                                f"— probabilities too close"
                            )
                    else:
                        logger.debug(
                            f"[SHADOW] No active scenario for {symbol} {timeframe}"
                        )
                else:
                    logger.debug(
                        f"[SHADOW] No cached thesis for {symbol} {timeframe}"
                    )
            else:
                # ── FIRST TIME: build graph + create thesis ──
                _swing_highs = getattr(structure, "swing_points", []) or []
                _swing_lows = getattr(structure, "swing_points", []) or []
                _equal_levels = detect_equal_levels(_swing_highs, _swing_lows)
                _external_levels = (
                    detect_external_liquidity(_df_clean, lookback=200)
                    if _df_clean is not None else []
                )

                _liq_graph = market_thesis_engine.build_liquidity_graph(
                    current_price=entry_price,
                    sweeps=sweeps,
                    order_blocks=order_blocks,
                    fvgs=fvgs,
                    structure=structure,
                    equal_levels=_equal_levels,
                    external_levels=_external_levels,
                    candle_quality=candle_quality,
                    timeframe=timeframe,
                )

                _thesis = DynamicTradeThesis(
                    symbol=symbol, timeframe=timeframe,
                )
                if _candle_data:
                    _thesis.update(
                        _liq_graph, entry_price, _candle_data,
                        atr=ind.atr if ind.atr else 0,
                    )
                    _thesis_stability = _thesis.scenario_stability

                # Cache for next cycle
                _dynamic_graphs[_cache_key] = _liq_graph
                _dynamic_theses[_cache_key] = _thesis

                logger.debug(
                    f"[SHADOW] Built initial graph for {symbol} {timeframe}: "
                    f"{len(_liq_graph.nodes)} nodes"
                )

            # Also evaluate backward-compatible opportunity for trade plan
            _thesis_opportunity = market_thesis_engine.evaluate_trade_opportunity(
                graph=_liq_graph,
                direction=setup.direction,
                entry_price=entry_price,
                atr=ind.atr if ind.atr else 0,
                symbol=symbol,
                timeframe=timeframe,
            )

            if _thesis_opportunity:
                trade_plan.market_thesis = _thesis_opportunity.thesis
                trade_plan.liquidity_path = _thesis_opportunity.expected_path
                trade_plan.scenario_score = _thesis_opportunity.scenario_score
                trade_plan.scenario_stability = _thesis_stability
                trade_plan.thesis_source = "market_thesis"

                logger.info(
                    f"[SHADOW] Market Thesis: {symbol} {timeframe} | "
                    f"direction={_thesis_opportunity.direction} | "
                    f"score={_thesis_opportunity.scenario_score:.0f} | "
                    f"stability={_thesis_stability:.2f} | "
                    f"target={_thesis_opportunity.expected_target:.4f} | "
                    f"invalidation={_thesis_opportunity.invalidation:.4f} | "
                    f"rr=1:{_thesis_opportunity.expected_rr:.1f}"
                )

                if _thesis_opportunity.thesis.score_breakdown:
                    bd = _thesis_opportunity.thesis.score_breakdown.breakdown()
                    logger.info(
                        f"[SHADOW] Score breakdown: OB={bd['ob']:.1f} "
                        f"Sweep={bd['sweep']:.1f} BOS={bd['bos']:.1f} "
                        f"FVG={bd['fvg']:.1f} Liq={bd['liquidity']:.1f} "
                        f"HTF={bd['htf']:.1f} total={bd['total']:.1f}"
                    )

            # Evaluate all ranked scenarios for comparison
            _scenarios = market_thesis_engine.evaluate_scenarios(
                graph=_liq_graph,
                direction=setup.direction,
                entry_price=entry_price,
                atr=ind.atr if ind.atr else 0,
                symbol=symbol,
                timeframe=timeframe,
            )
            if _scenarios:
                logger.info(
                    f"[SHADOW] Scenarios ranked: "
                    + " | ".join(
                        f"#{s.alternative_rank + 1} score={s.score:.1f} "
                        f"conf={s.confidence:.0f} rr=1:{s.expected_rr:.1f}"
                        for s in _scenarios[:3]
                    )
                )
            else:
                logger.debug(f"[SHADOW] No thesis for {symbol} {timeframe}")

        except Exception as e:
            logger.debug(f"[SHADOW] Market Thesis Engine error for {symbol} {timeframe}: {e}")

        # ═══ Phase 1.7: Hypothesis Engine + Decision Engine ═══
        # NEW PIPELINE: generates all hypotheses, Decision Engine selects winner.
        # Runs in parallel with existing shadow mode for comparison.

        _hypothesis_set = None
        _decision = None

        try:
            from strategy.hypothesis import HypothesisSet
            from strategy.decision_engine import DecisionEngine, MarketState

            if _liq_graph is not None:
                # 1. Build HypothesisSet (all competing hypotheses)
                _hypothesis_set = market_thesis_engine.build_hypothesis_set(
                    graph=_liq_graph,
                    direction=None,  # both buy and sell
                    atr=ind.atr if ind.atr else 0,
                    current_bar=_dynamic_bar_counters.get(_cache_key, 0),
                )

                # 2. Build MarketState from phase assessment
                if _phase_assessment:
                    _market_state = MarketState.from_assessment(_phase_assessment)
                else:
                    from strategy.market_phase_engine import MarketPhase
                    _market_state = MarketState(
                        phase=MarketPhase.COMPRESSION,
                        phase_confidence=0.5,
                        narrative_weights={},
                    )

                # 3. Decision Engine selects winner
                _decision_engine = DecisionEngine()
                _decision = _decision_engine.decide(
                    hypothesis_set=_hypothesis_set,
                    market_state=_market_state,
                )

                if _decision.trade and _decision.hypothesis:
                    h = _decision.hypothesis
                    logger.info(
                        f"[HYPOTHESIS] {symbol} {timeframe} | "
                        f"direction={h.direction} | "
                        f"narrative={h.narrative_type} | "
                        f"quality={h.quality:.0f} | "
                        f"confidence={h.confidence:.2f} | "
                        f"decay={h.decay_factor:.2f} | "
                        f"utility={_decision.utility:.3f} | "
                        f"phase={_market_state.phase.value} | "
                        f"hypotheses={len(_hypothesis_set)}"
                    )
                    # Store hypothesis in trace for ScenarioMemory tracking
                    trace.set_hypothesis(
                        hypothesis_id=h.id,
                        narrative_type=h.narrative_type,
                        direction=h.direction,
                        quality=h.quality,
                        confidence=h.confidence,
                        decay_factor=h.decay_factor,
                        utility=_decision.utility,
                        entry_price=h.entry_price,
                        invalidation_price=h.invalidation_price,
                        target_price=h.target_price,
                        rr_ratio=h.rr_ratio,
                        phase=_market_state.phase.value,
                    )
                    # Record expected metrics for future outcome tracking
                    scenario_memory.record_expected(
                        symbol=symbol,
                        hypothesis_name=h.name,
                        narrative_type=h.narrative_type,
                        direction=h.direction,
                        expected_rr=h.expected_rr,
                        expected_p_tp=h.expected_p_tp,
                        expected_quality=h.quality,
                        expected_confidence=h.confidence,
                    )
                else:
                    logger.debug(
                        f"[HYPOTHESIS] {symbol} {timeframe} | "
                        f"NO TRADE: {_decision.rejection_reason} | "
                        f"reasons={_decision.reasons}"
                    )

                # Log top hypotheses for debugging
                _top = _hypothesis_set.top(3)
                if _top:
                    _top_str = " | ".join(
                        f"{h.direction}:{h.narrative_type} "
                        f"q={h.quality:.0f} c={h.confidence:.2f} "
                        f"d={h.decay_factor:.2f}"
                        for h in _top
                    )
                    logger.debug(f"[HYPOTHESIS] Top: {_top_str}")

        except Exception as e:
            logger.debug(f"[HYPOTHESIS] Engine error for {symbol} {timeframe}: {e}")

        # ═══ Phase 1.65: Scenario Engine (SHADOW MODE) ═══

        _new_scenarios = []
        _new_evaluations = []
        try:
            if _liq_graph is not None:
                _new_scenarios = _scenario_engine.detect_scenarios(
                    graph=_liq_graph,
                    structure=structure,
                    phase=_phase_assessment,
                    direction=setup.direction,
                )

                if _new_scenarios:
                    # Estimate probability for each scenario
                    from strategy.probability_engine import probability_engine
                    for scenario in _new_scenarios[:5]:  # top 5
                        eval_result = probability_engine.estimate_scenario(
                            features=features,
                            scenario=scenario,
                        )
                        _new_evaluations.append(eval_result)

                    logger.info(
                        f"[SHADOW] ScenarioEngine: {symbol} {timeframe} | "
                        f"detected={len(_new_scenarios)} "
                        f"evaluated={len(_new_evaluations)} | "
                        + " | ".join(
                            f"{s.name}(p={e.probability:.2f})"
                            for s, e in zip(_new_scenarios[:3], _new_evaluations[:3])
                        )
                    )

                    # Record observations in ScenarioMemory
                    for scenario in _new_scenarios:
                        scenario_memory.record_observation(symbol, scenario.name)

                    # Update TradeThesisManager
                    _current_thesis = _thesis_manager.update(
                        symbol=symbol,
                        timeframe=timeframe,
                        scenarios=_new_scenarios,
                        evaluations=_new_evaluations,
                        bar=len(_df_clean),
                        price=entry_price,
                    )
                    if _current_thesis:
                        logger.info(
                            f"[SHADOW] Thesis: {symbol} {timeframe} | "
                            f"status={_current_thesis.status} "
                            f"dir={_current_thesis.direction} "
                            f"scenario={_current_thesis.scenario.name} "
                            f"p={_current_thesis.evaluation.probability:.2f} "
                            f"age={_current_thesis.age_bars}"
                        )

        except Exception as e:
            logger.debug(f"[SHADOW] ScenarioEngine error for {symbol} {timeframe}: {e}")

        # ═══ Phase 2: Feature Builder ═══

        regime = _detect_regime(ind, df, symbol, timeframe)
        from risk.volatility_regime import classify_volatility
        vol_regime = classify_volatility(ind.atr, ind.close)

        # MTF alignment (analytics — not a gate)
        mtf_aligned = False
        mtf_count = 0
        is_reversal = setup.is_reversal if setup.detected else False
        if config.market_structure.mtf_enabled:
            try:
                mtf_result = await check_mtf_alignment(
                    symbol=symbol,
                    direction="bullish" if setup.direction == "buy" else "bearish",
                    primary_tf=timeframe,
                    exchange_client=exchange_client,
                    required_alignment=1 if is_reversal else config.market_structure.mtf_required_alignment,
                )
                if mtf_result.aligned:
                    mtf_aligned = True
                    mtf_count = len(mtf_result.states)
            except Exception as e:
                logger.debug(f"MTF check failed for {symbol}: {e}")

        # Context enrichment (soft — no blocking)
        context_score_val = 0.0
        fear_greed_val = None
        funding_rate_val = None
        context_verdict = None
        if config.context_enabled:
            try:
                snapshot = await asyncio.wait_for(
                    context_engine.get_snapshot(symbol),
                    timeout=10.0,
                )
                context_verdict = context_scorer.score(setup.direction.upper(), snapshot)
                context_score_val = context_verdict.score
                fear_greed_val = snapshot.fear_greed_value
                funding_rate_val = snapshot.funding_rate
            except Exception as e:
                logger.debug(f"Context enrichment skipped for {symbol}: {e}")

        # OB state multiplier (mitigation factor for Probability Engine)
        _ob_state_multiplier = 1.0
        if order_blocks and setup.has_ob and setup.direction:
            _rel_obs = [ob for ob in order_blocks
                        if ob.type == ('bullish' if setup.direction == 'buy' else 'bearish')]
            if _rel_obs:
                _nearest_ob = min(_rel_obs, key=lambda ob: abs(ob.midpoint - setup.ob_midpoint))
                from liquidity.ob_state import get_ob_state, get_ob_multiplier, OBState
                _ob_state = get_ob_state(_df_clean, _nearest_ob.high, _nearest_ob.low, _nearest_ob.type)
                if _ob_state == OBState.BROKEN:
                    _ob_state_multiplier = 0.0
                else:
                    _ob_state_multiplier = get_ob_multiplier(_ob_state)

        # Build features
        features = feature_builder.build(
            setup=setup,
            ind=ind,
            structure=structure,
            regime=regime,
            vol_regime=vol_regime,
            mtf_aligned=mtf_aligned,
            mtf_count=mtf_count,
            context_score=context_score_val,
            fear_greed=fear_greed_val,
            funding_rate=funding_rate_val,
            sl=sl,
            tp=tp,
            entry_price=entry_price,
            candle_quality=candle_quality,
            is_reversal=is_reversal,
            htf_bias_penalty=_htf_bias_penalty,
            ob_state_multiplier=_ob_state_multiplier,
            smt_divergence_score=_smt_to_score(_smt_result),
        )

        # ═══ Phase 3: Probability Engine ═══

        probability = probability_engine.predict(features)

        # Apply zone quality multiplier
        if config.premium_discount and _zone_quality_multiplier != 1.0:
            probability.p_tp = min(probability.p_tp * _zone_quality_multiplier, 1.0)

        logger.info(
            f"Probability: P(TP)={probability.p_tp:.1%} | "
            f"RR={probability.expected_rr:.2f} | PF={probability.profit_factor:.2f} | "
            f"model={probability.model_type} | {symbol} {timeframe}"
        )

        # ═══ Phase 3.5: Probability selector (opt-in) ═══
        # When config.min_p_tp > 0 the Probability Engine becomes an actual selector:
        # setups below the P(TP) floor are dropped here. Default 0.0 = disabled (current
        # behavior, where p_tp only feeds Kelly sizing in the Risk Engine).
        if config.min_p_tp > 0.0 and probability.p_tp < config.min_p_tp:
            reason = f"P(TP)={probability.p_tp:.1%} < min {config.min_p_tp:.1%}"
            _current_funnel.log_gate(symbol, timeframe, "probability", "BLOCKED", reason)
            trace.blocked("probability", reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            logger.info(f"Probability BLOCKED: {symbol} {timeframe} — {reason}")
            return None

        # ═══ Phase 4: Risk Engine ═══

        portfolio_state = PortfolioState(
            active_count=active_count,
            total_risk_pct=portfolio_risk,
            max_active_signals=config.max_active_signals,
            max_portfolio_risk_pct=config.max_portfolio_risk_pct,
        )

        risk_decision = risk_engine.evaluate(
            features=features,
            probability=probability,
            portfolio=portfolio_state,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            scenario_score=_thesis_score,
            scenario_stability=_thesis_stability,
            mss_quality=setup.mss_score,
            atr=ind.atr if ind.atr else 0.0,
        )

        if not risk_decision.should_trade:
            _current_funnel.log_gate(symbol, timeframe, "risk_engine", "BLOCKED",
                                     risk_decision.rejection_reason)
            trace.blocked("risk_engine", risk_decision.rejection_reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            logger.info(f"Risk BLOCKED: {symbol} {timeframe} — {risk_decision.rejection_reason}")
            return None

        _current_funnel.log_gate(symbol, timeframe, "risk_engine", "PASS",
                                 f"risk={risk_decision.risk_pct:.2f}%")
        trace.passed("risk_engine")

        # ═══ Phase 4.5: Entry Trigger Check (new pipeline) ═══
        # Check if price is in the entry zone for the winning hypothesis
        if _decision and _decision.trade and _decision.hypothesis:
            from strategy.entry_trigger import EntryTrigger
            entry_trigger = EntryTrigger()

            # Get bid/ask for spread check
            _ticker_for_trigger = await exchange_client.fetch_ticker_full(symbol)
            _bid = _ticker_for_trigger.get("bid") if _ticker_for_trigger else None
            _ask = _ticker_for_trigger.get("ask") if _ticker_for_trigger else None

            trigger_result = entry_trigger.check(
                hypothesis=_decision.hypothesis,
                current_price=entry_price,
                bid=_bid,
                ask=_ask,
            )

            if not trigger_result.triggered:
                _current_funnel.log_gate(symbol, timeframe, "entry_trigger", "BLOCKED",
                                         trigger_result.reason)
                trace.blocked("entry_trigger", trigger_result.reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                logger.info(
                    f"EntryTrigger BLOCKED: {symbol} {timeframe} — "
                    f"{trigger_result.reason}"
                )
                return None

            _current_funnel.log_gate(symbol, timeframe, "entry_trigger", "PASS")
            trace.passed("entry_trigger")

        # ═══ Phase 5: Build SignalResult ═══

        signal_type = SignalType.BUY if setup.direction == "buy" else SignalType.SELL

        reasons = features.to_reasoning()
        reasons.append(f"P(TP)={probability.p_tp:.1%}")
        reasons.append(f"Risk={risk_decision.risk_pct:.2f}%")

        result = SignalResult(
            signal=signal_type,
            symbol=symbol,
            timeframe=timeframe,
            close=ind.close,
            entry_price=entry_price,
            sl=risk_decision.sl_price,
            tp=risk_decision.tp_price,
            reasons=reasons,
            score=features.components_count,
            _has_trigger=setup.has_trigger,
            _has_leading_trigger=setup.has_trigger,
            _regime=regime.regime if regime else None,
            _structure_trend=structure.trend if structure else None,
            _structure_bos=setup.bos_type,
            _sl_source=sl_source,
            _htf_result=_htf_result,
            _zone_type=_zone_type,
            _fib_level=_fib_level,
            _zone_quality_multiplier=_zone_quality_multiplier,
        )

        # Attach probability data for display (capped at 85%)
        result._confidence_v2 = type('Obj', (object,), {
            'confidence_pct': min(85.0, probability.p_tp * 100),
            'quality': probability.quality_label,
            'total_score': probability.expected_rr,
            'factors': [],
        })()

        # ═══ Phase 6: Dedup ═══

        dedup_cooldown_minutes = get_cooldown_minutes(
            timeframe, config.signal_cooldown_minutes, config.signal_cooldown_tf_multiplier
        )
        last = await db.get_last_signal(symbol, timeframe)
        if last is not None:
            last_sent = last.sent_at or last.created_at
            if last_sent is not None:
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                same_direction = last.signal_type == result.signal.value
                within_cooldown = (
                    datetime.now(timezone.utc) - last_sent
                ) < timedelta(minutes=dedup_cooldown_minutes)
                if same_direction and within_cooldown:
                    elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds() / 60
                    _current_funnel.log_gate(symbol, timeframe, "dedup", "BLOCKED",
                                             f"same dir, {elapsed:.0f}m < {dedup_cooldown_minutes}m")
                    trace.blocked("dedup", f"same direction, {elapsed:.0f}m < {dedup_cooldown_minutes}m")
                    await trace.save(db)
                    return None
                if not same_direction and within_cooldown:
                    cross_cooldown = timedelta(minutes=dedup_cooldown_minutes // 2)
                    if (datetime.now(timezone.utc) - last_sent) < cross_cooldown:
                        _current_funnel.log_gate(symbol, timeframe, "dedup", "BLOCKED", "cross-dir cooldown")
                        trace.blocked("dedup", "cross-direction cooldown")
                        await trace.save(db)
                        return None
        trace.passed("dedup")
        _current_funnel.log_gate(symbol, timeframe, "dedup", "PASS")

        # ═══ Phase 7: Save to DB ═══

        factor_fingerprint = "|".join(sorted(features.to_vector().keys()))

        # Calculate entry candle open time from dataframe
        _entry_candle_open = None
        if df is not None and len(df) > 0:
            last_candle_ts = df.iloc[-1].get("timestamp")
            if last_candle_ts is not None:
                _entry_candle_open = datetime.fromtimestamp(
                    last_candle_ts / 1000, tz=timezone.utc
                ) if isinstance(last_candle_ts, (int, float)) else last_candle_ts

        # Fetch execution snapshot data
        _ticker = await exchange_client.fetch_ticker_full(symbol)
        _tick_size = exchange_client.get_tick_size(symbol)
        _atr = features.atr if hasattr(features, 'atr') else None
        _last_candle = df.iloc[-1] if df is not None and len(df) > 0 else None

        saved_signal = await db.save_signal(
            symbol=result.symbol,
            timeframe=result.timeframe,
            signal_type=result.signal.value,
            close_price=result.close,
            sl=result.sl,
            tp=result.tp,
            score=result.score,
            reasons=result.reasons,
            confirmed=False,
            factor_fingerprint=factor_fingerprint,
            confidence_v2_pct=probability.p_tp * 100,
            confidence_v2_factors=[],
            entry_candle_open=_entry_candle_open,
            # Execution snapshot
            entry_price_source="CLOSE",
            entry_open=float(_last_candle["open"]) if _last_candle is not None else None,
            entry_mid=float((_last_candle["high"] + _last_candle["low"]) / 2) if _last_candle is not None else None,
            entry_bid=_ticker.get("bid") if _ticker else None,
            entry_ask=_ticker.get("ask") if _ticker else None,
            entry_spread=(_ticker.get("ask") - _ticker.get("bid")) if _ticker and _ticker.get("ask") and _ticker.get("bid") else None,
            entry_atr=_atr,
            entry_tick_size=_tick_size,
            signal_detected_at=datetime.now(timezone.utc),
        )

        trace.set_signal(
            signal_type=result.signal.value,
            score=result.score,
            close_price=result.close,
            sl=result.sl,
            tp=result.tp,
        )

        # Build execution snapshot
        _signal_detected_at = saved_signal.signal_detected_at
        _telegram_sent_at = saved_signal.sent_at
        _latency_ms = None
        if _signal_detected_at and _telegram_sent_at:
            _latency_ms = (_telegram_sent_at - _signal_detected_at).total_seconds() * 1000

        _exec_snapshot = ExecutionSnapshot(
            entry_candle_open=_entry_candle_open.isoformat() if _entry_candle_open else None,
            entry_timestamp=_signal_detected_at.isoformat() if _signal_detected_at else None,
            entry_bar_index=len(df) - 1 if df is not None else None,
            spread=(_ticker.get("ask") - _ticker.get("bid")) if _ticker and _ticker.get("ask") and _ticker.get("bid") else None,
            atr=_atr,
            tick_size=_tick_size,
            buffer_total=abs(result.sl - result.close) if result.sl and result.close else None,
            execution_latency_ms=_latency_ms,
            entry_source="CLOSE",
            entry_price=result.close,
            bid=_ticker.get("bid") if _ticker else None,
            ask=_ticker.get("ask") if _ticker else None,
            open=float(_last_candle["open"]) if _last_candle is not None else None,
            close=float(_last_candle["close"]) if _last_candle is not None else None,
            mid=float((_last_candle["high"] + _last_candle["low"]) / 2) if _last_candle is not None else None,
            signal_detected_at=_signal_detected_at.isoformat() if _signal_detected_at else None,
            telegram_sent_at=_telegram_sent_at.isoformat() if _telegram_sent_at else None,
        )
        trace.set_execution_snapshot(_exec_snapshot)

        _trace_features = features.to_vector()
        _trace_features["p_tp"] = probability.p_tp
        _trace_features["expected_rr"] = probability.expected_rr
        _trace_features["risk_pct"] = risk_decision.risk_pct
        trace.set_features(_trace_features)
        trace.set_version(VERSION)
        await trace.save(db, signal_id=saved_signal.id)

        await db.create_outcome(saved_signal.id, risk_pct=risk_decision.risk_pct)

        # ═══ Phase 8: Cooldown + Notify ═══

        await _set_cooldown(symbol, timeframe)

        try:
            await notify_callback(result, context_verdict)
        except Exception as e:
            logger.error(f"Failed to send notification for {result.signal} {symbol} {timeframe}: {e}")

        signals_total.labels(
            signal_type=result.signal.value,
            symbol=symbol,
            timeframe=timeframe,
        ).inc()

        _current_funnel.passed += 1
        logger.info(
            f"Signal: {result.signal.value} {symbol} {timeframe} | "
            f"P(TP)={probability.p_tp:.1%} RR={risk_decision.rr_ratio:.2f} "
            f"risk={risk_decision.risk_pct:.2f}%"
        )
        return result


# ══════════════════════════════════════════════════════════════════════
#  Scan Cycle
# ══════════════════════════════════════════════════════════════════════

async def run_scan_cycle(notify_callback, blocked_callback=None, timeframes: Optional[list[str]] = None):
    """One scan cycle —遍历 all symbols and timeframes in parallel."""
    await check_recent_losses()
    if is_circuit_breaker_active():
        logger.warning("Scan skipped — circuit breaker active (too many recent losses)")
        return

    symbols = get_active_symbols()
    disabled = await db.get_disabled_symbols() or []
    symbols = [s for s in symbols if s not in disabled]
    tfs = timeframes if timeframes is not None else config.trading.primary_timeframes

    logger.info(f"Starting scan: {len(symbols)} symbols × {tfs}")

    _current_funnel.__init__()

    tasks = []
    for symbol in symbols:
        for tf in tfs:
            tasks.append(scan_symbol_v2(symbol, tf, notify_callback, blocked_callback))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    signals_found = 0
    for result in results:
        if isinstance(result, SignalResult) and result is not None:
            signals_found += 1
        elif isinstance(result, Exception):
            logger.error(f"Scan task failed: {result}", exc_info=result)
    logger.info(f"Scan complete. Signals found: {signals_found}/{len(tasks)}")
    _current_funnel.log_summary()
