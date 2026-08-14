"""Multi-symbol backtest: BTC, ETH, SOL — Binance spot, 4h, 5000 candles each."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCHANGE"] = "binance"
os.environ["MARKET_TYPE"] = "spot"

from config.settings import config
config.trading.candles_limit = 5000

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


async def backtest_symbol(symbol: str, timeframe: str = "4h", limit: int = 5000):
    df = await exchange_client.fetch_ohlcv_paginated(symbol, timeframe, total_limit=limit)
    if df is None or len(df) < 100:
        print(f"  {symbol}: Not enough data")
        return []

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

        ct = {
            "entry": entry,
            "sl": rd.sl_price or tp.sl,
            "tp": rd.tp_price or tp.tp,
            "dir": setup.direction.upper(),
            "setup": setup.setup_type,
            "entry_i": i,
        }
        in_trade = True

    if in_trade and ct:
        ct["exit"] = float(df.iloc[-1]["close"])
        ct["reason"] = "EOB"
        trades.append(ct)

    return trades


def print_results(symbol, trades, days):
    if not trades:
        print(f"  {symbol}: 0 trades")
        return

    for t in trades:
        if t["dir"] == "BUY":
            t["pnl"] = (t["exit"] - t["entry"]) / t["entry"] * 100
        else:
            t["pnl"] = (t["entry"] - t["exit"]) / t["entry"] * 100

    total = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / total * 100
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0
    pf = gp / gl if gl > 0 else float("inf")
    cum = sum(t["pnl"] for t in trades)

    print(f"  {symbol}: {total} trades | WR={wr:.0f}% | PF={pf:.2f} | PnL={cum:+.2f}% (~{cum/days*365:+.1f}%/yr)")

    for d in ["BUY", "SELL"]:
        dt = [t for t in trades if t["dir"] == d]
        if dt:
            dw = [t for t in dt if t["pnl"] > 0]
            dp = sum(t["pnl"] for t in dt)
            print(f"    {d}: {len(dt)} trades, wr={len(dw)/len(dt)*100:.0f}%, pnl={dp:+.2f}%")

    for s in ["reversal", "continuation"]:
        st = [t for t in trades if t["setup"] == s]
        if st:
            sw = [t for t in st if t["pnl"] > 0]
            sp = sum(t["pnl"] for t in st)
            print(f"    [{s}]: {len(st)} trades, wr={len(sw)/len(st)*100:.0f}%, pnl={sp:+.2f}%")

    for i, t in enumerate(trades):
        em = "W" if t["pnl"] > 0 else "L"
        print(f"    #{i+1} [{em}] {t['dir']} {t['setup']} entry={t['entry']:.1f} exit={t['exit']:.1f} ({t['reason']}) pnl={t['pnl']:+.2f}%")


async def main():
    await exchange_client.connect()
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    all_trades = {}

    for sym in symbols:
        trades = await backtest_symbol(sym, "4h", 5000)
        all_trades[sym] = trades

    await exchange_client.close()

    print(f"\n{'='*60}")
    print(f"  MULTI-SYMBOL BACKTEST — Binance 4h")
    print(f"{'='*60}")

    total_all = 0
    pnl_all = 0
    for sym in symbols:
        trades = all_trades[sym]
        days = 833  # ~2.3 years
        print_results(sym, trades, days)
        total_all += len(trades)
        for t in trades:
            if t["dir"] == "BUY":
                t["pnl"] = (t["exit"] - t["entry"]) / t["entry"] * 100
            else:
                t["pnl"] = (t["entry"] - t["exit"]) / t["entry"] * 100
            pnl_all += t["pnl"]

    print(f"\n  AGGREGATE: {total_all} trades | PnL={pnl_all:+.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
