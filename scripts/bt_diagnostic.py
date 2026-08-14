"""Diagnostic: count reversal vs continuation attempts and rejection reasons."""
import asyncio
import sys
import os
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCHANGE"] = "binance"
os.environ["MARKET_TYPE"] = "spot"

from config.settings import config
config.trading.candles_limit = 80

from data.exchange_client import exchange_client
from indicators.engine import IndicatorEngine
from strategy.pattern_engine import pattern_engine
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from market_structure.structure import analyze_structure

import logging
logging.getLogger("indicators.engine").setLevel(logging.ERROR)


async def run(symbol="BTC/USDT", timeframe="4h", limit=2500):
    await exchange_client.connect()
    df = await exchange_client.fetch_ohlcv_paginated(symbol, timeframe, total_limit=limit)
    await exchange_client.close()

    if df is None or len(df) < 100:
        print("Not enough data")
        return

    print(f"{symbol}: {len(df)} candles\n")

    ind_engine = IndicatorEngine()
    warmup = 80

    reversal_attempts = 0
    continuation_attempts = 0
    reversal_found = 0
    continuation_found = 0
    reversal_reasons = Counter()
    continuation_reasons = Counter()
    sweep_types = Counter()

    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1].copy()
        ind = ind_engine.calculate(window, symbol, timeframe)
        if ind is None:
            continue

        _df_clean = window.dropna(subset=["open", "high", "low", "close", "volume"])
        sweeps, order_blocks, fvgs, structure, cq = [], [], [], None, None
        try:
            if len(_df_clean) >= 10:
                sweeps = detect_sweeps(_df_clean, lookback=50)
                order_blocks = detect_order_blocks(_df_clean, lookback=100)
                cq = analyze_last_candle(_df_clean, atr_value=ind.atr)
                fvgs = detect_fvg(_df_clean, lookback=100)
                structure = analyze_structure(
                    _df_clean, lookback=50, sweeps=sweeps,
                    displacement_atr=0.0, reclaim_bars=0,
                    atr_value=ind.atr if ind.atr else 0.0,
                )
        except Exception:
            pass

        # Count sweep types
        for s in sweeps:
            if s.is_valid:
                sweep_types[s.type] += 1

        # Try reversal
        reversal = pattern_engine._try_reversal(sweeps, structure, cq, ind.atr or 0.0, ind.close)
        reversal_attempts += 1
        if reversal.detected:
            reversal_found += 1
        else:
            reason = reversal.rejection_reason or "unknown"
            reversal_reasons[reason] += 1

        # Try continuation (even if reversal found)
        continuation = pattern_engine._try_continuation(structure, sweeps)
        continuation_attempts += 1
        if continuation.detected:
            continuation_found += 1
        else:
            reason = continuation.rejection_reason or "unknown"
            continuation_reasons[reason] += 1

    print(f"=== SWEEP TYPES (valid only) ===")
    for st, cnt in sweep_types.most_common():
        print(f"  {st}: {cnt}")

    print(f"\n=== REVERSAL ===")
    print(f"  Attempts: {reversal_attempts}")
    print(f"  Found: {reversal_found} ({reversal_found/reversal_attempts*100:.1f}%)")
    print(f"  Rejection reasons:")
    for r, cnt in reversal_reasons.most_common():
        print(f"    {r}: {cnt}")

    print(f"\n=== CONTINUATION ===")
    print(f"  Attempts: {continuation_attempts}")
    print(f"  Found: {continuation_found} ({continuation_found/continuation_attempts*100:.1f}%)")
    print(f"  Rejection reasons:")
    for r, cnt in continuation_reasons.most_common():
        print(f"    {r}: {cnt}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT"
    asyncio.run(run(symbol))
