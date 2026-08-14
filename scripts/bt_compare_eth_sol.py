"""Compare ETH vs SOL trade characteristics to find why ETH underperforms."""
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
from strategy.trade_engine import trade_engine
from strategy.feature_builder import feature_builder
from strategy.probability_engine import probability_engine
from risk.engine import risk_engine, PortfolioState
from risk.market_regime import MarketRegime
from risk.volatility_regime import classify_volatility
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from market_structure.structure import analyze_structure

import logging
logging.getLogger("indicators.engine").setLevel(logging.ERROR)


async def analyze(symbol, timeframe="4h", limit=2500):
    await exchange_client.connect()
    for attempt in range(3):
        try:
            df = await exchange_client.fetch_ohlcv_paginated(symbol, timeframe, total_limit=limit)
            break
        except Exception as e:
            if attempt < 2:
                await exchange_client.close()
                await asyncio.sleep(5)
                await exchange_client.connect()
            else:
                await exchange_client.close()
                return None

    if df is None or len(df) < 100:
        return None

    ind_engine = IndicatorEngine()
    warmup = 80
    trades = []
    in_trade = False
    ct = None

    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1].copy()
        ind = ind_engine.calculate(window, symbol, timeframe)
        if ind is None:
            continue

        if in_trade and ct:
            high, low = float(ind.high), float(ind.low)
            hit = False
            if ct["dir"] == "BUY":
                if low <= ct["sl"]: ct["exit"] = ct["sl"]; ct["reason"] = "SL"; hit = True
                elif high >= ct["tp"]: ct["exit"] = ct["tp"]; ct["reason"] = "TP"; hit = True
            else:
                if high >= ct["sl"]: ct["exit"] = ct["sl"]; ct["reason"] = "SL"; hit = True
                elif low <= ct["tp"]: ct["exit"] = ct["tp"]; ct["reason"] = "TP"; hit = True
            if hit:
                trades.append(ct)
                in_trade = False
                ct = None
        if in_trade:
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

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=order_blocks,
            structure=structure, fvgs=fvgs, candle_quality=cq,
            current_price=ind.close, atr=ind.atr if ind.atr else 0.0,
        )
        if not setup.detected:
            continue

        tp = trade_engine.build_trade_plan(
            ind=ind, direction=setup.direction,
            structure=structure, order_blocks=order_blocks,
            sweeps=sweeps, fvgs=fvgs, df=_df_clean, timeframe=timeframe,
        )
        if not tp.is_valid or tp.sl == 0 or tp.tp == 0:
            continue

        entry = float(ind.close)
        regime = MarketRegime(
            regime="trend", confidence=0.5,
            adx=float(ind.adx or 20), atr_percentile=50,
            ema_spread_trend="stable",
        )
        vol_regime = classify_volatility(float(ind.atr or 0), float(ind.close or 1))
        features = feature_builder.build(
            setup=setup, ind=ind, structure=structure,
            regime=regime, vol_regime=vol_regime,
            mtf_aligned=False, mtf_count=0,
            context_score=None, fear_greed=None, funding_rate=None,
            sl=tp.sl, tp=tp.tp, entry_price=entry,
            candle_quality=cq, is_reversal=setup.is_reversal,
        )
        prob = probability_engine.predict(features)
        rd = risk_engine.evaluate(
            features=features, probability=prob,
            portfolio=PortfolioState(), entry_price=entry,
            sl=tp.sl, tp=tp.tp, mss_quality=setup.mss_score,
            atr=float(ind.atr or 0), sl_source=tp.sl_source,
        )
        if not rd.should_trade:
            continue

        # Record trade with extra diagnostics
        atr_val = float(ind.atr or 0)
        sl_pct = abs(entry - (rd.sl_price or tp.sl)) / entry * 100
        tp_pct = abs((rd.tp_price or tp.tp) - entry) / entry * 100

        ct = {
            "entry": entry,
            "exit": 0,
            "sl": rd.sl_price or tp.sl,
            "tp": rd.tp_price or tp.tp,
            "dir": setup.direction.upper(),
            "setup": setup.setup_type,
            "quality": setup.overall_quality,
            "adx": float(ind.adx or 0),
            "atr_pct": atr_val / entry * 100,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "rr": tp_pct / sl_pct if sl_pct > 0 else 0,
            "entry_i": i,
            "reason": "",
        }
        in_trade = True

    if in_trade and ct:
        ct["exit"] = float(df.iloc[-1]["close"])
        ct["reason"] = "EOB"
        trades.append(ct)

    await exchange_client.close()

    if not trades:
        return None

    # Calculate PnL
    for t in trades:
        if t["dir"] == "BUY":
            t["pnl"] = (t["exit"] - t["entry"]) / t["entry"] * 100
        else:
            t["pnl"] = (t["entry"] - t["exit"]) / t["entry"] * 100

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = len(trades)

    # Analysis
    result = {
        "symbol": symbol,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "wr": len(wins) / total * 100 if total else 0,
        "pnl": sum(t["pnl"] for t in trades),
        "trades": trades,
    }

    # Breakdowns
    for label, subset in [
        ("BUY cont", [t for t in trades if t["dir"] == "BUY" and t["setup"] == "continuation"]),
        ("SELL cont", [t for t in trades if t["dir"] == "SELL" and t["setup"] == "continuation"]),
        ("BUY rev", [t for t in trades if t["dir"] == "BUY" and t["setup"] == "reversal"]),
        ("SELL rev", [t for t in trades if t["dir"] == "SELL" and t["setup"] == "reversal"]),
    ]:
        if subset:
            sw = [t for t in subset if t["pnl"] > 0]
            avg_adx = sum(t["adx"] for t in subset) / len(subset)
            avg_quality = sum(t["quality"] for t in subset) / len(subset)
            avg_sl = sum(t["sl_pct"] for t in subset) / len(subset)
            avg_rr = sum(t["rr"] for t in subset) / len(subset)
            avg_atr = sum(t["atr_pct"] for t in subset) / len(subset)
            pnl = sum(t["pnl"] for t in subset)
            print(f"  {label}: {len(subset)} trades, wr={len(sw)/len(subset)*100:.0f}%, "
                  f"pnl={pnl:+.2f}%, avg_adx={avg_adx:.1f}, avg_quality={avg_quality:.0f}, "
                  f"avg_sl={avg_sl:.2f}%, avg_rr={avg_rr:.2f}, avg_atr={avg_atr:.2f}%")

    # Quality distribution
    print(f"\n  Quality distribution:")
    for lo, hi in [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]:
        subset = [t for t in trades if lo <= t["quality"] < hi]
        if subset:
            sw = [t for t in subset if t["pnl"] > 0]
            pnl = sum(t["pnl"] for t in subset)
            print(f"    Q{lo}-{hi}: {len(subset)} trades, wr={len(sw)/len(subset)*100:.0f}%, pnl={pnl:+.2f}%")

    # ADX distribution
    print(f"\n  ADX distribution:")
    for lo, hi in [(0, 15), (15, 20), (20, 25), (25, 30), (30, 50)]:
        subset = [t for t in trades if lo <= t["adx"] < hi]
        if subset:
            sw = [t for t in subset if t["pnl"] > 0]
            pnl = sum(t["pnl"] for t in subset)
            print(f"    ADX {lo}-{hi}: {len(subset)} trades, wr={len(sw)/len(subset)*100:.0f}%, pnl={pnl:+.2f}%")

    # SL% distribution
    print(f"\n  SL% distribution:")
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 10)]:
        subset = [t for t in trades if lo <= t["sl_pct"] < hi]
        if subset:
            sw = [t for t in subset if t["pnl"] > 0]
            pnl = sum(t["pnl"] for t in subset)
            print(f"    SL {lo}-{hi}%: {len(subset)} trades, wr={len(sw)/len(subset)*100:.0f}%, pnl={pnl:+.2f}%")

    # ATR% distribution
    print(f"\n  ATR% distribution:")
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 10)]:
        subset = [t for t in trades if lo <= t["atr_pct"] < hi]
        if subset:
            sw = [t for t in subset if t["pnl"] > 0]
            pnl = sum(t["pnl"] for t in subset)
            print(f"    ATR {lo}-{hi}%: {len(subset)} trades, wr={len(sw)/len(subset)*100:.0f}%, pnl={pnl:+.2f}%")

    return result


async def main():
    eth = await analyze("ETH/USDT")
    print()
    sol = await analyze("SOL/USDT")


if __name__ == "__main__":
    asyncio.run(main())
