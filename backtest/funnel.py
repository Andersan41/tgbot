"""
backtest/funnel.py — Instrumented signal pipeline funnel analysis.

Runs the full_new backtest configuration with per-signal tracking to
classify where each potential signal was stopped in the pipeline.

OPTIMIZATION: Calculates indicators once on the full dataset, then extracts
per-bar values from the DataFrame columns. ~20x faster than expanding-window.

Usage:
    python -m backtest.funnel
    python -m backtest.funnel --symbols BTC/USDT,ETH/USDT
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger as _loguru_logger
_loguru_logger.disable("strategy.signal_engine")
_loguru_logger.disable("indicators.engine")
import logging
logging.getLogger("strategy.signal_engine").setLevel(logging.WARNING)
logging.getLogger("indicators.engine").setLevel(logging.WARNING)

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, BacktestTrade, _compute_regime
from backtest.cache_ohlcv import load_cached, CONFIRM_TIMEFRAME, CONFIRM_CANDLES
from config.settings import config
from indicators.engine import IndicatorEngine, IndicatorValues, _safe_float
from strategy.signal_engine import signal_engine, SignalType
from risk.dynamic_risk import calculate_structural_sl, calculate_structural_tp
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from market_structure.structure import analyze_structure
import pandas_ta as ta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
    "AVAX/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT", "UNI/USDT",
    "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT",
    "INJ/USDT", "WIF/USDT", "FLOKI/USDT", "FIL/USDT", "GRT/USDT",
]

TIMEFRAME = "1h"
CANDLES = 3900
RESULTS_DIR = Path(__file__).parent.parent / "reports" / "funnel"

BT_CONFIG = BacktestConfig()

PIPELINE_STEPS = [
    "NO_SIGNAL_ENGINE", "CONFIRM_TF_REJECT", "DISTANCE_FILTER",
    "TP_PATH_BLOCKED", "MTF_ALIGNMENT", "BTC_CORRELATION",
    "ETH_CORRELATION", "VOLATILITY_REGIME", "CONTEXT_BLOCKED",
    "NEWS_FILTER", "SL_DISTANCE_MIN", "SL_DISTANCE_MAX",
    "RR_GUARD", "NO_TRADE_ZONE", "DYNAMIC_RISK_WEAK",
    "COOLDOWN", "PORTFOLIO_RISK", "PASSED",
]


# ---------------------------------------------------------------------------
# Per-symbol funnel data
# ---------------------------------------------------------------------------

ENGINE_REJECTION_REASONS = [
    "ENGINE_DATA_INVALID", "ENGINE_NO_DIRECTION", "ENGINE_ADX_FLAT",
    "ENGINE_SUPERTREND", "ENGINE_REGIME_BLOCK", "ENGINE_NO_TRIGGER",
    "ENGINE_EMA_ALIGNMENT", "ENGINE_EMA_SPREAD", "ENGINE_EMA_SLOPE",
    "ENGINE_MIN_SCORE", "ENGINE_CANDLE_CLOSE", "ENGINE_OTHER",
]


@dataclass
class SymbolFunnel:
    symbol: str
    total_bars: int = 0
    counts: dict = field(default_factory=lambda: {step: 0 for step in PIPELINE_STEPS})
    engine_rejection_scores: dict = field(default_factory=lambda: {s: 0 for s in range(8)})
    engine_rejection_has_score_gte4: int = 0
    engine_rejection_total: int = 0
    engine_rejection_subreasons: dict = field(default_factory=lambda: {r: 0 for r in ENGINE_REJECTION_REASONS})
    passed_sl_sources: dict = field(default_factory=lambda: {"atr": 0, "bos": 0, "structural": 0})
    passed_scores: dict = field(default_factory=lambda: {s: 0 for s in range(1, 8)})
    passed_directions: dict = field(default_factory=lambda: {"BUY": 0, "SELL": 0})
    passed_regimes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fast indicator extraction: compute once, read per-bar
# ---------------------------------------------------------------------------

def _precompute_indicators(df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Calculate all indicators on the full dataset once. Returns enriched DataFrame."""
    cfg = config.trading
    df = df.copy()

    df["ema_fast"] = ta.ema(df["close"], length=cfg.ema_fast)
    df["ema_slow"] = ta.ema(df["close"], length=cfg.ema_slow)
    df["ema_trend"] = ta.ema(df["close"], length=cfg.ema_trend)
    df["rsi"] = ta.rsi(df["close"], length=cfg.rsi_period)

    macd_df = ta.macd(df["close"], fast=cfg.macd_fast, slow=cfg.macd_slow, signal=cfg.macd_signal)
    if macd_df is not None:
        macd_col = [c for c in macd_df.columns if c.startswith("MACD_")][0]
        hist_col = [c for c in macd_df.columns if c.startswith("MACDh_")][0]
        signal_col = [c for c in macd_df.columns if c.startswith("MACDs_")][0]
        df["macd"] = macd_df[macd_col]
        df["macd_signal_line"] = macd_df[signal_col]
        df["macd_hist"] = macd_df[hist_col]
    else:
        df["macd"] = np.nan
        df["macd_signal_line"] = np.nan
        df["macd_hist"] = np.nan

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=cfg.adx_period)
    if adx_df is not None:
        adx_cols = list(adx_df.columns)
        adx_col = [c for c in adx_cols if c.startswith("ADX_") and "R" not in c]
        dmp_col = [c for c in adx_cols if c.startswith("DMP_")]
        dmn_col = [c for c in adx_cols if c.startswith("DMN_")]
        df["adx"] = adx_df[adx_col[0]] if adx_col else np.nan
        df["dmi_plus"] = adx_df[dmp_col[0]] if dmp_col else np.nan
        df["dmi_minus"] = adx_df[dmn_col[0]] if dmn_col else np.nan
    else:
        df["adx"] = np.nan
        df["dmi_plus"] = np.nan
        df["dmi_minus"] = np.nan

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=cfg.atr_period)
    df["volume_sma"] = ta.sma(df["volume"], length=cfg.volume_sma_period)

    if "taker_buy_volume" in df.columns:
        buy_vol = df["taker_buy_volume"]
        sell_vol = df["volume"] - buy_vol
        df["volume_delta_pct"] = ((buy_vol - sell_vol) / df["volume"]) * 100
    else:
        df["volume_delta_pct"] = None

    st_df = ta.supertrend(
        df["high"], df["low"], df["close"],
        length=cfg.supertrend_period,
        multiplier=cfg.supertrend_multiplier,
    )
    if st_df is not None:
        st_cols = list(st_df.columns)
        st_val_col = [c for c in st_cols if c.startswith("SUPERT_") and "d" not in c and "l" not in c and "s" not in c]
        st_dir_col = [c for c in st_cols if "SUPERTd_" in c]
        if st_val_col and st_dir_col:
            df["supertrend"] = st_df[st_val_col[0]]
            df["supertrend_dir"] = st_df[st_dir_col[0]]
        else:
            df["supertrend"] = np.nan
            df["supertrend_dir"] = 0
    else:
        df["supertrend"] = np.nan
        df["supertrend_dir"] = 0

    # Prev values for cross detection
    df["ema_fast_prev"] = df["ema_fast"].shift(1)
    df["ema_slow_prev"] = df["ema_slow"].shift(1)
    df["macd_hist_prev"] = df["macd_hist"].shift(1)

    return df


def _extract_indicator(df: pd.DataFrame, i: int, symbol: str, timeframe: str) -> Optional[IndicatorValues]:
    """Extract IndicatorValues for bar i from pre-computed DataFrame."""
    try:
        row = df.iloc[i]
        if pd.isna(row.get("ema_fast")) or pd.isna(row.get("adx")) or pd.isna(row.get("atr")):
            return None

        prev = df.iloc[i - 1] if i > 0 else row

        return IndicatorValues(
            symbol=symbol,
            timeframe=timeframe,
            close=_safe_float(row["close"]),
            high=_safe_float(row["high"]),
            low=_safe_float(row["low"]),
            volume=_safe_float(row["volume"]),
            ema_fast=_safe_float(row["ema_fast"]),
            ema_slow=_safe_float(row["ema_slow"]),
            ema_trend=_safe_float(row["ema_trend"]),
            ema_fast_prev=_safe_float(prev["ema_fast"]),
            ema_slow_prev=_safe_float(prev["ema_slow"]),
            rsi=_safe_float(row["rsi"]),
            macd=_safe_float(row.get("macd")),
            macd_signal=_safe_float(row.get("macd_signal_line")),
            macd_hist=_safe_float(row.get("macd_hist")),
            macd_hist_prev=_safe_float(prev.get("macd_hist")),
            adx=_safe_float(row["adx"]),
            dmi_plus=_safe_float(row.get("dmi_plus")),
            dmi_minus=_safe_float(row.get("dmi_minus")),
            atr=_safe_float(row["atr"]),
            supertrend=_safe_float(row.get("supertrend"), _safe_float(row["close"])),
            supertrend_direction=int(_safe_float(row.get("supertrend_dir"), 0)),
            volume_sma=_safe_float(row.get("volume_sma"), _safe_float(row["volume"])),
            volume_delta_pct=_safe_float(row.get("volume_delta_pct"), None) if row.get("volume_delta_pct") is not None else None,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Instrumented backtest runner
# ---------------------------------------------------------------------------

async def run_funnel_one(symbol: str, candles: int) -> SymbolFunnel:
    """Run instrumented funnel analysis for one symbol."""
    funnel = SymbolFunnel(symbol=symbol)
    config.trading.candles_limit = 200

    cached_1h = load_cached(symbol, TIMEFRAME, candles)
    cached_15m = load_cached(symbol, CONFIRM_TIMEFRAME, CONFIRM_CANDLES)

    if cached_1h is None:
        print(f"  [SKIP] {symbol}: no cached 1h data", flush=True)
        return funnel

    # Pre-compute all indicators on full dataset (fast: single pass)
    enriched_df = _precompute_indicators(cached_1h, symbol, TIMEFRAME)
    if enriched_df is None:
        return funnel

    warmup = 80
    df = enriched_df.iloc[-(candles + 60):] if len(enriched_df) > candles + 60 else enriched_df
    confirm_df = cached_15m

    # Pre-compute confirm TF indicators if needed
    enriched_15m = None
    if config.trading.confirm_tf_enabled and confirm_df is not None and CONFIRM_TIMEFRAME != TIMEFRAME:
        enriched_15m = _precompute_indicators(confirm_df, symbol, CONFIRM_TIMEFRAME)

    atr_history: list[float] = []
    ema_spread_history: list[float] = []
    volume_history: list[float] = []
    in_trade = False
    ct: Optional[BacktestTrade] = None

    for i in range(warmup, len(df)):
        ind = _extract_indicator(df, i, symbol, TIMEFRAME)
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
                if low <= ct.sl or high >= ct.tp:
                    in_trade = False
                    ct = None
            else:
                if high >= ct.sl or low <= ct.tp:
                    in_trade = False
                    ct = None

        # --- New signal attempt ---
        if not in_trade:
            funnel.total_bars += 1
            rejection = None

            # Step 1: Confirm TF gate
            entry_price = float(ind.close)
            confirm_available = (
                config.trading.confirm_tf_enabled
                and enriched_15m is not None
                and CONFIRM_TIMEFRAME != TIMEFRAME
            )
            if confirm_available:
                primary_ts = df.index[i]
                try:
                    confirm_idx = enriched_15m.index.get_indexer([primary_ts], method="nearest")[0]
                    if 0 <= confirm_idx < len(enriched_15m):
                        confirm_ind = _extract_indicator(enriched_15m, confirm_idx, symbol, CONFIRM_TIMEFRAME)
                        if confirm_ind is not None:
                            direction_str = "buy" if ind.ema_fast > ind.ema_slow else "sell"
                            confirm_ok = signal_engine.evaluate_confirm(confirm_ind, direction_str)
                            entry_price = float(confirm_ind.close)
                except Exception:
                    pass

            # Step 2: Signal engine evaluate
            if rejection is None:
                regime_obj = _compute_regime(ind, atr_history, ema_spread_history, volume_history)

                # Use raw 1h data (non-enriched) for structure/sweep/OB analysis
                window = cached_1h.iloc[:len(cached_1h) - (len(enriched_df) - 1 - i) if i < len(df) else len(cached_1h)]
                # Calculate proper index into original data
                orig_idx = len(enriched_df) - len(df) + i
                orig_window = cached_1h.iloc[:orig_idx + 1]

                try:
                    structure = analyze_structure(orig_window.tail(100))
                except Exception:
                    structure = None
                try:
                    all_sweeps = detect_sweeps(orig_window.tail(100), swing_window=5)
                except Exception:
                    all_sweeps = []
                try:
                    all_obs = detect_order_blocks(orig_window.tail(100)) or []
                except Exception:
                    all_obs = []
                valid_sweeps = [s for s in all_sweeps if getattr(s, "is_valid", False)]
                valid_obs = [ob for ob in all_obs if getattr(ob, "is_valid", False)]

                result = signal_engine.evaluate(
                    ind,
                    regime=regime_obj,
                    structure=structure,
                    sweeps=valid_sweeps,
                    order_blocks=valid_obs,
                    entry_price=entry_price,
                )

                if not result.is_actionable or result.sl is None or result.tp is None:
                    rejection = "NO_SIGNAL_ENGINE"
                    funnel.engine_rejection_total += 1
                    score = result.score
                    if 0 <= score <= 7:
                        funnel.engine_rejection_scores[score] += 1
                    if score >= 4:
                        funnel.engine_rejection_has_score_gte4 += 1
                    subreason = result._rejection_reason or "ENGINE_OTHER"
                    if subreason not in ENGINE_REJECTION_REASONS:
                        subreason = "ENGINE_OTHER"
                    funnel.engine_rejection_subreasons[subreason] = funnel.engine_rejection_subreasons.get(subreason, 0) + 1

            # Step 3: Post-signal filters
            if rejection is None and result is not None and result.is_actionable:
                is_buy = result.signal == SignalType.BUY

                fvgs = []
                try:
                    _df_clean = orig_window.dropna(subset=["open", "high", "low", "close", "volume"])
                    if len(_df_clean) >= 10:
                        fvgs = detect_fvg(_df_clean, lookback=100)
                except Exception:
                    pass

                if fvgs and result.tp is not None:
                    try:
                        atr_val = float(ind.atr) if ind.atr is not None else 0.0
                        if atr_val <= 0:
                            atr_val = float(ind.close) * 0.02 if ind.close else 0.02
                        new_targets = calculate_structural_tp(
                            direction=result.signal.value, entry=entry_price,
                            sl=result.sl, sweeps=all_sweeps, order_blocks=all_obs,
                            structure=structure, fvgs=fvgs, atr=atr_val,
                            close=float(ind.close) if ind.close else 0.0,
                        )
                        if new_targets:
                            result.tp = new_targets[0].price
                    except Exception:
                        pass

                if result.sl is not None:
                    try:
                        atr_val_sl = float(ind.atr) if ind.atr is not None else 0.0
                        if atr_val_sl <= 0:
                            atr_val_sl = float(ind.close) * 0.02 if ind.close else 0.02
                        skip_structural_sl = result._sl_source == "bos"
                        if not skip_structural_sl:
                            new_sl = calculate_structural_sl(
                                direction=result.signal.value, entry=entry_price,
                                sweeps=all_sweeps, order_blocks=all_obs,
                                structure=structure, atr=atr_val_sl,
                                close=float(ind.close) if ind.close else 0.0,
                            )
                            current_dist = abs(entry_price - result.sl)
                            structural_dist = abs(entry_price - new_sl)
                            if structural_dist <= current_dist and new_sl != result.sl:
                                result.sl = new_sl
                                result._sl_source = "structural"
                    except Exception:
                        pass

                if rejection is None:
                    sl_dist_pct = abs(entry_price - result.sl) / entry_price * 100
                    min_dist = config.trading.min_sl_distance_pct
                    max_dist = config.trading.max_sl_distance_pct
                    if sl_dist_pct < min_dist:
                        rejection = "SL_DISTANCE_MIN"
                    elif sl_dist_pct > max_dist:
                        rejection = "SL_DISTANCE_MAX"

                if rejection is None:
                    risk = abs(entry_price - result.sl)
                    reward = abs(result.tp - entry_price)
                    rr = reward / risk if risk > 0 else 0
                    min_rr = config.trading.min_rr_threshold
                    if rr < min_rr:
                        rejection = "RR_GUARD"

            # Classify
            if rejection is None:
                rejection = "PASSED"
                funnel.passed_sl_sources[result._sl_source or "atr"] = \
                    funnel.passed_sl_sources.get(result._sl_source or "atr", 0) + 1
                score = result.score
                if 1 <= score <= 7:
                    funnel.passed_scores[score] = funnel.passed_scores.get(score, 0) + 1
                funnel.passed_directions[result.signal.value] = \
                    funnel.passed_directions.get(result.signal.value, 0) + 1
                regime = result._regime or "unknown"
                funnel.passed_regimes[regime] = funnel.passed_regimes.get(regime, 0) + 1

                ct = BacktestTrade(
                    symbol=symbol, timeframe=TIMEFRAME,
                    direction=result.signal.value, entry_price=entry_price,
                    entry_index=i, entry_timestamp=str(df.index[i]),
                    sl=result.sl, tp=result.tp,
                    sl_source=result._sl_source or "atr",
                    regime=result._regime or "",
                    signal_score=result.score, confidence=result.confidence,
                    reasons=list(result.reasons),
                    factor_strengths=dict(result._factor_strengths),
                    factor_present={k: v > 0 for k, v in result._factor_strengths.items()
                                    if k not in ("BUY", "SELL")},
                    verdict=result.score_verdict,
                    confidence_v2_score=result._confidence_v2.confidence_pct if result._confidence_v2 else 0.0,
                    confidence_v2_quality=result._confidence_v2.quality if result._confidence_v2 else "",
                )
                in_trade = True

            funnel.counts[rejection] = funnel.counts.get(rejection, 0) + 1

    return funnel


# ---------------------------------------------------------------------------
# Aggregation & output
# ---------------------------------------------------------------------------

def merge_funnels(funnels: list[SymbolFunnel]) -> SymbolFunnel:
    agg = SymbolFunnel(symbol="__AGGREGATE__")
    for f in funnels:
        agg.total_bars += f.total_bars
        for step in PIPELINE_STEPS:
            agg.counts[step] += f.counts.get(step, 0)
        for score in range(8):
            agg.engine_rejection_scores[score] += f.engine_rejection_scores.get(score, 0)
        agg.engine_rejection_has_score_gte4 += f.engine_rejection_has_score_gte4
        agg.engine_rejection_total += f.engine_rejection_total
        for reason in ENGINE_REJECTION_REASONS:
            agg.engine_rejection_subreasons[reason] += f.engine_rejection_subreasons.get(reason, 0)
        for src in ["atr", "bos", "structural"]:
            agg.passed_sl_sources[src] += f.passed_sl_sources.get(src, 0)
        for score in range(1, 8):
            agg.passed_scores[score] += f.passed_scores.get(score, 0)
        for d in ["BUY", "SELL"]:
            agg.passed_directions[d] += f.passed_directions.get(d, 0)
        for reg, cnt in f.passed_regimes.items():
            agg.passed_regimes[reg] = agg.passed_regimes.get(reg, 0) + cnt
    return agg


def print_funnel_table(agg: SymbolFunnel):
    total = agg.total_bars
    if total == 0:
        print("No signals to analyze.")
        return

    print(f"\n{'='*80}")
    print(f"  SIGNAL PIPELINE FUNNEL — full_new config ({CANDLES} candles)")
    print(f"{'='*80}")
    print(f"\n  Total bars evaluated: {total:,}")
    print(f"\n  {'Step':<25} {'Count':>8} {'% of total':>12} {'% pass-through':>15}")
    print(f"  {'-'*25} {'-'*8} {'-'*12} {'-'*15}")

    active_in_backtest = {"NO_SIGNAL_ENGINE", "SL_DISTANCE_MIN", "SL_DISTANCE_MAX", "RR_GUARD", "PASSED"}
    cumulative = total
    for step in PIPELINE_STEPS:
        count = agg.counts.get(step, 0)
        if step not in active_in_backtest:
            print(f"  {step:<25} {'N/A':>8} {'(not active in full_new)':>12}")
            continue
        pct_total = count / total * 100 if total > 0 else 0
        pct_prev = count / cumulative * 100 if cumulative > 0 else 0
        if step != "PASSED":
            cumulative -= count
        print(f"  {step:<25} {count:>8,} {pct_total:>11.1f}% {pct_prev:>14.1f}%")

    passed = agg.counts.get("PASSED", 0)
    killed = total - passed
    print(f"\n  Summary: {total:,} bars evaluated → {passed:,} trades ({passed/total*100:.1f}%)")
    print(f"           {killed:,} signals killed ({killed/total*100:.1f}%)")


def print_per_symbol(funnels: list[SymbolFunnel]):
    print(f"\n{'='*80}")
    print(f"  TOP-3 REJECTION REASONS BY SYMBOL")
    print(f"{'='*80}")
    print(f"\n  {'Symbol':<15} {'#1 reason':<25} {'count':>6} {'#2 reason':<25} {'count':>6} {'#3 reason':<25} {'count':>6}")
    print(f"  {'-'*15} {'-'*25} {'-'*6} {'-'*25} {'-'*6} {'-'*25} {'-'*6}")

    for f in sorted(funnels, key=lambda x: x.symbol):
        steps = [(s, c) for s, c in f.counts.items() if s != "PASSED" and c > 0]
        steps.sort(key=lambda x: -x[1])
        top3 = steps[:3]
        while len(top3) < 3:
            top3.append(("--", 0))
        print(f"  {f.symbol:<15} {top3[0][0]:<25} {top3[0][1]:>6} "
              f"{top3[1][0]:<25} {top3[1][1]:>6} "
              f"{top3[2][0]:<25} {top3[2][1]:>6}")


def print_top5_killers(agg: SymbolFunnel):
    print(f"\n{'='*80}")
    print(f"  TOP-5 SIGNAL KILLERS (absolute count)")
    print(f"{'='*80}")

    steps = [(s, c) for s, c in agg.counts.items() if s != "PASSED" and c > 0]
    steps.sort(key=lambda x: -x[1])
    total = agg.total_bars

    for i, (step, count) in enumerate(steps[:5], 1):
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {i}. {step:<25} {count:>8,} ({pct:>5.1f}%) {bar}")


def print_engine_score_distribution(agg: SymbolFunnel):
    print(f"\n{'='*80}")
    print(f"  SCORE DISTRIBUTION: NO_SIGNAL_ENGINE rejections")
    print(f"{'='*80}")
    total = agg.engine_rejection_total
    if total == 0:
        print("  No engine rejections.")
        return

    print(f"\n  Total engine rejections: {total:,}")
    print(f"  With score >= 4 (would pass min_score gate): "
          f"{agg.engine_rejection_has_score_gte4:,} "
          f"({agg.engine_rejection_has_score_gte4/total*100:.1f}%)")
    print(f"\n  {'Score':<8} {'Count':>8} {'%':>8}")
    print(f"  {'-'*8} {'-'*8} {'-'*8}")

    for score in range(8):
        count = agg.engine_rejection_scores.get(score, 0)
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {score:<8} {count:>8,} {pct:>7.1f}% {bar}")


def print_passed_metadata(agg: SymbolFunnel):
    passed = agg.counts.get("PASSED", 0)
    if passed == 0:
        return

    print(f"\n{'='*80}")
    print(f"  PASSED SIGNALS METADATA ({passed:,} trades)")
    print(f"{'='*80}")

    print(f"\n  SL Source Distribution:")
    for src in ["atr", "bos", "structural"]:
        count = agg.passed_sl_sources.get(src, 0)
        pct = count / passed * 100 if passed > 0 else 0
        print(f"    {src:<15} {count:>6} ({pct:>5.1f}%)")

    print(f"\n  Score Distribution:")
    for score in range(1, 8):
        count = agg.passed_scores.get(score, 0)
        pct = count / passed * 100 if passed > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"    Score {score:<4} {count:>6} ({pct:>5.1f}%) {bar}")

    print(f"\n  Direction:")
    for d in ["BUY", "SELL"]:
        count = agg.passed_directions.get(d, 0)
        pct = count / passed * 100 if passed > 0 else 0
        print(f"    {d:<6} {count:>6} ({pct:>5.1f}%)")

    print(f"\n  Regime:")
    for reg, count in sorted(agg.passed_regimes.items(), key=lambda x: -x[1]):
        pct = count / passed * 100 if passed > 0 else 0
        print(f"    {reg:<15} {count:>6} ({pct:>5.1f}%)")


# ---------------------------------------------------------------------------
# Engine breakdown (second-level classification of NO_SIGNAL_ENGINE)
# ---------------------------------------------------------------------------

def print_engine_breakdown(agg: SymbolFunnel):
    total = agg.total_bars
    engine_total = agg.engine_rejection_total
    if engine_total == 0:
        print("No engine rejections to break down.")
        return

    print(f"\n{'='*80}")
    print(f"  ENGINE REJECTION BREAKDOWN — NO_SIGNAL_ENGINE sub-reasons")
    print(f"{'='*80}")
    print(f"\n  Total NO_SIGNAL_ENGINE: {engine_total:,}")
    print(f"\n  {'Reason':<30} {'Count':>8} {'% of total':>12} {'% of NO_SIGNAL_ENGINE':>24}")
    print(f"  {'-'*30} {'-'*8} {'-'*12} {'-'*24}")

    sorted_reasons = sorted(
        [(r, c) for r, c in agg.engine_rejection_subreasons.items() if c > 0],
        key=lambda x: -x[1],
    )
    for reason, count in sorted_reasons:
        pct_total = count / total * 100 if total > 0 else 0
        pct_engine = count / engine_total * 100 if engine_total > 0 else 0
        print(f"  {reason:<30} {count:>8,} {pct_total:>11.1f}% {pct_engine:>23.1f}%")

    sum_reasons = sum(c for _, c in sorted_reasons)
    print(f"  {'─'*30} {'─'*8} {'─'*12} {'─'*24}")
    print(f"  {'TOTAL':<30} {sum_reasons:>8,} {sum_reasons/total*100:>11.1f}% {100.0:>23.1f}%")


def print_per_symbol_engine(funnels: list[SymbolFunnel]):
    print(f"\n{'='*80}")
    print(f"  TOP-2 ENGINE INTERNAL REJECTION REASONS BY SYMBOL")
    print(f"{'='*80}")
    print(f"\n  {'Symbol':<15} {'#1 reason':<30} {'count':>6} {'#2 reason':<30} {'count':>6}")
    print(f"  {'-'*15} {'-'*30} {'-'*6} {'-'*30} {'-'*6}")

    for f in sorted(funnels, key=lambda x: x.symbol):
        reasons = [(r, c) for r, c in f.engine_rejection_subreasons.items() if c > 0]
        reasons.sort(key=lambda x: -x[1])
        top2 = reasons[:2]
        while len(top2) < 2:
            top2.append(("--", 0))
        print(f"  {f.symbol:<15} {top2[0][0]:<30} {top2[0][1]:>6} "
              f"{top2[1][0]:<30} {top2[1][1]:>6}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    symbols = SYMBOLS
    if "--symbols" in sys.argv:
        idx = sys.argv.index("--symbols")
        if idx + 1 < len(sys.argv):
            symbols = sys.argv[idx + 1].split(",")

    print(f"{'='*70}", flush=True)
    print(f"  SIGNAL PIPELINE FUNNEL ANALYSIS", flush=True)
    print(f"  Config: full_new | Symbols: {len(symbols)} | Candles: {CANDLES}", flush=True)
    print(f"{'='*70}", flush=True)

    t0 = time.time()
    funnels: list[SymbolFunnel] = []

    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...", end=" ", flush=True)
        t_s = time.time()
        try:
            f = await run_funnel_one(symbol, CANDLES)
            elapsed = time.time() - t_s
            passed = f.counts.get("PASSED", 0)
            engine_kills = f.counts.get("NO_SIGNAL_ENGINE", 0)
            print(f"bars={f.total_bars:,} engine_kill={engine_kills:,} passed={passed} ({elapsed:.1f}s)", flush=True)
            funnels.append(f)
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            import traceback
            traceback.print_exc()

        await asyncio.sleep(0.1)

    elapsed_total = time.time() - t0
    print(f"\n  Completed in {elapsed_total:.1f}s", flush=True)

    if not funnels:
        print("No data to analyze.")
        return

    agg = merge_funnels(funnels)

    print_funnel_table(agg)
    print_per_symbol(funnels)
    print_top5_killers(agg)
    print_engine_score_distribution(agg)
    print_passed_metadata(agg)
    print_engine_breakdown(agg)
    print_per_symbol_engine(funnels)

    raw_data = {
        "config": "full_new",
        "symbols": [f.symbol for f in funnels],
        "timeframe": TIMEFRAME,
        "candles": CANDLES,
        "total_bars": agg.total_bars,
        "aggregate": {
            "counts": agg.counts,
            "engine_rejection_scores": agg.engine_rejection_scores,
            "engine_rejection_has_score_gte4": agg.engine_rejection_has_score_gte4,
            "engine_rejection_total": agg.engine_rejection_total,
            "engine_rejection_subreasons": agg.engine_rejection_subreasons,
            "passed_sl_sources": agg.passed_sl_sources,
            "passed_scores": agg.passed_scores,
            "passed_directions": agg.passed_directions,
            "passed_regimes": agg.passed_regimes,
        },
        "per_symbol": [],
    }
    for f in funnels:
        raw_data["per_symbol"].append({
            "symbol": f.symbol,
            "total_bars": f.total_bars,
            "counts": f.counts,
            "engine_rejection_scores": f.engine_rejection_scores,
            "engine_rejection_has_score_gte4": f.engine_rejection_has_score_gte4,
            "engine_rejection_total": f.engine_rejection_total,
            "engine_rejection_subreasons": f.engine_rejection_subreasons,
            "passed_sl_sources": f.passed_sl_sources,
            "passed_scores": f.passed_scores,
            "passed_directions": f.passed_directions,
            "passed_regimes": f.passed_regimes,
        })

    output_path = RESULTS_DIR / "funnel_full_new.json"
    with open(output_path, "w", encoding="utf-8") as fp:
        json.dump(raw_data, fp, indent=2, ensure_ascii=False)
    print(f"\n  Raw data saved to: {output_path}", flush=True)

    breakdown_data = {
        "config": "full_new",
        "timeframe": TIMEFRAME,
        "candles": CANDLES,
        "total_bars": agg.total_bars,
        "engine_rejection_total": agg.engine_rejection_total,
        "aggregate_subreasons": agg.engine_rejection_subreasons,
        "per_symbol": [],
    }
    for f in funnels:
        top2 = sorted(
            [(r, c) for r, c in f.engine_rejection_subreasons.items() if c > 0],
            key=lambda x: -x[1],
        )[:2]
        breakdown_data["per_symbol"].append({
            "symbol": f.symbol,
            "engine_rejection_total": f.engine_rejection_total,
            "subreasons": f.engine_rejection_subreasons,
            "top2": [{"reason": r, "count": c} for r, c in top2],
        })

    breakdown_path = RESULTS_DIR / "funnel_engine_breakdown.json"
    with open(breakdown_path, "w", encoding="utf-8") as fp:
        json.dump(breakdown_data, fp, indent=2, ensure_ascii=False)
    print(f"  Engine breakdown saved to: {breakdown_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
