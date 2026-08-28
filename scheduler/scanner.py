"""
scheduler/scanner.py — Основной сканер рынка.

ICT Core pipeline: Pattern Engine → Risk Engine (no ML, no scoring).
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
from strategy.signal_evaluator import (
    estimate_p_tp,
    apply_symbol_overrides,
    get_cooldown_minutes,
    dedup_block_window,
    entry_zone_touched,
)
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


# ── EMA Spread History for Regime Detection ────────────────────────────
# Stores rolling EMA spread values per symbol/timeframe across scan cycles.
# Used by _detect_regime() to compute ema_spread_trend (rising/falling/stable).
_ema_spread_history: dict[str, list[float]] = {}


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
        # Per-symbol limit: block if this symbol already has enough open positions
        max_per_sym = config.max_active_signals_per_symbol
        sym_active = await db.get_active_signals_count_by_symbol(symbol)
        if sym_active >= max_per_sym:
            reason = f"max active signals for {symbol} ({sym_active}/{max_per_sym})"
            _current_funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
            trace.blocked("portfolio_risk", reason)
            await trace.save(db)
            return None

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

        # 0.4 Compression regime gate (block choppy markets)
        if config.trading.block_compression_regime:
            _regime_check = _detect_regime(ind, df, symbol, timeframe)
            if _regime_check and _regime_check.regime == "compression":
                reason = f"compression regime (ATR percentile={_regime_check.atr_percentile:.0f})"
                _current_funnel.log_gate(symbol, timeframe, "compression_regime", "BLOCKED", reason)
                trace.blocked("compression_regime", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            # Also block range + high ADX combo (failure cluster pattern)
            if (_regime_check and _regime_check.regime == "range"
                    and float(ind.adx or 0) >= 26):
                # Price in middle of tight range = no edge
                if len(df) >= 20:
                    _recent = df.tail(20)
                    _range_pct = (_recent["high"].max() - _recent["low"].min()) / _recent["low"].min() * 100
                    _price = df["close"].iloc[-1]
                    _mid = (_recent["high"].max() + _recent["low"].min()) / 2
                    _dist_mid = abs(_price - _mid) / _mid * 100
                    if _range_pct < 1.5 and _dist_mid < 0.3:
                        reason = f"high ADX ({ind.adx:.0f}) in tight range ({_range_pct:.2f}%)"
                        _current_funnel.log_gate(symbol, timeframe, "compression_regime", "BLOCKED", reason)
                        trace.blocked("compression_regime", reason)
                        trace.set_version(VERSION, build_config_snapshot())
                        await trace.save(db)
                        return None
        _current_funnel.log_gate(symbol, timeframe, "compression_regime", "PASS")

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
            df=_df_clean,
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
        trace.set_features({
            "components": setup.components_count,
            "overall_quality": setup.overall_quality,
            "setup_confidence": setup.setup_confidence,
        })

        # ═══ Phase 1.35: Direction / Symbol filter ═══
        # Configurable via config.direction_filter (Rec 3). Historical baseline:
        # SELL blocked: WR 33.7%, PnL -0.450% across 360d backtest
        # WIF BUY blocked: WR 28.8%, PnL -1.255%
        if config.direction_filter.block_all_sell and setup.direction == "sell":
            reason = config.direction_filter.block_all_sell_reason
            _current_funnel.log_gate(symbol, timeframe, "direction_filter", "BLOCKED", reason)
            trace.blocked("direction_filter", reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            logger.info(f"Direction BLOCKED: {symbol} {timeframe} — {reason}")
            return None

        _blocked_dir = config.direction_filter.blocked_symbol_directions.get(symbol)
        if _blocked_dir is not None and setup.direction == _blocked_dir.lower():
            reason = (
                f"{symbol} {_blocked_dir.upper()} blocked: "
                f"WR {config.direction_filter.stats.get(f'{symbol}:{_blocked_dir.lower()}', {}).get('wr', '?')}% "
                f"across 360d"
            )
            _current_funnel.log_gate(symbol, timeframe, "symbol_filter", "BLOCKED", reason)
            trace.blocked("symbol_filter", reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            logger.info(f"Symbol BLOCKED: {symbol} {timeframe} — {reason}")
            return None

        # ═══ Phase 1.35: News Filter (Rec 4a, opt-in) ═══
        # Блокирует сигнал в окне high-impact макро-события. Отключён по
        # умолчанию (config.risk.news_filter_enabled); события — из локального
        # JSON (NEWS_EVENTS_FILE), см. risk/news_filter.py.
        if config.risk.news_filter_enabled:
            logger.debug(f"News filter disabled (news_filter.py removed) for {symbol}")

        # ═══ Phase 1.35: Confluence Mode (v3.0) ═══
        # Active only when STRATEGY_MODE == "confluence"
        # Based on 360d analysis: reversal WR=3.6%, BOS+OB WR=93.7%
        from config.settings import STRATEGY_MODE, StrategyMode
        if STRATEGY_MODE == StrategyMode.CONFLUENCE:
            # Reversal block: reversal setups are not trades, they're noise
            if setup.setup_type == "reversal":
                reason = "Confluence Mode: reversal blocked (WR 3.6% across 360d)"
                _current_funnel.log_gate(symbol, timeframe, "confluence_mode", "BLOCKED", reason)
                trace.blocked("confluence_mode", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                logger.info(f"Confluence BLOCKED: {symbol} {timeframe} — {reason}")
                return None

            # OB required for BOS continuation (WR 93.7% with OB vs 44.6% without)
            if setup.setup_type == "continuation" and not setup.has_ob:
                reason = "Confluence Mode: OB required for BOS (WR 93.7% with OB)"
                _current_funnel.log_gate(symbol, timeframe, "confluence_mode", "BLOCKED", reason)
                trace.blocked("confluence_mode", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                logger.info(f"Confluence BLOCKED: {symbol} {timeframe} — {reason}")
                return None

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

            # Sweep required for continuation (100% of winners had sweep)
            if not setup.has_sweep:
                reason = "continuation: no sweep"
                _current_funnel.log_gate(symbol, timeframe, "sweep_continuation", "BLOCKED", reason)
                trace.blocked("sweep_continuation", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            trace.passed("sweep_continuation")
            _current_funnel.log_gate(symbol, timeframe, "sweep_continuation", "PASS",
                                     f"sweep_type={setup.sweep_type}")

        # ── Phase 1.5 (moved before overrides): Build Trade Plan (ICT-based) ──
        # Built here so per-symbol override gates (max_sl_pct) can use real SL
        # distance instead of the non-existent setup.sl_distance_pct.
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

        entry_price = float(trade_plan.entry_price)

        # ── Phase 1.51: Live Price Alignment ──
        # Fetch live ticker and adjust entry/SL/TP to current market price.
        # Original SL/TP distances are preserved (offset-based adjustment).
        try:
            _live_ticker = await exchange_client.fetch_ticker_full(symbol)
            _live_price = None
            if _live_ticker:
                _live_price = _live_ticker.get("last") or _live_ticker.get("bid")
            if _live_price and _live_price > 0:
                _candle_close = entry_price
                _price_offset = _live_price - _candle_close
                _offset_pct = abs(_price_offset) / _candle_close * 100 if _candle_close > 0 else 0

                if _offset_pct > 0.1:
                    logger.info(
                        f"Live price alignment: candle_close={_candle_close:.6f} "
                        f"live={_live_price:.6f} offset={_price_offset:+.6f} ({_offset_pct:.2f}%)"
                    )

                entry_price = round(_live_price, 8)
                sl = round(sl + _price_offset, 8)
                tp = round(tp + _price_offset, 8)
            else:
                logger.warning(f"Live ticker unavailable for {symbol}, using candle close as entry")
        except Exception as e:
            logger.warning(f"Failed to fetch live ticker for {symbol}: {e}, using candle close as entry")

        # ── Direction sanity: BUY entry must be ≤ live price, SELL entry ≥ live price ──
        if _live_price and _live_price > 0:
            if setup.direction == "buy" and entry_price > _live_price * 1.001:
                reason = f"BUY entry {entry_price:.6f} > live price {_live_price:.6f}"
                _current_funnel.log_gate(symbol, timeframe, "direction_check", "BLOCKED", reason)
                trace.blocked("direction_check", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
            if setup.direction == "sell" and entry_price < _live_price * 0.999:
                reason = f"SELL entry {entry_price:.6f} < live price {_live_price:.6f}"
                _current_funnel.log_gate(symbol, timeframe, "direction_check", "BLOCKED", reason)
                trace.blocked("direction_check", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None

        # ── Phase 1.46: Per-Symbol Overrides (shared with backtest) ──
        _sym_blocked, _sym_reason = apply_symbol_overrides(
            symbol=symbol,
            ind=ind,
            setup=setup,
            trade_plan=trade_plan,
            atr_pct=(ind.atr / ind.close * 100) if ind.atr and ind.close > 0 else 0.0,
        )
        if _sym_blocked:
            _current_funnel.log_gate(symbol, timeframe, "symbol_overrides", "BLOCKED", _sym_reason)
            trace.blocked("symbol_overrides", _sym_reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            return None
        if _sym_overrides := config.trading.symbol_overrides.get(symbol, {}):
            _current_funnel.log_gate(symbol, timeframe, "symbol_overrides", "PASS")

        # ── Entry Zone (OB/FVG) — shared hard gate, same as backtest ──
        # Default: require_entry_zone=False → soft (log only), entry taken at close.
        # require_entry_zone=True → only emit when the current bar actually traded
        # in the FVG entry zone (rejects phantom fills at an unreached FVG median).
        if config.require_entry_zone:
            _bar_high = float(ind.high) if ind.high else float(ind.close)
            _bar_low = float(ind.low) if ind.low else float(ind.close)
            if not entry_zone_touched(setup.direction, fvgs, _bar_high, _bar_low):
                reason = "price not in FVG entry zone"
                _current_funnel.log_gate(symbol, timeframe, "entry_zone", "BLOCKED", reason)
                trace.blocked("entry_zone", reason)
                trace.set_version(VERSION, build_config_snapshot())
                await trace.save(db)
                return None
        elif not setup.entry_armed:
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

        # ═══ Phase 1.5: Trade Plan (built earlier, before overrides) ═══
        # `trade_plan`, `sl`, `tp`, `sl_source`, `entry_price` are set above
        # so per-symbol override gates can use the real SL distance.

        # ═══ Phase 2: Analytics (regime, context, MTF) ═══

        regime = _detect_regime(ind, df, symbol, timeframe)
        atr_pct = (ind.atr / ind.close * 100) if ind.atr and ind.close > 0 else 0.0

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

        # ═══ Phase 3: Inline Probability Estimation (shared with backtest) ═══

        # Simple rule-based P(TP) from Pattern Engine components + context.
        # `estimate_p_tp` also applies the HTF opposition penalty (htf_penalty).
        _components_score = setup.components_count if setup.detected else 0
        p_tp, confidence = estimate_p_tp(
            setup=setup,
            context_score=context_score_val,
            htf_result=_htf_result,
            htf_penalty=_htf_bias_penalty,
            mtf_aligned=mtf_aligned,
        )

        logger.info(
            f"Probability (inline): P(TP)={p_tp:.1%} | "
            f"components={_components_score} | context={context_score_val:.2f} | "
            f"{symbol} {timeframe}"
        )

        # ═══ Phase 4: Risk Engine ═══

        # Re-check portfolio state (may have changed during pipeline)
        _recheck_active = await db.get_active_signals_count()
        _recheck_risk = await db.get_portfolio_risk_sum()
        if _recheck_active >= config.max_active_signals:
            reason = f"max active signals ({_recheck_active}/{config.max_active_signals}) [re-check]"
            _current_funnel.log_gate(symbol, timeframe, "portfolio_risk_recheck", "BLOCKED", reason)
            trace.blocked("portfolio_risk_recheck", reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            return None
        if _recheck_risk >= config.max_portfolio_risk_pct:
            reason = f"portfolio risk {_recheck_risk:.1f}% >= {config.max_portfolio_risk_pct}% [re-check]"
            _current_funnel.log_gate(symbol, timeframe, "portfolio_risk_recheck", "BLOCKED", reason)
            trace.blocked("portfolio_risk_recheck", reason)
            trace.set_version(VERSION, build_config_snapshot())
            await trace.save(db)
            return None

        portfolio_state = PortfolioState(
            active_count=_recheck_active,
            total_risk_pct=_recheck_risk,
            max_active_signals=config.max_active_signals,
            max_portfolio_risk_pct=config.max_portfolio_risk_pct,
        )

        risk_decision = risk_engine.evaluate(
            portfolio=portfolio_state,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            atr_pct=atr_pct,
            p_tp=p_tp,
            confidence=confidence,
            mss_quality=setup.mss_score,
            atr=ind.atr if ind.atr else 0.0,
            sl_source=sl_source,
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

        # ═══ Phase 5: Build SignalResult ═══

        signal_type = SignalType.BUY if setup.direction == "buy" else SignalType.SELL

        reasons = []
        reasons.append(f"components={setup.components_count}")
        reasons.append(f"P(TP)={p_tp:.1%}")
        reasons.append(f"Risk={risk_decision.risk_pct:.2f}%")

        # Use live price for close (consistent with entry_price)
        _close_for_signal = entry_price if entry_price else ind.close

        result = SignalResult(
            signal=signal_type,
            symbol=symbol,
            timeframe=timeframe,
            close=_close_for_signal,
            entry_price=entry_price,
            sl=risk_decision.sl_price,
            tp=risk_decision.tp_price,
            reasons=reasons,
            score=setup.components_count,
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
            'confidence_pct': min(85.0, p_tp * 100),
            'quality': "moderate" if p_tp >= 0.5 else "weak",
            'total_score': 0.0,
            'factors': [],
        })()

        # ═══ Phase 6: Dedup ═══
        # Decision logic shared with the backtest engine (dedup_block_window in
        # strategy/signal_evaluator.py) — guaranteed bit-in-bit parity, no copy.

        last = await db.get_last_signal(symbol, timeframe)
        if last is not None:
            last_sent = last.sent_at or last.created_at
            if last_sent is not None:
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                window_min = dedup_block_window(
                    last_sent,
                    datetime.now(timezone.utc),
                    last.signal_type,
                    result.signal.value,
                    timeframe,
                    config.signal_cooldown_minutes,
                    config.signal_cooldown_tf_multiplier,
                )
                if window_min is not None:
                    elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds() / 60
                    _current_funnel.log_gate(symbol, timeframe, "dedup", "BLOCKED",
                                             f"elapsed {elapsed:.0f}m < {window_min}m")
                    trace.blocked("dedup", f"dedup cooldown {window_min}m")
                    await trace.save(db)
                    return None
        trace.passed("dedup")
        _current_funnel.log_gate(symbol, timeframe, "dedup", "PASS")

        # ═══ Phase 7: Save to DB ═══

        factor_fingerprint = f"components={setup.components_count}|regime={regime.regime if regime else 'none'}"

        # Calculate entry candle open time from dataframe
        _entry_candle_open = None
        if df is not None and len(df) > 0:
            last_candle_ts = df.iloc[-1].get("timestamp")
            if last_candle_ts is not None:
                _entry_candle_open = datetime.fromtimestamp(
                    last_candle_ts / 1000, tz=timezone.utc
                ) if isinstance(last_candle_ts, (int, float)) else last_candle_ts

        # Fetch execution snapshot data (reuse ticker from live price alignment if available)
        _ticker = _live_ticker if '_live_ticker' in dir() else await exchange_client.fetch_ticker_full(symbol)
        _tick_size = exchange_client.get_tick_size(symbol)
        _atr = ind.atr
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
            confidence_v2_pct=p_tp * 100,
            confidence_v2_factors=[],
            entry_candle_open=_entry_candle_open,
            # Execution snapshot
            entry_price_source="LIVE_TICKER",
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
            entry_source="LIVE_TICKER",
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

        _trace_features = {
            "components": setup.components_count,
            "atr_pct": atr_pct,
            "context_score": context_score_val,
            "mtf_aligned": mtf_aligned,
            "p_tp": p_tp,
            "expected_rr": 0.0,
            "risk_pct": risk_decision.risk_pct,
        }
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
            f"P(TP)={p_tp:.1%} RR={risk_decision.rr_ratio:.2f} "
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
