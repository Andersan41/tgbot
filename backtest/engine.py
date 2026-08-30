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
from strategy.signal_evaluator import (
    estimate_p_tp,
    apply_symbol_overrides,
    htf_opposition,
    dedup_block_window,
    entry_zone_touched,
)
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
    funding_pct: float = 0.0
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
    # Path quality (updated per-bar while the position is open)
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    # Per-FVG dedup key: (fvg_type, top, bottom) — prevents re-entry on same FVG
    fvg_key: Optional[tuple] = None


@dataclass
class PendingOrder:
    """A resting limit order at the FVG median (execution_model="limit_pending").

    Created when a signal passes every gate, but NOT filled on the signal bar.
    It fills on the first LATER bar whose low/high actually touches the median,
    and expires when ``execution_pending_max_bars`` elapse. The FVG reference
    (type/top/bottom) lets the engine check whether the generating imbalance is
    still alive; the median-entry property (median inside the gap) means a fully
    consumed gap already implies the median was touched, so expiry is the primary
    cancellation path.
    """
    symbol: str
    timeframe: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    sl: float
    tp: float
    sl_source: str = "atr"
    signal_index: int = 0
    signal_timestamp: str = ""
    fvg_type: Optional[str] = None
    fvg_top: float = 0.0
    fvg_bottom: float = 0.0
    regime: str = ""
    signal_score: int = 0
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
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
    entry_zone_not_reached: int = 0
    pending_expired: int = 0
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
    "ENTRY_ZONE",
    "RISK_ENGINE_BLOCKED",
    "DEDUP",
    "PASSED",
]

# Which pipeline steps are actually implemented in the backtest
BACKTEST_ACTIVE: dict[str, bool] = {
    "NO_PATTERN": True,
    "SETUP_TYPE_GATE": True,
    "HTF_BIAS_BLOCKED": True,
    "ENTRY_ZONE": True,
    "RISK_ENGINE_BLOCKED": True,
    "DEDUP": True,
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
    max_drawdown_sized: float = 0.0
    total_pnl_pct: float = 0.0
    total_net_pnl_pct: float = 0.0
    signals_generated: int = 0
    signals_rejected: int = 0
    exposure_time_pct: float = 0.0
    avg_trade_duration: float = 0.0
    sharpe_annualized: float = 0.0
    avg_mfe_pct: float = 0.0
    avg_mae_pct: float = 0.0
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
    enable_htf_bias_gate: bool = True
    min_p_tp: float = 0.0
    min_score_for_signal: Optional[int] = None
    # Intrabar SL/TP resolution model:
    #   "conservative" — SL checked first on same bar (default, matches live)
    #   "optimistic"   — TP checked first on same bar
    intrabar_model: Literal["conservative", "optimistic"] = "conservative"


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
        "enable_htf_bias_gate": True,
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
        self._df_override = False

    async def run(self, df: Optional[pd.DataFrame] = None, htf_base: Optional[pd.DataFrame] = None) -> BacktestResult:
        """Run the full backtest pipeline.

        ``df`` — optional pre-loaded/resampled OHLCV DataFrame (DatetimeIndex
        UTC, columns open/high/low/close/volume). When provided it is used
        directly instead of reading from the local cache or the exchange.
        HTF Bias V2 series (1w/1d/4h) are resampled on the fly: from
        ``htf_base`` when supplied (preferred — avoids up-sampling the main
        df, which corrupts HTF series for 1d/1w timeframes), otherwise from
        ``df`` itself.
        """
        if self.market_type:
            if self.market_type in ("future", "futures"):
                config.exchange.market_type = "future"
            elif self.market_type == "spot":
                config.exchange.market_type = "spot"

        if df is not None:
            self._local_data = (df, self._build_htf_from_df(df, htf_base))
            self.source = "local"
            self._df_override = True
            return await self._run_backtest()

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

    def _build_htf_from_df(self, df: pd.DataFrame, htf_base: Optional[pd.DataFrame] = None) -> dict:
        """Build the HTF Bias V2 dict (1w/1d/4h) from a supplied DataFrame.

        Prefer ``htf_base`` (e.g. the original 15m frame) when given: building
        the 4h/1d/1w series from an already-resampled ``df`` would UP-sample
        for 1d/1w main timeframes and produce empty/garbage HTF series.
        """
        from backtest.resampler import resample_ohlcv
        src = htf_base if htf_base is not None else df
        htf: dict = {}
        for tf in ("1w", "1d", "4h"):
            if tf == self.timeframe:
                htf[tf] = df
            else:
                htf[tf] = resample_ohlcv(src, tf)
        return htf

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

        warmup = max(80, config.trading.candles_limit // 2)
        candle_limit = config.trading.candles_limit
        if not self._df_override:
            df = df.iloc[-(candle_limit + 60):] if len(df) > candle_limit + 60 else df

        # Pre-compute indicator columns once (O(n)). Indicators are causal, so the
        # value at row i equals the per-window value — this eliminates the O(n²)
        # full recompute that used to happen on every candle.
        if self._indicator_engine.precompute(df, self.symbol, self.timeframe) is None:
            return BacktestResult(symbol=self.symbol, timeframe=self.timeframe)

        trades: list[BacktestTrade] = []
        reject_stats = RejectStats()
        atr_history: list[float] = []
        ema_spread_history: list[float] = []
        volume_history: list[float] = []
        open_trades: list[BacktestTrade] = []
        pending_orders: list[PendingOrder] = []
        signals_count = 0
        last_signal_time: Optional[pd.Timestamp] = None
        last_signal_direction: Optional[str] = None
        _traded_fvgs: set[tuple] = set()  # per-FVG dedup: (type, top, bottom)

        # Pre-fetch HTF data for bias.
        # local path: full HTF history available → compute per-candle (no look-ahead).
        # live path: fetch current HTF state once (mirrors the live scanner).
        _htf_once: Optional[HTFBiasResult] = None
        if config.htf_bias_v2 and htf is None:
            try:
                df_1d = await exchange_client.fetch_ohlcv(self.symbol, "1d", limit=60)
                df_4h = await exchange_client.fetch_ohlcv(self.symbol, "4h", limit=60)
                df_1w = await exchange_client.fetch_ohlcv(self.symbol, "1w", limit=60)
                _htf_once = get_htf_bias_v2(df_1w, df_1d, df_4h, df)
            except Exception:
                _htf_once = None

        # Funnel instrumentation (populated only when self.instrument=True)
        _funnel_counts: dict[str, int] = {s: 0 for s in FUNNEL_STEPS}

        for i in range(warmup, len(df)):
            # Analysis window capped at `candle_limit` — mirrors the live scanner,
            # which fetches exactly candles_limit candles. This is both a parity fix
            # (backtest no longer sees older history than live) and the main speedup
            # (O(n×limit) instead of O(n²)).
            window = df.iloc[max(0, i - candle_limit + 1): i + 1].copy()
            ind = self._indicator_engine.values_at(df, self.symbol, self.timeframe, i)
            if ind is None:
                continue

            atr_history.append(float(ind.atr))
            ema_spread_history.append(
                abs(float(ind.ema_fast) - float(ind.ema_slow)) / float(ind.ema_slow) * 100
                if float(ind.ema_slow) > 0 else 0.0
            )
            volume_history.append(float(ind.volume))

            # --- Pending order processing (execution_model="limit_pending") ---
            # A resting limit at the FVG median fills only when this bar actually
            # trades at the median (no phantom fill at an unreached price). A bar
            # that fills the order can also hit SL/TP on the same bar (fill then
            # stop/limit) — realistic for a limit order, so fills are processed
            # BEFORE the exit check below. Expires after execution_pending_max_bars;
            # a fully consumed gap implies the median was touched (median is
            # strictly inside the gap), so time expiry is the primary cancellation.
            if config.execution_model == "limit_pending" and pending_orders:
                _bar_high = float(ind.high) if ind.high else float(ind.close)
                _bar_low = float(ind.low) if ind.low else float(ind.close)
                _alive: list[PendingOrder] = []
                for po in pending_orders:
                    _touched = (
                        (po.direction == "BUY" and _bar_low <= po.entry_price)
                        or (po.direction == "SELL" and _bar_high >= po.entry_price)
                    )
                    if _touched:
                        open_trades.append(BacktestTrade(
                            symbol=po.symbol,
                            timeframe=po.timeframe,
                            direction=po.direction,
                            entry_price=po.entry_price,
                            entry_index=i,
                            entry_timestamp=str(df.index[i]),
                            sl=po.sl,
                            tp=po.tp,
                            sl_source=po.sl_source,
                            regime=po.regime,
                            signal_score=po.signal_score,
                            confidence=po.confidence,
                            reasons=list(po.reasons),
                            p_tp=po.p_tp,
                            risk_pct=po.risk_pct,
                            setup_type=po.setup_type,
                            components=list(po.components),
                        ))
                        continue
                    if (i - po.signal_index) > config.execution_pending_max_bars:
                        reject_stats.pending_expired += 1
                        reject_stats.total_rejected += 1
                        continue
                    _alive.append(po)
                pending_orders = _alive

            # --- Exit check (all open positions) ---
            # Intrabar model: when SL and TP are both hit on the same bar,
            # the order of checking determines the outcome.
            #   "conservative" (default): SL first → pessimistic, matches live exchange fills
            #   "optimistic": TP first → best-case fill order
            _intrabar = getattr(self.bt_config, "intrabar_model", "conservative")
            if open_trades:
                high, low = float(ind.high), float(ind.low)
                _close_price = float(ind.close)
                remaining: list[BacktestTrade] = []
                for t in open_trades:
                    entry = t.entry_price
                    if t.direction == "BUY":
                        t.mfe_pct = max(t.mfe_pct, (high - entry) / entry * 100)
                        t.mae_pct = min(t.mae_pct, (low - entry) / entry * 100)
                        _sl_hit = low <= t.sl
                        _tp_hit = high >= t.tp
                        if _sl_hit and _tp_hit:
                            # Both on same bar — resolve by intrabar model
                            if _intrabar == "optimistic":
                                t.exit_price, t.exit_index, t.exit_reason = t.tp, i, "tp"
                            else:
                                t.exit_price, t.exit_index, t.exit_reason = t.sl, i, "sl"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                        if _sl_hit:
                            t.exit_price, t.exit_index, t.exit_reason = t.sl, i, "sl"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                        if _tp_hit:
                            t.exit_price, t.exit_index, t.exit_reason = t.tp, i, "tp"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                    else:
                        t.mfe_pct = max(t.mfe_pct, (entry - low) / entry * 100)
                        t.mae_pct = min(t.mae_pct, (entry - high) / entry * 100)
                        _sl_hit = high >= t.sl
                        _tp_hit = low <= t.tp
                        if _sl_hit and _tp_hit:
                            if _intrabar == "optimistic":
                                t.exit_price, t.exit_index, t.exit_reason = t.tp, i, "tp"
                            else:
                                t.exit_price, t.exit_index, t.exit_reason = t.sl, i, "sl"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                        if _sl_hit:
                            t.exit_price, t.exit_index, t.exit_reason = t.sl, i, "sl"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                        if _tp_hit:
                            t.exit_price, t.exit_index, t.exit_reason = t.tp, i, "tp"
                            t.exit_timestamp = str(df.index[i])
                            trades.append(t)
                            continue
                    # Max-duration exit: close stale trades at current price
                    if config.max_trade_duration_bars > 0 and (i - t.entry_index) >= config.max_trade_duration_bars:
                        t.exit_price, t.exit_index, t.exit_reason = _close_price, i, "max_dur"
                        t.exit_timestamp = str(df.index[i])
                        trades.append(t)
                        continue
                    remaining.append(t)
                open_trades = remaining

            # --- New signal (limited by max active positions) ---
            if len(open_trades) < config.max_active_signals:
                # Portfolio risk gate (live Phase 0.2): total risk of open positions
                _open_risk = sum(t.risk_pct for t in open_trades)
                if _open_risk >= config.max_portfolio_risk_pct:
                    reject_stats.other_rejected += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                signals_count += 1
                regime_obj = _compute_regime(ind, atr_history, ema_spread_history, volume_history)

                # === Compression Regime Gate (live Phase 0.4) ===
                if config.trading.block_compression_regime and regime_obj is not None:
                    if regime_obj.regime == "compression":
                        reject_stats.other_rejected += 1
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                        continue
                    # Block range + high ADX combo (failure cluster pattern)
                    if regime_obj.regime == "range" and float(ind.adx or 0) >= 26:
                        if len(window) >= 20:
                            _recent = window.tail(20)
                            _range_pct = (_recent["high"].max() - _recent["low"].min()) / _recent["low"].min() * 100
                            _mid = (_recent["high"].max() + _recent["low"].min()) / 2
                            _price = float(window["close"].iloc[-1])
                            _dist_mid = abs(_price - _mid) / _mid * 100
                            if _range_pct < 1.5 and _dist_mid < 0.3:
                                reject_stats.other_rejected += 1
                                reject_stats.total_rejected += 1
                                if self.instrument:
                                    _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                                continue

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
                    df=_df_clean,
                )

                if not setup.detected:
                    reject_stats.no_pattern += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["NO_PATTERN"] += 1
                    continue

                # === Phase 1.35: Direction / Symbol filter (live order) ===
                if config.direction_filter.block_all_sell and setup.direction == "sell":
                    reject_stats.other_rejected += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                _blocked_dir = config.direction_filter.blocked_symbol_directions.get(self.symbol)
                if _blocked_dir is not None and setup.direction == _blocked_dir.lower():
                    reject_stats.other_rejected += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                # === Phase 1.35: Confluence Mode (v3.0) ===
                from config.settings import STRATEGY_MODE, StrategyMode
                if STRATEGY_MODE == StrategyMode.CONFLUENCE:
                    if setup.setup_type == "reversal":
                        reject_stats.other_rejected += 1
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                        continue
                    if setup.setup_type == "continuation" and not setup.has_ob:
                        reject_stats.other_rejected += 1
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
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

                # === Phase 1.45: HTF Bias (per-candle in local path → no look-ahead) ===
                # Local path slices HTF history up to the current candle; the
                # live-fetch path reuses `_htf_once` (mirrors the live scanner,
                # which also reads the current HTF state).
                _htf_result: Optional[HTFBiasResult] = _htf_once
                _htf_penalty = 1.0
                if config.htf_bias_v2:
                    if htf is not None:
                        try:
                            _ts = df.index[i]
                            _f1h = df.iloc[:i + 1]
                            _f4h = htf.get("4h")
                            _f1d = htf.get("1d")
                            _f1w = htf.get("1w")
                            _htf_result = get_htf_bias_v2(
                                _f1w.loc[:_ts].tail(60) if _f1w is not None and len(_f1w) else None,
                                _f1d.loc[:_ts].tail(60) if _f1d is not None and len(_f1d) else None,
                                _f4h.loc[:_ts].tail(60) if _f4h is not None and len(_f4h) else None,
                                _f1h,
                            )
                        except Exception:
                            _htf_result = None
                    if self.bt_config.enable_htf_bias_gate and _htf_result is not None:
                        _opposed, _hard_block = htf_opposition(setup, _htf_result.direction)
                        if _hard_block:
                            reject_stats.htf_bias_blocked += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["HTF_BIAS_BLOCKED"] += 1
                            continue
                        if _opposed:
                            _htf_penalty = config.htf_bias_continuation_penalty

                # === Phase 1.44: Entry Zone (opt-in hard gate, shared with live) ===
                # require_entry_zone=True: only emit when the current bar actually
                # traded in the FVG entry zone (no phantom fills at an unreached
                # FVG median). Shared function = identical decision in live+backtest.
                if config.require_entry_zone:
                    bar_high = float(ind.high) if ind.high else float(ind.close)
                    bar_low = float(ind.low) if ind.low else float(ind.close)
                    if not entry_zone_touched(setup.direction, fvgs, bar_high, bar_low):
                        reject_stats.entry_zone_not_reached += 1
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["ENTRY_ZONE"] += 1
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

                entry_price = float(trade_plan.entry_price)
                sl = trade_plan.sl
                tp = trade_plan.tp

                # === Execution model ===
                # "close": the signal fires at candle close, so the fill price is
                # the signal bar's close (mirrors live P&L tracking, which computes
                # from signal.close_price). "median_immediate" (default) keeps the
                # FVG median even if price never traded there; "limit_pending" keeps
                # the median as a resting limit and fills only on a later bar's touch.
                if config.execution_model == "close":
                    entry_price = float(ind.close) if ind.close else entry_price

                # === Phase 2: Analytics (inline) ===
                atr_pct = (float(ind.atr) / float(ind.close) * 100) if ind.atr and ind.close > 0 else 0.0

                # === Per-symbol overrides (live Phase 1.46) ===
                _ov_blocked, _ov_reason = apply_symbol_overrides(
                    symbol=self.symbol,
                    ind=ind,
                    setup=setup,
                    trade_plan=trade_plan,
                    atr_pct=atr_pct,
                )
                if _ov_blocked:
                    reject_stats.other_rejected += 1
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                # === Phase 3: Inline Probability (shared with live scanner) ===
                # context_score/mtf_aligned are unavailable historically → 0/False
                _components_score = setup.components_count if setup.detected else 0
                p_tp, confidence = estimate_p_tp(
                    setup=setup,
                    context_score=0.0,
                    htf_result=_htf_result,
                    htf_penalty=_htf_penalty,
                    mtf_aligned=False,
                )

                # Optional probability gate
                if self.bt_config.enable_probability_gate and p_tp < self.bt_config.min_p_tp:
                    reject_stats.total_rejected += 1
                    if self.instrument:
                        _funnel_counts["RISK_ENGINE_BLOCKED"] += 1
                    continue

                # === Phase 4: Risk Engine ===
                risk_decision = risk_engine.evaluate(
                    portfolio=PortfolioState(
                        active_count=len(open_trades),
                        total_risk_pct=sum(t.risk_pct for t in open_trades),
                        max_active_signals=config.max_active_signals,
                        max_portfolio_risk_pct=config.max_portfolio_risk_pct,
                    ),
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

                # === Phase 6: Dedup (shared live gate — dedup_block_window in
                # strategy/signal_evaluator.py; same-dir within effective cooldown,
                # cross-dir within half cooldown) ===
                is_buy = setup.direction == "buy"
                _sig_dir = "BUY" if is_buy else "SELL"
                if last_signal_time is not None:
                    window_min = dedup_block_window(
                        last_signal_time.to_pydatetime(),
                        df.index[i].to_pydatetime(),
                        last_signal_direction or "",
                        _sig_dir,
                        self.timeframe,
                        config.signal_cooldown_minutes,
                        config.signal_cooldown_tf_multiplier,
                    )
                    if window_min is not None:
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["DEDUP"] += 1
                        continue

                # === Build Trade ===
                if self.instrument:
                    _funnel_counts["PASSED"] += 1

                _ref_fvg = None
                for _f in fvgs or []:
                    _f_dir = "buy" if _f.type == "bullish" else "sell" if _f.type == "bearish" else _f.type
                    if _f.is_active and _f_dir == setup.direction:
                        _ref_fvg = _f
                        break

                if config.execution_model == "limit_pending":
                    # Resting limit at the FVG median: no trade yet — registered and
                    # filled only when a later bar actually touches the median.
                    # Per-FVG dedup: while the same FVG (type+top+bottom) already
                    # has an alive pending, do NOT re-register — one signal per FVG
                    # (the user's "один сигнал → одна сделка"), which collapses the
                    # phantom re-entry stacks even after a touch delay.
                    _fvg_key = None
                    if _ref_fvg is not None:
                        _fvg_key = (round(float(_ref_fvg.top), 8), round(float(_ref_fvg.bottom), 8))
                    _dup = False
                    if _fvg_key is not None:
                        for _po in pending_orders:
                            if _po.fvg_type == _ref_fvg.type and (
                                round(float(_po.fvg_top), 8), round(float(_po.fvg_bottom), 8)
                            ) == _fvg_key:
                                _dup = True
                                break
                    if not _dup:
                        pending_orders.append(PendingOrder(
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            direction="BUY" if is_buy else "SELL",
                            entry_price=entry_price,
                            sl=sl,
                            tp=tp,
                            sl_source=trade_plan.sl_source or "atr",
                            signal_index=i,
                            signal_timestamp=str(df.index[i]),
                            fvg_type=_ref_fvg.type if _ref_fvg else None,
                            fvg_top=float(_ref_fvg.top) if _ref_fvg else 0.0,
                            fvg_bottom=float(_ref_fvg.bottom) if _ref_fvg else 0.0,
                            regime=regime_obj.regime if regime_obj else "",
                            signal_score=setup.components_count,
                            confidence=p_tp * 100,
                            reasons=list(setup.components_found),
                            p_tp=p_tp,
                            risk_pct=risk_decision.risk_pct,
                            setup_type=setup.setup_type or "",
                            components=list(setup.components_found),
                        ))
                    else:
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["DEDUP"] += 1
                else:
                    # Per-FVG dedup: skip if the same FVG (type+top+bottom) already
                    # produced a trade that is still open or was recently closed.
                    _fvg_key = None
                    if _ref_fvg is not None:
                        _fvg_key = (
                            _ref_fvg.type,
                            round(float(_ref_fvg.top), 8),
                            round(float(_ref_fvg.bottom), 8),
                        )
                    if _fvg_key is not None and _fvg_key in _traded_fvgs:
                        reject_stats.total_rejected += 1
                        if self.instrument:
                            _funnel_counts["DEDUP"] += 1
                        continue

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
                        confidence=p_tp * 100,
                        reasons=list(setup.components_found),
                        p_tp=p_tp,
                        risk_pct=risk_decision.risk_pct,
                        setup_type=setup.setup_type or "",
                        components=list(setup.components_found),
                        fvg_key=_fvg_key,
                    )
                    if _fvg_key is not None:
                        _traded_fvgs.add(_fvg_key)
                    open_trades.append(ct)
                last_signal_time = df.index[i]
                last_signal_direction = _sig_dir

                if 0 < self.max_trades <= len(trades):
                    break

        # Close open trades at end of data
        if open_trades:
            last_close = float(df.iloc[-1]["close"])
            for t in open_trades:
                t.exit_price = last_close
                t.exit_index = len(df) - 1
                t.exit_timestamp = str(df.index[-1])
                t.exit_reason = "eob"
                trades.append(t)

        # --- Compute PnL with commission/slippage ---
        fee_pct = config.trading.exchange_fee_pct / 100.0
        slip_pct = config.trading.slippage_pct / 100.0
        tf_hours = {
            "1m": 1 / 60, "3m": 3 / 60, "5m": 5 / 60, "15m": 0.25, "30m": 0.5,
            "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "1d": 24, "1w": 168,
        }.get(self.timeframe, 1.0)

        for t in trades:
            if t.direction == "BUY":
                gross = (t.exit_price - t.entry_price) / t.entry_price * 100
            else:
                gross = (t.entry_price - t.exit_price) / t.entry_price * 100
            t.pnl_pct = round(gross, 4)

            # Commission: 2 sides (entry + exit)
            total_cost_pct = (fee_pct * 2 + slip_pct * 2) * 100

            # Funding (swap): charged for each 8h boundary crossed while holding.
            # Approximation — assumes funding is always paid (conservative).
            t.funding_pct = 0.0
            funding_rate = getattr(config.derivatives, "funding_rate_pct_8h", 0.0) or 0.0
            if funding_rate > 0 and t.exit_index is not None and t.exit_index >= t.entry_index:
                dur_hours = (t.exit_index - t.entry_index) * tf_hours
                periods = int(dur_hours // 8)
                t.funding_pct = -round(funding_rate * periods, 4)

            t.net_pnl_pct = round(gross - total_cost_pct + t.funding_pct, 4)

            risk = abs(t.entry_price - t.sl)
            t.rr = round(abs(t.exit_price - t.entry_price) / risk, 2) if risk > 0 else 0.0

        # Build funnel data
        if self.instrument:
            self.funnel_data = FunnelData(
                symbol=self.symbol,
                steps=_funnel_counts,
                total_signals_processed=signals_count,
            )

        return self._build_result(trades, signals_count, reject_stats, len(df), tf_hours)

    def _build_result(
        self,
        trades: list[BacktestTrade],
        signals_count: int,
        reject_stats: RejectStats,
        total_candles: int,
        tf_hours: float = 1.0,
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
        gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

        winrate = win_count / total
        avg_win = gross_profit / win_count if win_count > 0 else 0.0
        avg_loss = gross_loss / loss_count if loss_count > 0 else 0.0
        expectancy = winrate * avg_win - (1 - winrate) * avg_loss

        if len(net_pnl_values) > 1:
            std_net = float(np.std(net_pnl_values, ddof=1))
            sharpe_net = (avg_net_pnl / std_net) if std_net > 0 else 0.0
        else:
            sharpe_net = 0.0

        # Annualized Sharpe: per-trade Sharpe scaled by trade frequency.
        # Approximation — per-trade returns are treated as a return stream and
        # annualized by trades-per-year (equivalent time assumes uniform spacing).
        days = total_candles * tf_hours / 24.0 if tf_hours > 0 else 0.0
        trades_per_year = total / days * 365.25 if days > 0 else 0.0
        sharpe_annualized = sharpe_net * (trades_per_year ** 0.5) if trades_per_year > 0 else 0.0

        # Max drawdown on NET pnl (honest: excludes commission/slippage)
        cum_net = np.cumsum(net_pnl_values)
        peak_net = np.maximum.accumulate(cum_net)
        dd_net = peak_net - cum_net
        max_dd = float(np.max(dd_net)) if len(dd_net) > 0 else 0.0

        # Sizing-aware equity curve: each trade risks `risk_pct`% of current equity,
        # with position notional = risk_pct / SL-distance(%). Drawdown reflects real exposure.
        _equity = 100.0
        _equity_peak = 100.0
        sized_max_dd = 0.0
        for t in trades:
            _sl_dist = abs(t.entry_price - t.sl) / t.entry_price * 100 if t.entry_price else 0.0
            if _sl_dist > 0:
                _equity *= (1 + t.net_pnl_pct * t.risk_pct / _sl_dist / 100.0)
            _equity_peak = max(_equity_peak, _equity)
            sized_max_dd = max(sized_max_dd, _equity_peak - _equity)

        # MFE/MAE (path quality): avg best-favorable / worst-adverse excursion
        _mfe = [t.mfe_pct for t in trades if t.mfe_pct != 0.0]
        _mae = [t.mae_pct for t in trades if t.mae_pct != 0.0]
        avg_mfe = float(np.mean(_mfe)) if _mfe else 0.0
        avg_mae = float(np.mean(_mae)) if _mae else 0.0

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
            d_pf = d_pf_num / d_pf_den if d_pf_den > 0 else 0.0
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
            sharpe_ratio=round(sharpe_net, 2),
            max_drawdown=round(max_dd, 4),
            max_drawdown_sized=round(sized_max_dd, 4),
            total_pnl_pct=round(total_pnl, 4),
            total_net_pnl_pct=round(total_net_pnl, 4),
            signals_generated=signals_count,
            signals_rejected=reject_stats.total_rejected,
            exposure_time_pct=round(exposure_pct, 2),
            avg_trade_duration=round(avg_dur, 1),
            sharpe_annualized=round(sharpe_annualized, 2),
            avg_mfe_pct=round(avg_mfe, 4),
            avg_mae_pct=round(avg_mae, 4),
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
