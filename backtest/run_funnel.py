"""
backtest/run_funnel.py — Instrumented funnel analysis for full_new config.

Runs BacktestEngine with instrument=True on all 20 symbols (3900 candles),
collects per-signal stop-point data, and produces the funnel report.

Usage:
    python -m backtest.run_funnel
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

_saved_stdout = sys.stdout
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger as _loguru_logger
_loguru_logger.disable("strategy.signal_engine")
_loguru_logger.disable("indicators.engine")
import logging
logging.getLogger("strategy.signal_engine").setLevel(logging.WARNING)
logging.getLogger("indicators.engine").setLevel(logging.WARNING)

from backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    FunnelData,
    FUNNEL_STEPS,
    BACKTEST_ACTIVE,
    _compute_regime,
    get_preset_config,
)
from config.settings import config
from indicators.engine import IndicatorEngine, IndicatorValues
from strategy.signal_engine import signal_engine, SignalType
from risk.dynamic_risk import calculate_structural_sl, calculate_structural_tp
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from market_structure.structure import analyze_structure

sys.stdout = _saved_stdout

# ── Config ────────────────────────────────────────────────────────────────
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "LINK/USDT",
]

FULL_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
    "AVAX/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT", "UNI/USDT",
    "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT",
    "INJ/USDT", "WIF/USDT", "FLOKI/USDT", "FIL/USDT", "GRT/USDT",
]

TIMEFRAME = "1h"
CANDLES = 3900
PRESET = "full_new"

FULL_NEW_FLAGS: dict[str, bool] = {
    "enable_unified_entry": True,
    "enable_confirm_tf_gate": False,
    "enable_structural_sl": True,
    "enable_sl_distance_guard": True,
    "enable_rr_filter": True,
    "enable_news_filter": True,
    "enable_stop_hunt_buffer": False,
}

REPORTS_DIR = Path(__file__).parent.parent / "reports" / "funnel"
FUNNEL_OUTPUT_PATH = REPORTS_DIR / "funnel_full_new1.json"


# ── Aggregation helpers ──────────────────────────────────────────────────

def aggregate_funnel(all_data: list[FunnelData]) -> dict:
    """Aggregate per-symbol FunnelData into a single summary dict."""
    steps: dict[str, int] = {s: 0 for s in FUNNEL_STEPS}
    total_signals = 0
    total_passed = 0
    signal_engine_scores: dict[str, int] = {}

    for fd in all_data:
        for step, count in fd.steps.items():
            if step in steps:
                steps[step] += count
        total_signals += fd.total_signals_processed
        total_passed += fd.steps.get("PASSED", 0)
        # collect score distribution
        for key, val in fd.steps.items():
            if key.startswith("SCORE_"):
                signal_engine_scores[key] = signal_engine_scores.get(key, 0) + val

    total_stopped = total_signals - total_passed
    return {
        "steps": steps,
        "total_signals": total_signals,
        "total_passed": total_passed,
        "total_stopped": total_stopped,
        "signal_engine_score_distribution": signal_engine_scores,
    }


def build_funnel_table(agg: dict) -> list[tuple[str, int, float, float]]:
    """Build table rows from aggregated funnel data."""
    total = agg["total_signals"]
    rows = []
    cumulative = total
    for step in FUNNEL_STEPS:
        count = agg["steps"].get(step, 0)
        pct_total = count / total * 100 if total > 0 else 0
        pct_prev = count / cumulative * 100 if cumulative > 0 else 0
        if step != "PASSED":
            cumulative -= count
        rows.append((step, count, pct_total, pct_prev))
    return rows


def print_funnel_table(rows: list[tuple[str, int, float, float]]) -> None:
    """Print a funnel table from rows."""
    print(f"\n  {'Step':<25} {'Count':>8} {'% of total':>12} {'% pass-through':>15}")
    print(f"  {'-'*25} {'-'*8} {'-'*12} {'-'*15}")
    for step, count, pct_total, pct_prev in rows:
        print(f"  {step:<25} {count:>8,} {pct_total:>11.1f}% {pct_prev:>14.1f}%")


def save_funnel_json(all_data: list[FunnelData], agg: dict, path: Path) -> None:
    """Save raw funnel data and aggregation to JSON."""
    payload = {
        "aggregated": {
            "steps": agg["steps"],
            "total_signals": agg["total_signals"],
            "total_passed": agg["total_passed"],
            "total_stopped": agg["total_stopped"],
        },
        "symbols": [
            {"symbol": fd.symbol, "steps": fd.steps, "total_signals_processed": fd.total_signals_processed}
            for fd in all_data
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


# ── Per-symbol analysis ──────────────────────────────────────────────────

def top_n_stoppers(fd: FunnelData, n: int = 3) -> list[tuple[str, int]]:
    """Return top-N stopping steps (excluding PASSED and N/A steps)."""
    candidates = [
        (step, count)
        for step, count in fd.steps.items()
        if step != "PASSED" and BACKTEST_ACTIVE.get(step, False) and count > 0
    ]
    return sorted(candidates, key=lambda x: -x[1])[:n]


# ── Fast instrumented runner (precomputes indicators once per symbol) ────

def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    f = float(val)
    return default if math.isnan(f) or math.isinf(f) else f


def _build_ind_values(row, prev, symbol: str, tf: str):
    """Build IndicatorValues from precomputed indicator columns."""
    return IndicatorValues(
        symbol=symbol, timeframe=tf,
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
        macd_signal=_safe_float(row.get("macd_signal")),
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


def _row_has_valid_indicators(row) -> bool:
    """Check if row has all required indicator columns with non-NaN values."""
    for col in ("ema_fast", "ema_slow", "rsi", "adx", "atr"):
        if col not in row.index or pd.isna(row.get(col)):
            return False
    return True


async def run_one_fast(symbol: str, *, cached_1h, cached_15m) -> FunnelData:
    """Fast instrumented run with precomputed indicators."""
    bt_config = BacktestConfig(**FULL_NEW_FLAGS)
    config.trading.candles_limit = CANDLES
    tf = TIMEFRAME
    confirm_tf = config.trading.confirm_timeframe

    if cached_1h is None:
        raise ValueError(f"No cached 1h data for {symbol}")

    df = cached_1h.iloc[-(CANDLES + 60):].copy() if len(cached_1h) > CANDLES + 60 else cached_1h.copy()

    # Precompute indicators on full df (adds columns in-place)
    indicator_engine = IndicatorEngine()
    indicator_engine.calculate(df, symbol, tf)

    # Confirm TF
    confirm_df = None
    if config.trading.confirm_tf_enabled and confirm_tf != tf and cached_15m is not None:
        confirm_df = cached_15m.copy()
        indicator_engine.calculate(confirm_df, symbol, confirm_tf)

    warmup = 80
    _funnel_counts: dict[str, int] = {s: 0 for s in FUNNEL_STEPS}
    _funnel_engine_scores: dict[int, int] = {}
    _funnel_passed_scores: dict[int, int] = {}
    _funnel_passed_sl_sources: dict[str, int] = {}
    _funnel_sl_shifted = 0
    signals_count = 0

    # Track last 2 clean row indices
    clean_indices: list[int] = []

    in_trade = False
    # We don't track trade exits for funnel — only signal stops matter

    for i in range(warmup, len(df)):
        row = df.iloc[i]

        # Track indicator validity incrementally
        if _row_has_valid_indicators(row):
            clean_indices.append(i)
            if len(clean_indices) > 2:
                clean_indices.pop(0)

        if len(clean_indices) < 2:
            continue

        last_idx = clean_indices[-1]
        prev_idx = clean_indices[-2]

        # Only process on bars where the current bar IS the last clean bar
        # to match engine behavior (it evaluates on last clean row of window)
        if last_idx != i:
            # Indicators are stale — skip evaluation (matches engine behavior)
            if not in_trade:
                # Still count as a signal attempt
                pass
            continue

        # Build indicator values from precomputed columns
        ind = _build_ind_values(df.iloc[last_idx], df.iloc[prev_idx], symbol, tf)

        # Track histories
        atr_val = float(ind.atr)
        ema_spread_val = (
            abs(float(ind.ema_fast) - float(ind.ema_slow)) / float(ind.ema_slow) * 100
            if float(ind.ema_slow) > 0 else 0.0
        )

        # --- Exit check (simplified — only to manage in_trade flag) ---
        if in_trade:
            high, low = float(ind.high), float(ind.low)
            if _current_trade_dir == "BUY":
                if low <= _current_sl or high >= _current_tp:
                    in_trade = False
            else:
                if high >= _current_sl or low <= _current_tp:
                    in_trade = False
            if not in_trade:
                continue  # skip signal check on exit bar

        if in_trade:
            continue

        signals_count += 1
        rejection = None

        # === Pipeline: CONFIRM TF GATE ===
        entry_price = float(ind.close)
        confirm_available = (
            config.trading.confirm_tf_enabled
            and confirm_df is not None
            and confirm_tf != tf
        )
        if confirm_available:
            primary_ts = df.index[i]
            try:
                confirm_idx = confirm_df.index.get_indexer([primary_ts], method="nearest")[0]
                if 0 <= confirm_idx < len(confirm_df):
                    c_row = confirm_df.iloc[confirm_idx]
                    if _row_has_valid_indicators(c_row):
                        c_prev = confirm_df.iloc[max(0, confirm_idx - 1)]
                        c_ind = _build_ind_values(c_row, c_prev, symbol, confirm_tf)
                        direction_str = "buy" if ind.ema_fast > ind.ema_slow else "sell"
                        confirm_ok = signal_engine.evaluate_confirm(c_ind, direction_str)
                        if bt_config.enable_unified_entry:
                            entry_price = float(c_ind.close)
                        if not confirm_ok and bt_config.enable_confirm_tf_gate:
                            rejection = "CONFIRM_TF_REJECT"
            except Exception:
                pass

        # === Pipeline: SIGNAL ENGINE ===
        if rejection is None:
            regime_obj = _compute_regime(ind, [], [], [])
            try:
                structure = analyze_structure(df.iloc[max(0, i - 100):i + 1])
            except Exception:
                structure = None
            try:
                all_sweeps = detect_sweeps(df.iloc[max(0, i - 100):i + 1], swing_window=5)
            except Exception:
                all_sweeps = []
            try:
                all_obs = detect_order_blocks(df.iloc[max(0, i - 100):i + 1]) or []
            except Exception:
                all_obs = []
            valid_sweeps = [s for s in all_sweeps if getattr(s, "is_valid", False)]
            valid_obs = [ob for ob in all_obs if getattr(ob, "is_valid", False)]

            result = signal_engine.evaluate(
                ind, regime=regime_obj, structure=structure,
                sweeps=valid_sweeps, order_blocks=valid_obs,
                entry_price=entry_price,
            )

            if not (result.is_actionable and result.sl is not None and result.tp is not None):
                rejection = "NO_SIGNAL_ENGINE"
                score = result.score
                _funnel_engine_scores[score] = _funnel_engine_scores.get(score, 0) + 1

        # === Pipeline: POST-SIGNAL FILTERS ===
        if rejection is None and result.is_actionable:
            is_buy = result.signal == SignalType.BUY

            # FVG + TP recalc
            fvgs = []
            try:
                _df_clean = df.iloc[max(0, i - 100):i + 1].dropna(subset=["open", "high", "low", "close", "volume"])
                if len(_df_clean) >= 10:
                    fvgs = detect_fvg(_df_clean, lookback=100)
            except Exception:
                pass

            if fvgs and result.tp is not None:
                try:
                    atr_val_sl = float(ind.atr) if ind.atr is not None else 0.0
                    if atr_val_sl <= 0:
                        atr_val_sl = float(ind.close) * 0.02 if ind.close else 0.02
                    new_targets = calculate_structural_tp(
                        direction=result.signal.value, entry=entry_price, sl=result.sl,
                        sweeps=all_sweeps, order_blocks=all_obs, structure=structure,
                        fvgs=fvgs, atr=atr_val_sl,
                        close=float(ind.close) if ind.close else 0.0,
                    )
                    if new_targets:
                        result.tp = new_targets[0].price
                except Exception:
                    pass

            # Structural SL
            if result.sl is not None and bt_config.enable_structural_sl:
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

            # SL distance guard
            if bt_config.enable_sl_distance_guard:
                sl_dist_pct = abs(entry_price - result.sl) / entry_price * 100
                min_dist = config.trading.min_sl_distance_pct
                max_dist = config.trading.max_sl_distance_pct
                if sl_dist_pct < min_dist:
                    if is_buy:
                        result.sl = round(entry_price * (1 - min_dist / 100), 8)
                    else:
                        result.sl = round(entry_price * (1 + min_dist / 100), 8)
                    _funnel_sl_shifted += 1
                elif sl_dist_pct > max_dist:
                    rejection = "SL_DISTANCE_MAX"

            # RR filter
            if rejection is None and bt_config.enable_rr_filter:
                risk = abs(entry_price - result.sl)
                reward = abs(result.tp - entry_price)
                rr = reward / risk if risk > 0 else 0
                min_rr = config.trading.min_rr_threshold
                if rr < min_rr:
                    rejection = "RR_GUARD"

        # === Classify ===
        if rejection is None:
            rejection = "PASSED"
            score = result.score
            _funnel_passed_scores[score] = _funnel_passed_scores.get(score, 0) + 1
            sl_src = result._sl_source or "atr"
            _funnel_passed_sl_sources[sl_src] = _funnel_passed_sl_sources.get(sl_src, 0) + 1

            # Mark trade as active (only for exit tracking for next bars)
            in_trade = True
            _current_sl = result.sl
            _current_tp = result.tp
            _current_trade_dir = result.signal.value

        _funnel_counts[rejection] = _funnel_counts.get(rejection, 0) + 1

    return FunnelData(
        symbol=symbol,
        steps=_funnel_counts,
        total_signals_processed=signals_count,
        signal_engine_score_distribution=_funnel_engine_scores,
        passed_score_distribution=_funnel_passed_scores,
        passed_sl_sources=_funnel_passed_sl_sources,
        sl_shifted=_funnel_sl_shifted,
    )


# Initialize module-level trade state (used by run_one_fast)
_current_trade_dir = None
_current_sl = None
_current_tp = None


async def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"  FUNNEL ANALYSIS: {PRESET}")
    print(f"  Symbols: {len(SYMBOLS)} | Timeframe: {TIMEFRAME} | Candles: {CANDLES}")
    print(f"  Flags: {json.dumps(FULL_NEW_FLAGS, indent=2)}")
    print("=" * 70)

    from backtest.cache_ohlcv import load_cached, CONFIRM_TIMEFRAME, CONFIRM_CANDLES

    # Pre-load all cached data (no exchange connection needed)
    cached_data = {}
    for symbol in SYMBOLS:
        cached_data[f"{symbol}_1h"] = load_cached(symbol, TIMEFRAME, CANDLES)
        cached_data[f"{symbol}_15m"] = load_cached(symbol, CONFIRM_TIMEFRAME, CONFIRM_CANDLES)

    all_funnel_data: list[FunnelData] = []
    total_runs = len(SYMBOLS)

    for idx, symbol in enumerate(SYMBOLS, 1):
        print(f"  [{idx}/{total_runs}] {symbol}...", end=" ", flush=True)
        t0 = time.time()
        try:
            cached_1h = cached_data.get(f"{symbol}_1h")
            cached_15m = cached_data.get(f"{symbol}_15m")
            fdata = await run_one_fast(symbol, cached_1h=cached_1h, cached_15m=cached_15m)
            elapsed = time.time() - t0
            n_passed = fdata.steps.get("PASSED", 0)
            n_signals = fdata.total_signals_processed
            print(f"signals={n_signals} passed={n_passed} ({elapsed:.1f}s)")
            all_funnel_data.append(fdata)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"ERROR: {e} ({elapsed:.1f}s)")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(0.3)

    # ── Aggregate ────────────────────────────────────────────────────────
    agg = aggregate_funnel(all_funnel_data)
    total_signals = agg["total_signals"]
    total_passed = agg["total_passed"]
    total_stopped = agg["total_stopped"]

    print(f"\n{'=' * 70}")
    print(f"  FUNNEL: {PRESET} — {len(all_funnel_data)} symbols")
    print(f"  Total potential signals: {total_signals}")
    print(f"  Total stopped (all reasons): {total_stopped}")
    print(f"  Total PASSED (trades): {total_passed}")
    print(f"  Pass rate: {total_passed / total_signals * 100:.2f}%" if total_signals > 0 else "  N/A")
    print(f"{'=' * 70}\n")

    # ── Save raw data FIRST (before any print errors) ────────────────────
    save_funnel_json(all_funnel_data, agg, FUNNEL_OUTPUT_PATH)
    print(f"\n  Raw data saved to: {FUNNEL_OUTPUT_PATH}")

    # ── 1. Aggregated funnel table ───────────────────────────────────────
    print("=" * 70)
    print("  1. FUNNEL TABLE (aggregated across all symbols)")
    print("=" * 70)
    rows = build_funnel_table(agg)
    print_funnel_table(rows)

    # ── 2. Per-symbol top-3 stoppers ────────────────────────────────────
    print("=" * 70)
    print("  2. PER-SYMBOL TOP-3 STOPPING REASONS")
    print("=" * 70)
    for fd in all_funnel_data:
        top = top_n_stoppers(fd, 3)
        total_stopped_sym = sum(fd.steps[s] for s in FUNNEL_STEPS if s != "PASSED" and BACKTEST_ACTIVE.get(s, False))
        top_str = ", ".join(f"{s}={c}" for s, c in top)
        passed_count = fd.steps.get("PASSED", 0)
        print(f"  {fd.symbol:12s} stopped={total_stopped_sym:5d} passed={passed_count:4d}  top: {top_str}")

    # ── 3. Top-5 killers ────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  3. TOP-5 KILLERS (absolute counts, all symbols)")
    print("=" * 70)
    killer_counts: list[tuple[str, int]] = [
        (step, agg["steps"][step])
        for step in FUNNEL_STEPS
        if step != "PASSED" and BACKTEST_ACTIVE.get(step, False) and agg["steps"][step] > 0
    ]
    killer_counts.sort(key=lambda x: -x[1])
    for i, (step, count) in enumerate(killer_counts[:5], 1):
        pct = count / total_signals * 100 if total_signals > 0 else 0
        print(f"  #{i} {step:25s} {count:>8d} ({pct:>5.2f}% of all signals)")

    # ── 4. Score distribution for NO_SIGNAL_ENGINE ──────────────────────
    print("\n" + "=" * 70)
    print("  4. SIGNAL_ENGINE SCORE DISTRIBUTION (stopped at NO_SIGNAL_ENGINE)")
    print("=" * 70)
    se_dist = agg["signal_engine_score_distribution"]
    total_no_signal = sum(se_dist.values()) if se_dist else 0
    score_ge4 = sum(int(k) for k, v in se_dist.items() if int(k) >= 4) if se_dist else 0
    count_ge4 = sum(v for k, v in se_dist.items() if int(k) >= 4) if se_dist else 0

    if se_dist:
        for score_str, count in se_dist.items():
            pct = count / total_no_signal * 100
            bar = "█" * max(1, int(pct / 2))
            print(f"  score={score_str:>2s}  {count:>6d} ({pct:>5.1f}%) {bar}")
        print(f"  ──")
        print(f"  score>=4  {count_ge4:>6d} ({count_ge4 / total_no_signal * 100:.1f}% of NO_SIGNAL)")
    else:
        print("  (no data)")

    # ── 5. PASSED distribution by score and sl_source ──────────────────
    print("\n" + "=" * 70)
    print("  5. PASSED SIGNALS — DISTRIBUTION BY score AND sl_source")
    print("=" * 70)
    print(f"\n  Passed score distribution:")
    ps_dist = agg["passed_score_distribution"]
    if ps_dist:
        for score_str, count in ps_dist.items():
            pct = count / total_passed * 100
            bar = "█" * max(1, int(pct / 3))
            print(f"    score={score_str:>2s}  {count:>5d} ({pct:>5.1f}%) {bar}")
    else:
        print("    (no data)")

    print(f"\n  Passed sl_source distribution:")
    ps_src = agg["passed_sl_sources"]
    if ps_src:
        for src, count in ps_src.items():
            pct = count / total_passed * 100
            print(f"    {src:15s}  {count:>5d} ({pct:>5.1f}%)")
    else:
        print("    (no data)")

    # ── Summary line ────────────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  SUMMARY: {total_signals} signals → {total_stopped} stopped → {total_passed} trades "
          f"({total_passed / total_signals * 100:.2f}% pass rate)" if total_signals > 0 else "")
    print(f"{'─' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
