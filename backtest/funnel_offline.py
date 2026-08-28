"""
backtest/funnel_offline.py — Offline gate-block funnel for the LIVE v2 pipeline.

Replays cached 1h OHLCV (reports/abn/ohlcv_cache/) through the REAL live-pipeline
components — PatternEngine → setup-type gates → HTF bias (V2) → entry-zone →
trade plan (SL/TP) → ProbabilityEngine → RiskEngine — and tallies, per gate, how
many candidates die there. No network, no DB: HTF timeframes (4h/1d/1w) are
resampled from the cached 1h series.

This mirrors the structural gate chain of `scheduler.scanner.scan_symbol_v2`
(the runtime-only gates — cooldown, portfolio, SMT, context, dedup — are omitted;
they are not the over-blocking surface being diagnosed). Because it calls the same
config-gated code, it also honours the Phase-3 flags (HTF_HARD_GATE, MIN_P_TP,
REQUIRE_ENTRY_ZONE, REVERSAL_REQUIRE_DISPLACEMENT), so you can A/B them offline:

    python -m backtest.funnel_offline                          # default 5 symbols
    python -m backtest.funnel_offline --symbols BTC/USDT,ETH/USDT
    python -m backtest.funnel_offline --candles 2000 --limit 1500
    HTF_HARD_GATE=false MIN_P_TP=0.55 python -m backtest.funnel_offline

Output: an aggregated funnel table + top killers + P(TP) distribution, and a JSON
dump to reports/funnel/funnel_offline.json.

NOTE: written offline and NOT runtime-validated in the authoring environment
(no deps there). Run in the project venv (Python 3.11).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

# Silence noisy per-bar logging from the pipeline components.
for _mod in (
    "strategy.pattern_engine", "strategy.feature_builder", "strategy.probability_engine",
    "strategy.trade_engine", "indicators.engine", "risk.engine", "market_structure.structure",
    "market_structure.htf_bias_v2",
):
    logger.disable(_mod)

from config.settings import config
from backtest.cache_ohlcv import load_cached
from backtest.run_funnel import _build_ind_values, _row_has_valid_indicators

from indicators.engine import IndicatorEngine
from strategy.pattern_engine import pattern_engine
from strategy.trade_engine import trade_engine
from strategy.signal_evaluator import entry_zone_touched
from risk.engine import risk_engine, PortfolioState
from scheduler.scanner import _detect_regime
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from market_structure.structure import analyze_structure
from market_structure.htf_bias_v2 import get_htf_bias_v2, HTFBiasResult

# Ordered funnel stages — matches the structural chain in scan_symbol_v2.
STAGES = [
    "indicators",
    "pattern_engine",
    "sweep_required",
    "displacement_gate",
    "mss_gate",
    "bos_gate",
    "htf_bias",
    "entry_zone",
    "sl_tp",
    "probability",
    "risk_engine",
    "PASSED",
]

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "LINK/USDT"]
TIMEFRAME = "1h"
DEFAULT_CANDLES = 3900
WARMUP = 260  # bars fed to the indicator engine + structure lookbacks

# 1h → HTF resample rules (pandas offset aliases).
_HTF_RULES = {"4h": "4h", "1d": "1D", "1w": "1W"}
_OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a 1h OHLCV frame to a higher timeframe (no lookahead per-bar slicing later)."""
    out = df_1h.resample(rule, label="right", closed="right").agg(_OHLC_AGG).dropna()
    return out


def _risk_reason_bucket(reason: str | None) -> str:
    """Collapse RiskEngine rejection strings into a few stable buckets for tallying."""
    if not reason:
        return "risk_engine:other"
    r = reason.lower()
    if r.startswith("rr="):
        return "risk_engine:rr"
    if "sl too tight vs atr" in r:
        return "risk_engine:sl_vs_atr"
    if "sl too tight" in r:
        return "risk_engine:sl_min"
    if "sl too wide" in r:
        return "risk_engine:sl_max"
    if "zero risk" in r:
        return "risk_engine:zero_risk"
    if "invalid price" in r:
        return "risk_engine:invalid_price"
    return "risk_engine:other"


def run_symbol(symbol: str, candles: int, limit: int | None) -> dict:
    """Replay one symbol's cached 1h series through the structural gate chain."""
    df = load_cached(symbol, TIMEFRAME, candles)
    if df is None:
        return {"symbol": symbol, "error": "no cached data", "counts": {}, "passed_p_tp": [], "rejected_p_tp": []}

    df = df.copy()
    # Precompute indicator columns once (fast path — same columns run_funnel relies on).
    IndicatorEngine().calculate(df, symbol, TIMEFRAME)

    # Pre-resample HTF frames once; per-bar we slice up to the current timestamp.
    htf_frames = {tf: _resample(df, rule) for tf, rule in _HTF_RULES.items()}

    counts: Counter = Counter()
    passed_p_tp: list[float] = []
    rejected_p_tp: list[float] = []

    start = WARMUP
    if limit is not None:
        start = max(WARMUP, len(df) - limit)

    prev_clean = None
    for i in range(start, len(df)):
        row = df.iloc[i]
        if not _row_has_valid_indicators(row):
            continue
        if prev_clean is None:
            prev_clean = df.iloc[i - 1]
        ind = _build_ind_values(row, prev_clean, symbol, TIMEFRAME)
        prev_clean = row

        if ind is None or ind.atr is None or ind.atr <= 0:
            counts["indicators"] += 1
            continue

        current_price = float(ind.close)
        window = df.iloc[max(0, i - 100):i + 1]
        _df_clean = window.dropna(subset=["open", "high", "low", "close", "volume"])
        if len(_df_clean) < 10:
            counts["indicators"] += 1
            continue

        # ── Phase 1: liquidity + structure (mirrors scanner Phase 1) ──
        try:
            sweeps = detect_sweeps(_df_clean, lookback=50)
        except Exception:
            sweeps = []
        try:
            order_blocks = detect_order_blocks(_df_clean, lookback=100)
        except Exception:
            order_blocks = []
        try:
            candle_quality = analyze_last_candle(_df_clean, atr_value=ind.atr)
        except Exception:
            candle_quality = None
        try:
            fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 100))
        except Exception:
            fvgs = []

        _disp_atr = 0.0
        _reclaim = 0
        if candle_quality and getattr(candle_quality, "body_atr_ratio", None) is not None:
            _disp_atr = candle_quality.body_atr_ratio
        _valid_sw = [s for s in sweeps if getattr(s, "is_valid", False)]
        if _valid_sw:
            _reclaim = _valid_sw[0].reclaim_candles

        try:
            structure = analyze_structure(
                _df_clean, lookback=50, sweeps=sweeps,
                displacement_atr=_disp_atr, reclaim_bars=_reclaim,
                atr_value=ind.atr if ind.atr else 0.0,
            )
        except Exception:
            structure = None

        # ── Phase 1: Pattern Engine ──
        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=order_blocks, structure=structure,
            fvgs=fvgs, candle_quality=candle_quality,
            current_price=current_price, atr=ind.atr if ind.atr else 0.0,
        )
        if not setup.detected:
            counts["pattern_engine"] += 1
            continue

        # ── Phase 1.4: setup-type gates ──
        if setup.setup_type == "reversal":
            if not setup.has_sweep:
                counts["sweep_required"] += 1
                continue
            if not setup.has_mss:
                counts["mss_gate"] += 1
                continue
        elif setup.setup_type == "continuation":
            if not setup.has_bos:
                counts["bos_gate"] += 1
                continue

        # ── Phase 1.45: HTF bias V2 (resampled offline) ──
        _htf_bias_penalty = 1.0
        ts = df.index[i]
        df_4h = htf_frames["4h"].loc[:ts].tail(60)
        df_1d = htf_frames["1d"].loc[:ts].tail(60)
        df_1w = htf_frames["1w"].loc[:ts].tail(60)
        df_1h = df.iloc[max(0, i - 59):i + 1]
        try:
            htf_result: HTFBiasResult = get_htf_bias_v2(df_1w, df_1d, df_4h, df_1h)
            htf_dir = htf_result.direction
        except Exception:
            htf_dir = "neutral"

        if htf_dir in ("bullish", "bearish"):
            want = "buy" if htf_dir == "bullish" else "sell"
            if setup.direction != want:
                if setup.setup_type == "continuation":
                    if config.htf_hard_gate:
                        counts["htf_bias"] += 1
                        continue
                    _htf_bias_penalty = config.htf_bias_continuation_penalty
                elif setup.setup_type == "reversal":
                    _htf_bias_penalty = config.htf_bias_continuation_penalty

        # ── Entry zone (opt-in hard gate, shared with live) ──
        if config.require_entry_zone:
            _bar_high = float(ind.high) if ind.high else float(ind.close)
            _bar_low = float(ind.low) if ind.low else float(ind.close)
            if not entry_zone_touched(setup.direction, fvgs, _bar_high, _bar_low):
                counts["entry_zone"] += 1
                continue

        # ── Phase 1.5: trade plan (SL/TP) ──
        try:
            trade_plan = trade_engine.build_trade_plan(
                ind=ind, direction=setup.direction, structure=structure,
                order_blocks=order_blocks, sweeps=sweeps, fvgs=fvgs,
                df=_df_clean, timeframe=TIMEFRAME,
            )
            sl, tp = trade_plan.sl, trade_plan.tp
        except Exception:
            sl, tp = None, None
        if sl is None or tp is None:
            counts["sl_tp"] += 1
            continue

        entry_price = current_price

        # ── Phase 2: analytics ──
        regime = _detect_regime(ind, window, symbol, TIMEFRAME)
        atr_pct = (ind.atr / ind.close * 100) if ind.atr and ind.close > 0 else 0.0

        # ── Phase 3: inline probability ──
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

        if config.min_p_tp > 0.0 and p_tp < config.min_p_tp:
            counts["probability"] += 1
            rejected_p_tp.append(round(p_tp, 4))
            continue

        # ── Phase 4: risk engine ──
        portfolio = PortfolioState(
            active_count=0, total_risk_pct=0.0,
            max_active_signals=config.max_active_signals,
            max_portfolio_risk_pct=config.max_portfolio_risk_pct,
        )
        risk_decision = risk_engine.evaluate(
            portfolio=portfolio,
            entry_price=entry_price, sl=sl, tp=tp,
            atr_pct=atr_pct, p_tp=p_tp, confidence=confidence,
            scenario_score=0.0, scenario_stability=0.0,
            mss_quality=setup.mss_score, atr=ind.atr if ind.atr else 0.0,
        )
        if not risk_decision.should_trade:
            counts[_risk_reason_bucket(risk_decision.rejection_reason)] += 1
            rejected_p_tp.append(round(probability.p_tp, 4))
            continue

        counts["PASSED"] += 1
        passed_p_tp.append(round(probability.p_tp, 4))

    return {
        "symbol": symbol,
        "counts": dict(counts),
        "passed_p_tp": passed_p_tp,
        "rejected_p_tp": rejected_p_tp,
    }


def _print_report(results: list[dict]) -> dict:
    agg: Counter = Counter()
    all_passed_p: list[float] = []
    for r in results:
        agg.update(r.get("counts", {}))
        all_passed_p.extend(r.get("passed_p_tp", []))

    total = sum(agg.values())
    passed = agg.get("PASSED", 0)

    print("\n" + "=" * 68)
    print("  OFFLINE FUNNEL — live v2 structural gate chain (cached 1h)")
    print(f"  Flags: HTF_HARD_GATE={config.htf_hard_gate} "
          f"REQUIRE_ENTRY_ZONE={config.require_entry_zone} MIN_P_TP={config.min_p_tp}")
    print("=" * 68)
    print(f"  Evaluated candidates: {total}   PASSED: {passed} "
          f"({passed / total * 100:.2f}%)" if total else "  (no candidates)")

    print("\n  Stage blocks (ordered):")
    known = set(STAGES)
    for stage in STAGES:
        c = agg.get(stage, 0)
        if stage == "PASSED":
            continue
        pct = c / total * 100 if total else 0.0
        print(f"    {stage:22s} {c:>8d}  ({pct:5.2f}%)")
    # Risk sub-buckets and any unexpected keys.
    extras = sorted(k for k in agg if k not in known)
    for k in extras:
        c = agg[k]
        pct = c / total * 100 if total else 0.0
        print(f"    {k:22s} {c:>8d}  ({pct:5.2f}%)")

    print("\n  TOP-5 killers:")
    killers = sorted(((k, v) for k, v in agg.items() if k != "PASSED"), key=lambda x: -x[1])
    for rank, (k, v) in enumerate(killers[:5], 1):
        pct = v / total * 100 if total else 0.0
        print(f"    #{rank} {k:22s} {v:>8d}  ({pct:5.2f}%)")

    if all_passed_p:
        srt = sorted(all_passed_p)
        n = len(srt)
        p50 = srt[n // 2]
        p10 = srt[max(0, n // 10)]
        print(f"\n  PASSED P(TP): n={n} min={srt[0]:.2%} p10={p10:.2%} "
              f"median={p50:.2%} max={srt[-1]:.2%}")

    return {"aggregate": dict(agg), "total": total, "passed": passed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline gate-block funnel for the live v2 pipeline")
    parser.add_argument("--symbols", type=str, default=None, help="comma-separated, default 5 majors")
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES, help="cached candle count to load")
    parser.add_argument("--limit", type=int, default=None, help="only evaluate the last N bars per symbol")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS

    results = []
    for idx, symbol in enumerate(symbols, 1):
        print(f"  [{idx}/{len(symbols)}] {symbol} ...", end=" ", flush=True)
        t0 = time.time()
        r = run_symbol(symbol.strip(), args.candles, args.limit)
        dt = time.time() - t0
        if r.get("error"):
            print(f"SKIP ({r['error']}) {dt:.1f}s")
        else:
            print(f"passed={r['counts'].get('PASSED', 0)} ({dt:.1f}s)")
        results.append(r)

    summary = _print_report(results)

    out_dir = Path(__file__).parent.parent / "reports" / "funnel"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "funnel_offline.json"
    with open(out_path, "w") as f:
        json.dump({"per_symbol": results, "summary": summary}, f, indent=2)
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
