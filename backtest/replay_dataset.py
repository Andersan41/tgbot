"""
backtest/replay_dataset.py — Offline labeled-dataset builder for the LIVE v2 pipeline.

Replays cached OHLCV bar-by-bar through the REAL current ICT pipeline components
(the same ones `scheduler.scanner.scan_symbol_v2` uses: PatternEngine → setup-type
gates → HTF bias V2 → trade plan → FeatureBuilder → ProbabilityEngine → RiskEngine)
and, for every structurally-valid setup, emits:

  • the full ML feature vector (`SetupFeatures.to_vector()` — the exact input the live
    ProbabilityEngine consumes),
  • the forward-resolved outcome label (HIT_TP / HIT_SL / EXPIRED) using the same
    SL/TP-first-touch resolution as the live outcome tracker, plus realized PnL,
  • metadata (symbol, timestamp, direction, setup type) and diagnostic flags
    (rules-based P(TP), whether the RiskEngine would have accepted it).

The result is a leakage-free supervised dataset for the feasibility spike: features
known at decision time, label resolved strictly forward. Written to
reports/dataset/ as parquet + CSV, consumable by the training/validation scripts.

Design notes:
  • POPULATION = every structurally-valid setup (passes pattern + setup-type + HTF +
    trade-plan gates). The probability/risk gates are RECORDED, not applied as filters —
    filtering the training set by the rules P(TP) would bias it and shrink it.
  • NO LOOK-AHEAD in features: every window is sliced up to the decision bar; HTF frames
    are resampled from 1h and sliced with `.loc[:ts]`. The one live bug avoided here is
    `FeatureBuilder` stamping `session` from wall-clock `datetime.now()` — offline we
    override it from the bar's own timestamp (see `_session_for_ts`).
  • Label resolution is conservative on same-bar SL+TP touches: SL is assumed first
    (matches the live/backtest first-touch convention).

NOTE: written offline and NOT runtime-validated in the authoring environment
(no deps there). Run in the project venv (Python 3.11).

Usage:
    # Build the dataset for the deep-cached majors (see backtest.cache_ohlcv --max-history)
    python -m backtest.replay_dataset --symbols BTC/USDT,ETH/USDT --max-history
    python -m backtest.replay_dataset --symbols BTC/USDT --candles 3900   # fixed-count cache
    python -m backtest.replay_dataset --max-history --ttl-days 7 --out reports/dataset/ict_dataset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

from config.settings import config, VERSION, STRATEGY_MODE, StrategyMode
from backtest.cache_ohlcv import load_cached, load_cached_max
from backtest.run_funnel import _build_ind_values, _row_has_valid_indicators

from indicators.engine import IndicatorEngine
from strategy.pattern_engine import pattern_engine
from strategy.feature_builder import feature_builder, _SESSION_MAP
from strategy.probability_engine import probability_engine
from strategy.trade_engine import trade_engine
from strategy.signal_evaluator import entry_zone_touched
from risk.engine import risk_engine, PortfolioState
from risk.volatility_regime import classify_volatility
from scheduler.scanner import _detect_regime
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from market_structure.structure import analyze_structure, calc_premium_discount_score
from market_structure.htf_bias_v2 import get_htf_bias_v2, HTFBiasResult

TIMEFRAME = "1h"
DEFAULT_CANDLES = 3900
WARMUP = 260  # bars fed to the indicator engine + structure lookbacks

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "LINK/USDT"]

# Transaction-cost model — mirrors backtest/run_new_pipeline.py for parity.
COMMISSION_PCT = 0.06  # taker fee per side (%)
SLIPPAGE_PCT = 0.02    # slippage per side (%)
_ROUND_TRIP_COST = (COMMISSION_PCT + SLIPPAGE_PCT) * 2

_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

# 1h → HTF resample rules (pandas offset aliases).
_HTF_RULES = {"4h": "4h", "1d": "1D", "1w": "1W"}
_OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a 1h OHLCV frame to a higher timeframe."""
    return df_1h.resample(rule, label="right", closed="right").agg(_OHLC_AGG).dropna()


def _session_for_ts(ts: pd.Timestamp) -> str:
    """Trading session for a bar timestamp — the offline, look-ahead-free analogue of
    FeatureBuilder's wall-clock `_detect_session()`."""
    hour = ts.hour
    for session, (start, end) in _SESSION_MAP.items():
        if start <= hour < end:
            return session
    return "off_hours"


def _resolve_label(
    df: pd.DataFrame,
    entry_idx: int,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    ttl_bars: int,
) -> dict:
    """Resolve a setup's forward outcome from bars AFTER the decision bar.

    First-touch semantics with a conservative same-bar tie-break (SL assumed first).
    Returns result in {HIT_TP, HIT_SL, EXPIRED, UNRESOLVED} plus realized PnL. UNRESOLVED
    means the series ran out before TTL with no touch — its label cannot be trusted and it
    should be excluded from training (`label_complete=False`).
    """
    is_buy = direction.lower() == "buy"
    last_available = len(df) - 1
    horizon_end = entry_idx + ttl_bars

    exit_price = None
    result = None
    exit_idx = None

    for j in range(entry_idx + 1, min(horizon_end, last_available) + 1):
        high = float(df["high"].iloc[j])
        low = float(df["low"].iloc[j])
        if is_buy:
            if low <= sl:
                exit_price, result, exit_idx = sl, "HIT_SL", j
                break
            if high >= tp:
                exit_price, result, exit_idx = tp, "HIT_TP", j
                break
        else:  # sell
            if high >= sl:
                exit_price, result, exit_idx = sl, "HIT_SL", j
                break
            if low <= tp:
                exit_price, result, exit_idx = tp, "HIT_TP", j
                break

    label_complete = True
    if result is None:
        # No touch within the window.
        if horizon_end <= last_available:
            # TTL fully elapsed inside available data → a genuine EXPIRED outcome.
            exit_idx = min(horizon_end, last_available)
            exit_price = float(df["close"].iloc[exit_idx])
            result = "EXPIRED"
        else:
            # Ran out of data before TTL — outcome unknown, do not trust the label.
            exit_idx = last_available
            exit_price = float(df["close"].iloc[exit_idx])
            result = "UNRESOLVED"
            label_complete = False

    assert exit_price is not None and exit_idx is not None  # every branch above assigns them
    if is_buy:
        pnl_pct = (exit_price / entry - 1) * 100
    else:
        pnl_pct = (1 - exit_price / entry) * 100
    net_pnl_pct = pnl_pct - _ROUND_TRIP_COST

    return {
        "result": result,
        "tp_hit": int(result == "HIT_TP"),
        "win": int(net_pnl_pct > 0),
        "pnl_pct": round(pnl_pct, 4),
        "net_pnl_pct": round(net_pnl_pct, 4),
        "bars_held": int(exit_idx - entry_idx),
        "label_complete": label_complete,
    }


def run_symbol(symbol: str, candles: int, limit: int | None, ttl_bars: int,
               use_max: bool) -> list[dict]:
    """Replay one symbol and collect a labeled row per structurally-valid setup."""
    df = load_cached_max(symbol, TIMEFRAME) if use_max else load_cached(symbol, TIMEFRAME, candles)
    if df is None:
        print(f"    (no cached data for {symbol})")
        return []

    df = df.copy()
    IndicatorEngine().calculate(df, symbol, TIMEFRAME)
    htf_frames = {tf: _resample(df, rule) for tf, rule in _HTF_RULES.items()}

    rows: list[dict] = []
    start = WARMUP
    if limit is not None:
        start = max(WARMUP, len(df) - limit)

    prev_clean = None
    total_bars = len(df) - start
    for i in range(start, len(df)):
        if (i - start) % 500 == 0 and i > start:
            print(f"  {symbol}: {i - start}/{total_bars} bars...", end=" ", flush=True)
        row = df.iloc[i]
        if not _row_has_valid_indicators(row):
            continue
        if prev_clean is None:
            prev_clean = df.iloc[i - 1]
        ind = _build_ind_values(row, prev_clean, symbol, TIMEFRAME)
        prev_clean = row

        if ind is None or ind.atr is None or ind.atr <= 0:
            continue

        current_price = float(ind.close)
        window = df.iloc[max(0, i - 100):i + 1]
        _df_clean = window.dropna(subset=["open", "high", "low", "close", "volume"])
        if len(_df_clean) < 10:
            continue

        # ── Phase 1: liquidity + structure ──
        try:
            sweeps = detect_sweeps(_df_clean, lookback=50)
        except Exception:
            logger.debug("replay: detect_sweeps failed", exc_info=True)
            sweeps = []
        try:
            order_blocks = detect_order_blocks(_df_clean, lookback=100)
        except Exception:
            logger.debug("replay: detect_order_blocks failed", exc_info=True)
            order_blocks = []
        try:
            candle_quality = analyze_last_candle(_df_clean, atr_value=ind.atr)
        except Exception:
            logger.debug("replay: analyze_last_candle failed", exc_info=True)
            candle_quality = None
        try:
            fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 100))
        except Exception:
            logger.debug("replay: detect_fvg failed", exc_info=True)
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
            logger.debug("replay: analyze_structure failed", exc_info=True)
            structure = None

        # ── Phase 1: Pattern Engine ──
        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=order_blocks, structure=structure,
            fvgs=fvgs, candle_quality=candle_quality,
            current_price=current_price, atr=ind.atr if ind.atr else 0.0,
        )
        if not setup.detected or not setup.direction:
            continue

        # ── Direction / Symbol filter (same as live) ──
        if setup.direction == "sell":
            continue
        if symbol == "WIF/USDT" and setup.direction == "buy":
            continue

        # ── Confluence Mode (v3.0) ──
        # Active only when STRATEGY_MODE == "confluence"
        if STRATEGY_MODE == StrategyMode.CONFLUENCE:
            # Reversal block: WR 3.6% across 360d
            if setup.setup_type == "reversal":
                continue
            # OB required for BOS continuation: WR 93.7% with OB
            if setup.setup_type == "continuation" and not setup.has_ob:
                continue

        # ── Phase 1.4: setup-type structural gates (same as live) ──
        if setup.setup_type == "reversal":
            if not setup.has_sweep:
                continue
            if config.pattern_engine.reversal_require_displacement and not setup.has_displacement:
                continue
            if not setup.has_mss:
                continue
        elif setup.setup_type == "continuation":
            if not setup.has_bos:
                continue

        # ── Phase 1.45: HTF bias V2 (resampled offline, no look-ahead) ──
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
            logger.debug("replay: get_htf_bias_v2 failed", exc_info=True)
            htf_dir = "neutral"

        if htf_dir in ("bullish", "bearish"):
            want = "buy" if htf_dir == "bullish" else "sell"
            if setup.direction != want:
                if setup.setup_type == "continuation":
                    if config.htf_hard_gate:
                        continue
                    _htf_bias_penalty = config.htf_bias_continuation_penalty
                elif setup.setup_type == "reversal":
                    _htf_bias_penalty = config.htf_bias_continuation_penalty

        # ── Entry zone (opt-in hard gate, same as live) ──
        if config.require_entry_zone:
            _bar_high = float(ind.high) if ind.high else float(ind.close)
            _bar_low = float(ind.low) if ind.low else float(ind.close)
            if not entry_zone_touched(setup.direction, fvgs, _bar_high, _bar_low):
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
            logger.debug("replay: build_trade_plan failed", exc_info=True)
            sl, tp = None, None
        if sl is None or tp is None:
            continue

        entry_price = current_price

        # ── Phase 2: features (regime + vol regime, no MTF/context/SR offline) ──
        regime = _detect_regime(ind, window, symbol, TIMEFRAME)
        vol_regime = classify_volatility(ind.atr, ind.close)
        is_reversal = setup.setup_type == "reversal"
        features = feature_builder.build(
            setup=setup, ind=ind, structure=structure, regime=regime, vol_regime=vol_regime,
            mtf_aligned=False, mtf_count=0, context_score=0.0, fear_greed=None, funding_rate=None,
            sl=sl, tp=tp, entry_price=entry_price, candle_quality=candle_quality,
            is_reversal=is_reversal, htf_bias_penalty=_htf_bias_penalty,
            ob_state_multiplier=1.0, smt_divergence_score=0.0,
        )
        # Override wall-clock session with the bar's own session (no look-ahead / no drift).
        features.session = _session_for_ts(ts)
        # Compute PD score from structure window (look-back only, no look-ahead)
        try:
            pd_window = df.iloc[max(0, i - 59):i + 1]
            pd_score = calc_premium_discount_score(pd_window, setup.direction)
        except Exception:
            logger.debug("replay: calc_premium_discount_score failed", exc_info=True)
            pd_score = 0.5
        features.premium_discount_score = pd_score
        feat_vector = features.to_vector()

        # ── Phase 3: rules P(TP) baseline (recorded, not used as a filter) ──
        try:
            probability = probability_engine.predict(features)
            p_tp_rules = float(probability.p_tp)
        except Exception:
            logger.debug("replay: probability_engine.predict failed", exc_info=True)
            probability = None
            p_tp_rules = float("nan")

        # ── Phase 4: risk engine decision (recorded, not used as a filter) ──
        passed_risk = False
        risk_reason = ""
        if probability is not None:
            portfolio = PortfolioState(
                active_count=0, total_risk_pct=0.0,
                max_active_signals=config.max_active_signals,
                max_portfolio_risk_pct=config.max_portfolio_risk_pct,
            )
            try:
                risk_decision = risk_engine.evaluate(
                    features=features, probability=probability, portfolio=portfolio,
                    entry_price=entry_price, sl=sl, tp=tp,
                    scenario_score=0.0, scenario_stability=0.0,
                    mss_quality=setup.mss_score, atr=ind.atr if ind.atr else 0.0,
                )
                passed_risk = bool(risk_decision.should_trade)
                risk_reason = risk_decision.rejection_reason or ""
            except Exception as e:  # noqa: BLE001
                risk_reason = f"risk_error:{e}"

        # ── Forward-resolved label ──
        label = _resolve_label(df, i, setup.direction, entry_price, sl, tp, ttl_bars)

        meta = {
            "symbol": symbol,
            "timeframe": TIMEFRAME,
            "created_at": str(ts),
            "direction": setup.direction.upper(),
            "setup_type": setup.setup_type or "unknown",
            "entry_index": i,
            "entry_price": round(entry_price, 8),
            "sl_price": round(float(sl), 8),
            "tp_price": round(float(tp), 8),
            "p_tp_rules": round(p_tp_rules, 4) if p_tp_rules == p_tp_rules else None,
            "passed_risk": passed_risk,
            "risk_reason": risk_reason,
            "strategy_version": VERSION,
        }
        rows.append({**feat_vector, **label, **meta})

    return rows


def _summarize(df: pd.DataFrame) -> dict:
    print("\n" + "=" * 70)
    print("  ICT REPLAY DATASET — summary")
    print("=" * 70)
    print(f"  Total setups:        {len(df)}")
    complete = df[df["label_complete"]]
    summary: dict = {"total_setups": int(len(df)), "label_complete": int(len(complete))}
    print(f"  Label-complete:      {len(complete)} "
          f"({len(complete) / len(df) * 100:.1f}%)" if len(df) else "  (empty)")
    if len(complete):
        outcomes = {str(k): int(v) for k, v in complete["result"].value_counts().items()}
        print("\n  Outcome distribution (label-complete):")
        for res, cnt in outcomes.items():
            print(f"    {res:12s} {cnt:>7d}  ({cnt / len(complete) * 100:5.1f}%)")
        print(f"\n  Win rate (net>0):    {complete['win'].mean() * 100:.1f}%")
        print(f"  Avg net PnL/setup:   {complete['net_pnl_pct'].mean():+.3f}%")
        print(f"  TP-before-SL rate:   {complete['tp_hit'].mean() * 100:.1f}%")
        print(f"  Passed-risk subset:  {int(complete['passed_risk'].sum())} setups")
        print("\n  Per-symbol counts:")
        per_symbol = {str(k): int(v) for k, v in complete["symbol"].value_counts().items()}
        for sym, cnt in per_symbol.items():
            print(f"    {sym:14s} {cnt:>7d}")
        summary.update({
            "outcomes": outcomes,
            "win_rate_pct": round(float(complete["win"].mean()) * 100, 1),
            "avg_net_pnl_pct": round(float(complete["net_pnl_pct"].mean()), 4),
            "tp_before_sl_pct": round(float(complete["tp_hit"].mean()) * 100, 1),
            "passed_risk_setups": int(complete["passed_risk"].sum()),
            "per_symbol": per_symbol,
            "date_range": [str(complete["created_at"].min()), str(complete["created_at"].max())],
        })
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a labeled ICT dataset by offline replay")
    parser.add_argument("--symbols", type=str, default=None, help="comma-separated, default 5 majors")
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES,
                        help="fixed-count cache size to load (ignored with --max-history)")
    parser.add_argument("--max-history", action="store_true",
                        help="load the deepest-available cache (<sym>_<tf>_max.parquet)")
    parser.add_argument("--limit", type=int, default=None, help="only evaluate the last N bars per symbol")
    parser.add_argument("--ttl-days", type=float, default=7.0,
                        help="max holding horizon before EXPIRED (default: 7, matches live outcome TTL)")
    parser.add_argument("--out", type=str, default="reports/dataset/ict_dataset",
                        help="output path stem (writes .parquet and .csv)")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS
    ttl_bars = int(args.ttl_days * 24 * 60 / _TF_MINUTES[TIMEFRAME])
    print(f"  TTL: {args.ttl_days} days = {ttl_bars} bars ({TIMEFRAME})")

    all_rows: list[dict] = []
    for idx, symbol in enumerate(symbols, 1):
        symbol = symbol.strip()
        print(f"  [{idx}/{len(symbols)}] {symbol} ...", end=" ", flush=True)
        t0 = time.time()
        rows = run_symbol(symbol, args.candles, args.limit, ttl_bars, args.max_history)
        dt = time.time() - t0
        print(f"{len(rows)} setups ({dt:.1f}s)")
        all_rows.extend(rows)

    if not all_rows:
        print("\n  No setups collected — check that the cache is populated "
              "(backtest.cache_ohlcv --max-history).")
        return

    df = pd.DataFrame(all_rows)
    summary = _summarize(df)

    out_stem = Path(args.out)
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    parquet_path = out_stem.with_suffix(".parquet")
    csv_path = out_stem.with_suffix(".csv")
    df.to_parquet(parquet_path, index=False)
    df.to_csv(csv_path, index=False)
    print(f"\n  Saved: {parquet_path} ({df.shape[0]} rows × {df.shape[1]} cols)")
    print(f"  Saved: {csv_path}")

    # Compact JSON summary for the feasibility report (pure-stdlib consumers).
    summary["dataset_path"] = str(parquet_path)
    summary_dir = Path(__file__).parent.parent / "reports" / "feasibility"
    summary_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_dir / "dataset_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  Saved: {summary_dir / 'dataset_summary.json'}")


if __name__ == "__main__":
    main()
