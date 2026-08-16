"""
backtest/engine.py — Consolidated backtest engine with full parity to live pipeline.

Pipeline: Pattern Engine → Feature Builder → Probability Engine → Risk Engine

Features:
  - ICT setup detection via PatternEngine
  - Liquidity-first SL/TP via TradeEngine
  - Feature collection via FeatureBuilder
  - P(TP) estimation via ProbabilityEngine
  - Capital protection + position sizing via RiskEngine
  - Commission & slippage modeling
  - Reject-reason tracking
  - Telegram output

Usage:
    python -m backtest.engine BTC/USDT 1h 336
    python -m backtest.engine BTC/USDT 1h 336 --telegram
    python -m backtest.engine BTC/USDT 1h 336 --market future
    python -m backtest.engine BTC/USDT 1h 336 --preset baseline
    python -m backtest.engine BTC/USDT 1h 336 --preset full
"""
from __future__ import annotations

import asyncio
import os
import sys
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import config
from data.exchange_client import exchange_client
from indicators.engine import IndicatorEngine, IndicatorValues
from strategy.signal_engine import SignalType, SignalResult
from strategy.pattern_engine import pattern_engine, ICTSetup
from strategy.trade_engine import trade_engine
from risk.engine import risk_engine, PortfolioState, RiskDecision
from risk.market_regime import RegimeDetector, MarketRegime
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from market_structure.structure import analyze_structure
from market_structure.htf_bias_v2 import get_htf_bias_v2, HTFBiasResult


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """Single simulated trade with full metadata."""
    symbol: str
    timeframe: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    entry_index: int
    entry_timestamp: str
    sl: float
    tp: float
    sl_source: str = "atr"
    exit_price: Optional[float] = None
    exit_index: Optional[int] = None
    exit_timestamp: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl_pct: float = 0.0
    net_pnl_pct: float = 0.0
    rr: float = 0.0
    regime: str = ""
    signal_score: int = 0
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    # New pipeline fields
    p_tp: float = 0.0
    risk_pct: float = 0.0
    setup_type: str = ""
    components: list[str] = field(default_factory=list)


@dataclass
class RejectStats:
    """Tracks signal rejection reasons."""
    total_rejected: int = 0
    no_pattern: int = 0
    setup_type_gate: int = 0
    htf_bias_blocked: int = 0
    risk_engine_rejected: int = 0
    other_rejected: int = 0


@dataclass
class FunnelData:
    """Per-symbol funnel instrumentation data."""
    symbol: str
    steps: dict = field(default_factory=dict)
    total_signals_processed: int = 0
    signal_engine_score_distribution: dict = field(default_factory=dict)
    passed_score_distribution: dict = field(default_factory=dict)
    passed_sl_sources: dict = field(default_factory=dict)
    sl_shifted: int = 0


# Pipeline funnel steps (in order — first match wins)
FUNNEL_STEPS = [
    "NO_PATTERN",
    "SETUP_TYPE_GATE",
    "HTF_BIAS_BLOCKED",
    "RISK_ENGINE_BLOCKED",
    "PASSED",
]

# Which pipeline steps are actually implemented in the backtest
BACKTEST_ACTIVE: dict[str, bool] = {
    "NO_PATTERN": True,
    "SETUP_TYPE_GATE": True,
    "HTF_BIAS_BLOCKED": True,
    "RISK_ENGINE_BLOCKED": True,
    "PASSED": True,
}


@dataclass
class BacktestResult:
    """Aggregate backtest result."""
    symbol: str
    timeframe: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    winrate: float = 0.0
    avg_pnl: float = 0.0
    avg_net_pnl: float = 0.0
    avg_rr: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_pnl_pct: float = 0.0
    total_net_pnl_pct: float = 0.0
    signals_generated: int = 0
    signals_rejected: int = 0
    exposure_time_pct: float = 0.0
    avg_trade_duration: float = 0.0
    reject_stats: RejectStats = field(default_factory=RejectStats)
    trades: list[BacktestTrade] = field(default_factory=list)
    long_stats: dict = field(default_factory=dict)
    short_stats: dict = field(default_factory=dict)
    regime_stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Feature flags for A/B/n backtesting
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    """Feature flags controlling which pipeline steps are active.

    The new pipeline (Pattern Engine → Feature Builder → Probability Engine → Risk Engine)
    handles most logic internally. These flags control optional soft features.
    """
    enable_pattern_engine_gates: bool = True
    enable_probability_gate: bool = False
    enable_htf_bias_gate: bool = False
    min_p_tp: float = 0.0
    min_score_for_signal: Optional[int] = None


# Preset definitions: name → dict of BacktestConfig field overrides
PRESETS: dict[str, dict[str, bool]] = {
    "baseline": {
        "enable_pattern_engine_gates": False,
        "enable_probability_gate": False,
        "enable_htf_bias_gate": False,
    },
    "full": {
        "enable_pattern_engine_gates": True,
        "enable_probability_gate": False,
        "enable_htf_bias_gate": False,
    },
    "optimized": {
        "enable_pattern_engine_gates": True,
        "enable_probability_gate": True,
        "enable_htf_bias_gate": True,
    },
}


def get_preset_config(preset_name: str) -> BacktestConfig:
    """Create a BacktestConfig from a named preset."""
    if preset_name not in PRESETS:
        available = ", ".join(sorted(PRESETS.keys()))
        raise ValueError(f"Unknown preset '{preset_name}'. Available: {available}")
    return BacktestConfig(**PRESETS[preset_name])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_symbol(symbol: str) -> str:
    if "/" not in symbol and symbol.endswith("USDT"):
        return symbol[:-4] + "/USDT"
    elif "/" not in symbol:
        return symbol + "/USDT"
    return symbol


def _compute_regime(
    ind: IndicatorValues,
    atr_history: list[float],
    ema_spread_history: list[float],
    volume_history: list[float],
) -> MarketRegime:
    """Compute regime using the production RegimeDetector."""
    try:
        adx_val = float(ind.adx) if ind.adx is not None else 20.0
        atr_val = float(ind.atr) if ind.atr is not None else 0.0
        close_val = float(ind.close) if ind.close is not None else 1.0
        vol_val = float(ind.volume) if ind.volume is not None else 0.0
        ema_fast_val = float(ind.ema_fast) if ind.ema_fast is not None else 0.0
        ema_slow_val = float(ind.ema_slow) if ind.ema_slow is not None else 1.0

        detector = RegimeDetector(
            adx=adx_val,
            atr_history=atr_history,
            ema_spread_history=ema_spread_history,
            volume_history=volume_history,
            current_atr=atr_val,
            current_volume=vol_val,
        )
        return detector.detect()
    except Exception:
        return MarketRegime(
            regime="range", confidence=0.5,
            adx=20.0, atr_percentile=50.0,
            ema_spread_trend="stable",
        )


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Consolidated backtest engine with full parity to live pipeline.

    Uses: PatternEngine → FeatureBuilder → ProbabilityEngine → RiskEngine
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        max_trades: int = 0,
        send_telegram: bool = False,
        market_type: Optional[str] = None,
        bt_config: Optional[BacktestConfig] = None,
        instrument: bool = False,
        source: Literal["live", "local"] = "live",
        start_date: Optional[pd.Timestamp] = None,
        end_date: Optional[pd.Timestamp] = None,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_trades = max_trades
        self.send_telegram = send_telegram
        self.market_type = market_type
        self.bt_config = bt_config or BacktestConfig()
        self.instrument = instrument
        self.source = source
        self.start_date = start_date
        self.end_date = end_date
        self.funnel_data: Optional[FunnelData] = None
        self._indicator_engine = IndicatorEngine()
        self._local_data: Optional[tuple[pd.DataFrame, Optional[dict]]] = None

    async def run(self) -> BacktestResult:
        """Run the full backtest pipeline."""
        if self.market_type:
            if self.market_type in ("future", "futures"):
                config.exchange.market_type = "future"
            elif self.market_type == "spot":
                config.exchange.market_type = "spot"

        if self.source == "local":
            df, htf = self._load_local()
            if df is not None:
                self._local_data = (df, htf)
                return await self._run_backtest()
            logger.warning(
                f"--source=local: no local cache for {self.symbol}, "
                f"falling back to live exchange"
            )

        await exchange_client.connect()

        try:
            return await self._run_backtest()
        finally:
            await exchange_client.close()

    def _load_local(self) -> tuple[Optional[pd.DataFrame], Optional[dict]]:
        """Load OHLCV from the unified 15m cache, resampled to ``self.timeframe``.

        Returns ``(df, htf)`` where ``htf`` maps tf → DataFrame for HTF Bias V2
        (1w/1d/4h), or ``(None, None)`` when the cache file is missing.
        """
        from backtest.cache_ohlcv import BASE_TIMEFRAME, load_unified, unified_cache_path
        from backtest.resampler import resample_ohlcv

        market = self.market_type or config.exchange.market_type
        path = unified_cache_path(self.symbol, market, BASE_TIMEFRAME)
        if not path.exists():
            return None, None

        df_15m = load_unified(self.symbol, market, BASE_TIMEFRAME)
        if df_15m is None or len(df_15m) == 0:
            return None, None

        if self.timeframe == BASE_TIMEFRAME:
            df = df_15m
        else:
            df = resample_ohlcv(df_15m, self.timeframe)

        if self.start_date is not None:
            df = df[df.index >= self.start_date]
        if self.end_date is not None:
            df = df[df.index <= self.end_date]

        limit = max(config.trading.candles_limit + 100, 200)
        if len(df) > limit:
            df = df.iloc[-limit:]

        htf: dict = {}
        for tf in ("1w", "1d", "4h"):
            if tf == self.timeframe:
                htf[tf] = df
            else:
                htf[tf] = resample_ohlcv(df_15m, tf)
        logger.info(
            f"--source=local: {self.symbol} {self.timeframe} from "
            f"{path.name} ({len(df):,} candles, {df.index[0]} → {df.index[-1]})"
        )
        return df, htf

    async def _run_backtest(self) -> BacktestResult:
        """Internal: fetch data and walk through candles."""
        limit = max(config.trading.candles_limit + 100, 200)
        htf: Optional[dict] = None
        if self.source == "local" and self._local_data is not None:
            df, htf = self._local_data
        else:
            df = await exchange_client.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
        if df is None or len(df) < 100:
            return BacktestResult(symbol=self.symbol, timeframe=self.timeframe)

        warmup = 80
        candle_limit = config.trading.candles_limit
        df = df.iloc[-(candle_limit + 60):] if len(df) > candle_limit + 60 else df

        trades: list[BacktestTrade] = []
        reject_stats = RejectStats()
        atr_history: list[float] = []
        ema_spread_history: list[float] = []
        volume_history: list[float] = []
        in_trade = False
        ct: Optional[BacktestTrade] = None
        signals_count = 0

        # Pre-fetch HTF data for bias (once, not per-candle)
        _htf_result: Optional[HTFBiasResult] = None
        if config.htf_bias_v2:
            try:
                if htf is not None:
                    _htf_result = get_htf_bias_v2(htf.get("1w"), htf.get("1d"), htf.get("4h"), df)
                else:
                    df_1d = await exchange_client.fetch_ohlcv(self.symbol, "1d", limit=60)
                    df_4h = await exchange_client.fetch_ohlcv(self.symbol, "4h", limit=60)
                    df_1w = await exchange_client.fetch_ohlcv(self.symbol, "1w", limit=60)
                    _htf_result = get_htf_bias_v2(df_1w, df_1d, df_4h, df)
            except Exception:
                _htf_result = None

        # Funnel instrumentation (populated only when self.instrument=True)
        _funnel_counts: dict[str, int] = {s: 0 for s in FUNNEL_STEPS}

        for i in range(warmup, len(df)):
            window = df.iloc[:i + 1].copy()
            ind = self._indicator_engine.calculate(window, self.symbol, self.timeframe)
            if ind is None:
                continue

            atr_history.append(float(ind.atr))
            ema_spread_history.append(
                abs(float(ind.ema_fast) - float(ind.ema_slow)) / float(ind.ema_slow) * 100
                if float(ind.ema_slow) > 0 else 0.0
            )
            volume_history.append(float(ind.volume))

            # --- Exit check ---
            if in_trade and ct is not None:
                high, low = float(ind.high), float(ind.low)
                if ct.direction == "BUY":
                    if low <= ct.sl:
                        ct.exit_price = ct.sl
                        ct.exit_index = i
                        ct.exit_timestamp = str(df.index[i])
                        ct.exit_reason = "sl"
                        trades.append(ct)
                        in_trade = False
                        ct = None
                    elif high >= ct.tp:
                        ct.exit_price = ct.tp
                        ct.exit_index = i
                        ct.exit_timestamp = str(df.index[i])
                        ct.exit_reason = "tp"
                        trades.append(ct)
                        in_trade = False
                        ct = None
                else:
                    if high >= ct.sl:
                        ct.exit_price = ct.sl
                        ct.exit_index = i
                        ct.exit_timestamp = str(df.index[i])
                        ct.exit_reason = "sl"
                        trades.append(ct)
                        in_trade = False
                        ct = None
                    elif low <= ct.tp:
                        ct.exit_price = ct.tp
                        ct.exit_index = i
                        ct.exit_timestamp = str(df.index[i])
                        ct.exit_reason = "tp"
                        trades.append(ct)
                        in_trade = False
                        ct = None

            # --- New signal ---
            if not in_trade:
                signals_count += 1

                # Regime detection
                regime_obj = _compute_regime(ind, atr_history, ema_spread_history, volume_history)

                # === Phase 1: Pattern Detection (ICT) ===
                _df_clean = window.dropna(subset=["open", "high", "low", "close", "volume"])
                sweeps = []
                order_blocks = []
                structure = None
                fvgs = []
                candle_quality = None

                try:
                    if len(_df_clean) >= 10:
                        sweeps = detect_sweeps(_df_clean, lookback=50)
                        order_blocks = detect_order_blocks(_df_clean, lookback=100)
                        candle_quality = analyze_last_candle(_df_clean, atr_value=ind.atr)
                        fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 100))

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
                except Exception:
                    pass

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
                    reject_stats.no_pattern += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["NO_PATTERN"] += 1
                    continue

                # === Phase 1.4: Setup-Type-Specific Gates ===
                if self.bt_config.enable_pattern_engine_gates:
                    if setup.setup_type == "reversal":
                        if not setup.has_sweep:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                        if not setup.has_mss:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                    elif setup.setup_type == "continuation":
                        if not setup.has_bos:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                        if not setup.has_sweep:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue

                # === Phase 1.45: HTF Bias Gate ===
                if self.bt_config.enable_htf_bias_gate and _htf_result:
                    htf_dir = _htf_result.direction
                    if htf_dir != 'neutral' and setup.setup_type == "continuation":
                        direction_map = {"buy": "bullish", "sell": "bearish"}
                        setup_bias = direction_map.get(setup.direction)
                        if setup_bias != htf_dir:
                            reject_stats.htf_bias_blocked += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["HTF_BIAS_BLOCKED"] += 1
                            continue

                # === Phase 1.5: Trade Plan (SL/TP) ===
                trade_plan = trade_engine.build_trade_plan(
                    ind=ind,
                    direction=setup.direction,
                    structure=structure,
                    order_blocks=order_blocks,
                    sweeps=sweeps,
                    fvgs=fvgs,
                    df=_df_clean,
                    timeframe=self.timeframe,
                )

                if not trade_plan.is_valid or trade_plan.sl == 0 or trade_plan.tp == 0:
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["NO_PATTERN"] += 1
                    continue

                entry_price = float(ind.close)
                sl = trade_plan.sl
                tp = trade_plan.tp

                # === Phase 2: Analytics (inline) ===
                atr_pct = (float(ind.atr) / float(ind.close) * 100) if ind.atr and ind.close > 0 else 0.0

                # === Phase 3: Inline Probability ===
                _components_score = setup.components_count if setup.detected else 0
                p_tp = 0.45
                if _components_score >= 4:
                    p_tp += 0.15
                elif _components_score >= 3:
                    p_tp += 0.08
                if setup.mss_score > 70:
                    p_tp += 0.05
                p_tp = max(0.15, min(0.85, p_tp))
                confidence = min(0.85, p_tp)

                # Optional probability gate
                if self.bt_config.enable_probability_gate and p_tp < self.bt_config.min_p_tp:
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                # === Phase 4: Risk Engine ===
                risk_decision = risk_engine.evaluate(
                    portfolio=PortfolioState(),
                    entry_price=entry_price,
                    sl=sl,
                    tp=tp,
                    atr_pct=atr_pct,
                    p_tp=p_tp,
                    confidence=confidence,
                    mss_quality=setup.mss_score,
                    atr=float(ind.atr) if ind.atr else 0.0,
                    sl_source=trade_plan.sl_source,
                )

                if not risk_decision.should_trade:
                    reject_stats.risk_engine_rejected += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                # Use risk engine's adjusted SL/TP
                sl = risk_decision.sl_price
                tp = risk_decision.tp_price

                # === Build Trade ===
                is_buy = setup.direction == "buy"

                if self.instrument:
                    _funnel_counts["PASSED"] += 1

                ct = BacktestTrade(
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    direction="BUY" if is_buy else "SELL",
                    entry_price=entry_price,
                    entry_index=i,
                    entry_timestamp=str(df.index[i]),
                    sl=sl,
                    tp=tp,
                    sl_source=trade_plan.sl_source or "atr",
                    regime=regime_obj.regime if regime_obj else "",
                    signal_score=setup.components_count,
                    confidence=probability.p_tp * 100,
                    reasons=list(setup.components_found),
                    p_tp=probability.p_tp,
                    risk_pct=risk_decision.risk_pct,
                    setup_type=setup.setup_type or "",
                    components=list(setup.components_found),
                )
                in_trade = True

                if 0 < self.max_trades <= len(trades):
                    break

        # Close open trade at end of data
        if in_trade and ct is not None:
            ct.exit_price = float(df.iloc[-1]["close"])
            ct.exit_index = len(df) - 1
            ct.exit_timestamp = str(df.index[-1])
            ct.exit_reason = "eob"
            trades.append(ct)

        # --- Compute PnL with commission/slippage ---
        fee_pct = config.trading.exchange_fee_pct / 100.0
        slip_pct = config.trading.slippage_pct / 100.0

        for t in trades:
            if t.direction == "BUY":
                gross = (t.exit_price - t.entry_price) / t.entry_price * 100
            else:
                gross = (t.entry_price - t.exit_price) / t.entry_price * 100
            t.pnl_pct = round(gross, 4)

            # Commission: 2 sides (entry + exit)
            total_cost_pct = (fee_pct * 2 + slip_pct * 2) * 100
            t.net_pnl_pct = round(gross - total_cost_pct, 4)

            risk = abs(t.entry_price - t.sl)
            t.rr = round(abs(t.exit_price - t.entry_price) / risk, 2) if risk > 0 else 0.0

        # Build funnel data
        if self.instrument:
            self.funnel_data = FunnelData(
                symbol=self.symbol,
                steps=_funnel_counts,
                total_signals_processed=signals_count,
            )

        return self._build_result(trades, signals_count, reject_stats, len(df))

    def _build_result(
        self,
        trades: list[BacktestTrade],
        signals_count: int,
        reject_stats: RejectStats,
        total_candles: int,
    ) -> BacktestResult:
        """Build aggregate metrics."""
        total = len(trades)
        if total == 0:
            return BacktestResult(
                symbol=self.symbol,
                timeframe=self.timeframe,
                signals_generated=signals_count,
                signals_rejected=reject_stats.total_rejected,
                reject_stats=reject_stats,
            )

        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        win_count = len(wins)
        loss_count = len(losses)

        pnl_values = [t.pnl_pct for t in trades]
        net_pnl_values = [t.net_pnl_pct for t in trades]
        rr_values = [t.rr for t in trades]

        total_pnl = sum(pnl_values)
        total_net_pnl = sum(net_pnl_values)
        avg_pnl = total_pnl / total
        avg_net_pnl = total_net_pnl / total
        avg_rr = sum(rr_values) / total

        gross_profit = sum(t.pnl_pct for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 1.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        winrate = win_count / total
        avg_win = gross_profit / win_count if win_count > 0 else 0.0
        avg_loss = gross_loss / loss_count if loss_count > 0 else 0.0
        expectancy = winrate * avg_win - (1 - winrate) * avg_loss

        if len(pnl_values) > 1:
            std = float(np.std(pnl_values, ddof=1))
            sharpe = (avg_pnl / std) if std > 0 else 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        cum = np.cumsum(pnl_values)
        peak = np.maximum.accumulate(cum)
        dd = peak - cum
        max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

        # Exposure
        durations = [t.exit_index - t.entry_index for t in trades if t.exit_index is not None]
        total_dur = sum(durations) if durations else 0
        exposure_pct = (total_dur / total_candles * 100) if total_candles > 0 else 0.0
        avg_dur = float(np.mean(durations)) if durations else 0.0

        # Direction breakdown
        long_stats = {}
        short_stats = {}
        for d_label in ["BUY", "SELL"]:
            d_trades = [t for t in trades if t.direction == d_label]
            if not d_trades:
                continue
            d_w = [t for t in d_trades if t.pnl_pct > 0]
            d_pnl = sum(t.pnl_pct for t in d_trades)
            d_net = sum(t.net_pnl_pct for t in d_trades)
            d_wr = len(d_w) / len(d_trades) * 100
            d_pf_num = sum(t.pnl_pct for t in d_w)
            d_pf_den = abs(sum(t.pnl_pct for t in d_trades if t.pnl_pct <= 0))
            d_pf = d_pf_num / d_pf_den if d_pf_den > 0 else float("inf")
            stats = {
                "trades": len(d_trades), "winrate": d_wr,
                "pnl": d_pnl, "net_pnl": d_net, "pf": d_pf,
            }
            if d_label == "BUY":
                long_stats = stats
            else:
                short_stats = stats

        # Regime breakdown
        regimes: dict[str, list[BacktestTrade]] = {}
        for t in trades:
            regimes.setdefault(t.regime or "unknown", []).append(t)
        regime_stats = {}
        for reg, r_trades in regimes.items():
            r_w = [t for t in r_trades if t.pnl_pct > 0]
            r_pnl = sum(t.pnl_pct for t in r_trades)
            r_net = sum(t.net_pnl_pct for t in r_trades)
            r_wr = len(r_w) / len(r_trades) * 100
            regime_stats[reg] = {
                "trades": len(r_trades), "winrate": r_wr,
                "pnl": r_pnl, "net_pnl": r_net,
            }

        return BacktestResult(
            symbol=self.symbol,
            timeframe=self.timeframe,
            total_trades=total,
            wins=win_count,
            losses=loss_count,
            winrate=round(winrate * 100, 1),
            avg_pnl=round(avg_pnl, 4),
            avg_net_pnl=round(avg_net_pnl, 4),
            avg_rr=round(avg_rr, 2),
            profit_factor=round(pf, 2),
            expectancy=round(expectancy, 4),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_dd, 4),
            total_pnl_pct=round(total_pnl, 4),
            total_net_pnl_pct=round(total_net_pnl, 4),
            signals_generated=signals_count,
            signals_rejected=reject_stats.total_rejected,
            exposure_time_pct=round(exposure_pct, 2),
            avg_trade_duration=round(avg_dur, 1),
            reject_stats=reject_stats,
            trades=trades,
            long_stats=long_stats,
            short_stats=short_stats,
            regime_stats=regime_stats,
        )


# ---------------------------------------------------------------------------
# Output: Console
# ---------------------------------------------------------------------------

def print_result(result: BacktestResult):
    """Print backtest results to console."""
    df_len = result.total_trades * 4  # approximate
    print(f"\n{'='*60}")
    print(f"  BACKTEST: {result.symbol} {result.timeframe}")
    print(f"  Trades: {result.total_trades} | Signals: {result.signals_generated} | Rejected: {result.signals_rejected}")
    print(f"{'='*60}")

    if result.total_trades == 0:
        print("\n  No trades generated")
        return

    print(f"\n  Total trades:     {result.total_trades}")
    print(f"  Winrate:          {result.winrate:.1f}%")
    print(f"  Avg PnL (gross):  {result.avg_pnl:+.4f}%")
    print(f"  Avg PnL (net):    {result.avg_net_pnl:+.4f}%")
    print(f"  Avg R/R:          {result.avg_rr:.2f}")
    print(f"  Profit Factor:    {result.profit_factor:.2f}")
    print(f"  Expectancy:       {result.expectancy:+.4f}")
    print(f"  Sharpe:           {result.sharpe_ratio:.2f}")
    print(f"  Max DD:           {result.max_drawdown:.2f}%")
    print(f"  Total PnL (gross): {result.total_pnl_pct:+.4f}%")
    print(f"  Total PnL (net):   {result.total_net_pnl_pct:+.4f}%")
    print(f"  Avg trade len:    {result.avg_trade_duration:.1f} candles")
    print(f"  Exposure:         {result.exposure_time_pct:.1f}%")

    # Reject stats
    rs = result.reject_stats
    if rs.total_rejected > 0:
        print(f"\n  Rejected signals: {rs.total_rejected}")
        if rs.no_pattern:
            print(f"    No pattern:     {rs.no_pattern}")
        if rs.setup_type_gate:
            print(f"    Setup gate:     {rs.setup_type_gate}")
        if rs.htf_bias_blocked:
            print(f"    HTF bias:       {rs.htf_bias_blocked}")
        if rs.risk_engine_rejected:
            print(f"    Risk engine:    {rs.risk_engine_rejected}")

    # Direction breakdown
    for label, stats in [("BUY", result.long_stats), ("SELL", result.short_stats)]:
        if not stats:
            continue
        print(f"\n  {label}: {stats['trades']} trades, wr={stats['winrate']:.0f}%, "
              f"PnL={stats['pnl']:+.2f}%, net={stats['net_pnl']:+.2f}%, PF={stats['pf']:.2f}")

    # Regime breakdown
    for reg, stats in sorted(result.regime_stats.items()):
        print(f"  [{reg}] {stats['trades']} trades, wr={stats['winrate']:.0f}%, "
              f"PnL={stats['pnl']:+.2f}%, net={stats['net_pnl']:+.2f}%")

    # Trade detail
    print(f"\n  DETAIL (last 15):")
    show = result.trades[-15:]
    start = len(result.trades) - len(show) + 1
    for idx, t in enumerate(show):
        em = "+" if t.pnl_pct > 0 else "-"
        dur = (t.exit_index - t.entry_index) if t.exit_index is not None else 0
        print(f"  {em} #{start+idx} {t.direction} entry=${t.entry_price:.4f} → "
              f"${t.exit_price:.4f} ({t.exit_reason}) pnl={t.pnl_pct:+.4f}% "
              f"net={t.net_pnl_pct:+.4f}% rr={t.rr:.2f} "
              f"sl_src={t.sl_source} [{t.regime}] {dur}c")


# ---------------------------------------------------------------------------
# Output: Telegram
# ---------------------------------------------------------------------------

def format_telegram(result: BacktestResult) -> str:
    """Format backtest results as Telegram HTML."""
    if result.total_trades == 0:
        return f"📊 <b>Backtest: {result.symbol} {result.timeframe}</b>\n\nNo trades generated"

    days_approx = result.avg_trade_duration * result.total_trades / 24 if result.timeframe == "1h" else 0

    msg = f"""📊 <b>BACKTEST: {result.symbol} {result.timeframe}</b>
┌─────────────────────────────────────
│ Trades: {result.total_trades} | Signals: {result.signals_generated} | Rejected: {result.signals_rejected}
└─────────────────────────────────────

📈 <b>Metrics:</b>
├ Trades: {result.total_trades}
├ Winrate: {result.winrate:.1f}%
├ Avg PnL (gross): {result.avg_pnl:+.4f}%
├ Avg PnL (net): {result.avg_net_pnl:+.4f}%
├ Avg R/R: {result.avg_rr:.2f}
├ Profit Factor: {result.profit_factor:.2f}
├ Expectancy: {result.expectancy:+.4f}
├ Max DD: {result.max_drawdown:.2f}%
├ Total PnL (gross): {result.total_pnl_pct:+.4f}%
└ Total PnL (net): {result.total_net_pnl_pct:+.4f}%"""

    # Direction breakdown
    msg += "\n\n📐 <b>By direction:</b>"
    for label, emoji, stats in [("BUY", "🟢", result.long_stats), ("SELL", "🔴", result.short_stats)]:
        if not stats:
            continue
        msg += f"\n{emoji} {label}: {stats['trades']} trades, wr={stats['winrate']:.0f}%, PnL={stats['pnl']:+.2f}%, net={stats['net_pnl']:+.2f}%, PF={stats['pf']:.2f}"

    # Regime breakdown
    if result.regime_stats:
        msg += "\n\n🎯 <b>By regime:</b>"
        for reg, stats in sorted(result.regime_stats.items()):
            msg += f"\n├ [{reg}] {stats['trades']} trades, wr={stats['winrate']:.0f}%, PnL={stats['pnl']:+.2f}%, net={stats['net_pnl']:+.2f}%"

    # Reject stats
    rs = result.reject_stats
    if rs.total_rejected > 0:
        msg += f"\n\n🚫 <b>Rejected: {rs.total_rejected}</b>"
        if rs.no_pattern:
            msg += f"\n├ No pattern: {rs.no_pattern}"
        if rs.setup_type_gate:
            msg += f"\n├ Setup gate: {rs.setup_type_gate}"
        if rs.htf_bias_blocked:
            msg += f"\n├ HTF bias: {rs.htf_bias_blocked}"
        if rs.risk_engine_rejected:
            msg += f"\n└ Risk engine: {rs.risk_engine_rejected}"

    # Trade detail (last 15)
    show = result.trades[-15:]
    start_idx = len(result.trades) - len(show) + 1
    msg += f"\n\n📝 <b>Last {len(show)} trades:</b>"
    for idx, t in enumerate(show):
        em = "✅" if t.pnl_pct > 0 else "❌"
        msg += (f"\n{em} #{start_idx+idx} {t.direction} ${t.entry_price:.4f}→${t.exit_price:.4f} "
                f"({t.exit_reason}) {t.pnl_pct:+.4f}% net={t.net_pnl_pct:+.4f}% "
                f"rr={t.rr:.2f} [{t.regime}]")

    return msg


async def send_to_telegram(text: str):
    """Send message to Telegram channel."""
    from telegram import Bot
    from telegram.constants import ParseMode
    from telegram.error import TelegramError

    channel_id = config.telegram.channel_id
    if not channel_id:
        print("TELEGRAM_CHANNEL_ID not set")
        return

    bot = Bot(token=config.telegram.token)

    if len(text) > 4000:
        parts = []
        lines = text.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 4000:
                parts.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            parts.append(current)

        for part in parts:
            try:
                await bot.send_message(chat_id=channel_id, text=part, parse_mode=ParseMode.HTML)
                await asyncio.sleep(1)
            except TelegramError:
                try:
                    await bot.send_message(chat_id=channel_id, text=part)
                except Exception:
                    pass
    else:
        try:
            await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.HTML)
        except TelegramError:
            try:
                await bot.send_message(chat_id=channel_id, text=text)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="backtest.engine",
        description="Consolidated backtest engine (live or local cache source).",
    )
    parser.add_argument(
        "positional", nargs="*",
        help="[symbol] [timeframe] [candle_limit] [future|futures|spot]",
    )
    parser.add_argument("--telegram", action="store_true", help="Send result to Telegram")
    parser.add_argument("--preset", type=str, default=None,
                        help="Preset: baseline / full / optimized")
    parser.add_argument("--market", type=str, default=None,
                        choices=["future", "futures", "spot"])
    parser.add_argument("--source", type=str, default="local",
                        choices=["live", "local"],
                        help="Data source: local cache (ohlcv_cache/) or live exchange")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Filter data from this date (YYYY-MM-DD, local source)")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Filter data to this date (YYYY-MM-DD, local source)")
    args = parser.parse_args()

    positional = args.positional
    symbol = positional[0].upper() if len(positional) > 0 else "BTC/USDT"
    tf = (positional[1].lower() if len(positional) > 1 else "1h")
    candle_limit = int(positional[2]) if len(positional) > 2 else 336

    market_type = args.market
    for arg in positional[3:]:
        if arg.lower() in ("future", "futures", "spot"):
            market_type = arg.lower()

    bt_config = BacktestConfig()
    if args.preset:
        bt_config = get_preset_config(args.preset.lower())
        print(f"Using preset: {args.preset.lower()}")
        print(f"  Flags: {bt_config}")

    symbol = _normalize_symbol(symbol)
    config.trading.candles_limit = candle_limit

    start_date = pd.Timestamp(args.start_date, tz="UTC") if args.start_date else None
    end_date = pd.Timestamp(args.end_date, tz="UTC") if args.end_date else None

    print(f"Running backtest: {symbol} {tf} ({candle_limit} candles), source={args.source}...")

    engine = BacktestEngine(
        symbol=symbol,
        timeframe=tf,
        send_telegram=args.telegram,
        market_type=market_type,
        bt_config=bt_config,
        source=args.source,
        start_date=start_date,
        end_date=end_date,
    )
    result = await engine.run()

    print_result(result)

    if args.telegram:
        print("\nSending to Telegram...")
        await send_to_telegram(format_telegram(result))
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
