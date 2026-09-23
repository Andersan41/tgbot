"""Offline reproduction against audited production sources; no production edits."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
from backtest.resampler import resample_ohlcv
from config.settings import config
from liquidity.fvg import FairValueGap, detect_fvg
from market_structure.structure import CHoCH, StructureState, classify_choch
from strategy.pattern_engine import PatternEngine
from strategy.trade_engine import TradeEngine


def main():
    out = {}
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    index = pd.date_range(now, periods=9, freq="h")
    data = pd.DataFrame({
        "open": [100] * 7 + [105, 106],
        "high": [101] * 7 + [106, 107],
        "low": [99] * 7 + [104, 105],
        "close": [100] * 7 + [105, 106],
        "volume": [100] * 9,
    }, index=index)
    gap = next(f for f in detect_fvg(data, min_size_pct=0.1) if f.index == 7)
    assert gap.filled is True
    assert all(data["low"].iloc[gap.index + 1:] > gap.top)
    out["fvg_sparse_index"] = {
        "formed_at_index": gap.index,
        "gap_top": gap.top,
        "post_formation_lows": data["low"].iloc[gap.index + 1:].tolist(),
        "incorrectly_filled": gap.filled,
    }

    out["phantom_live_entry"] = []
    for direction, kind, low, high, live in [
        ("buy", "bullish", 98, 100, 101),
        ("sell", "bearish", 102, 104, 101),
    ]:
        fvg = FairValueGap(kind, high, low, now)
        ind = SimpleNamespace(close=live, atr=1, symbol="BTC/USDT")
        plan = TradeEngine().build_trade_plan(
            ind, direction, fvgs=[fvg], entry_override=live)
        rejected_by_live_direction_gate = (
            direction == "buy" and plan.entry_price > live * 1.001
        ) or (direction == "sell" and plan.entry_price < live * 0.999)
        assert plan.entry_price != live
        assert not rejected_by_live_direction_gate
        out["phantom_live_entry"].append({
            "direction": direction, "live": live,
            "booked_entry": plan.entry_price,
            "direction_gate_rejects": rejected_by_live_direction_gate,
        })

    out["reversal_semantics"] = []
    for kind in ("bullish", "bearish"):
        for sweep_kind in (kind, "bearish" if kind == "bullish" else "bullish"):
            sweep = SimpleNamespace(type=sweep_kind, candle_index=7,
                is_valid=True, strength=0.8, reclaim_candles=1)
            choch = CHoCH(kind, 100, now, 9)
            classified = classify_choch(choch, [sweep], displacement_atr=1.5,
                                        reclaim_bars=1)
            state = StructureState(trend=kind,
                last_mss=classified if classified.strength == "mss" else None)
            setup = PatternEngine()._try_reversal([sweep], state, None, 1, 100)
            assert not setup.detected
            out["reversal_semantics"].append({
                "choch": kind, "sweep": sweep_kind,
                "classified_strength": classified.strength,
                "reversal_detected": setup.detected,
                "rejection": setup.rejection_reason,
            })

    hourly = pd.DataFrame({
        "open": [100, 110, 120, 130], "high": [101, 111, 121, 131],
        "low": [99, 109, 119, 129], "close": [100, 110, 120, 130],
        "volume": [1] * 4,
    }, index=pd.date_range(now, periods=4, freq="h"))
    h4 = resample_ohlcv(hourly, "4h")
    ts = hourly.index[1]
    leaked = float(h4.loc[:ts]["close"].iloc[-1])
    assert leaked == 130 and float(hourly.loc[ts, "close"]) == 110
    out["htf_lookahead"] = {
        "signal_bar_open": str(ts), "decision_time": str(ts + pd.Timedelta(hours=1)),
        "future_close_seen": leaked,
        "future_close_available_at": str(h4.index[0] + pd.Timedelta(hours=4)),
    }
    partial = hourly.iloc[:3].copy()
    partial.index = pd.date_range(now, periods=3, freq="15min")
    partial_h1 = resample_ohlcv(partial, "1h")
    assert len(partial_h1) == 1
    out["partial_resample"] = {"input_15m_bars": 3,
        "incorrectly_kept_1h_bars": len(partial_h1)}
    text = json.dumps(out, indent=2, ensure_ascii=False)
    (Path(__file__).with_name("reproduction-results.json")).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
