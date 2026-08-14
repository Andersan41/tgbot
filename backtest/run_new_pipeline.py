"""
backtest/run_new_pipeline.py — Backtest for the new ICT pipeline.

Pipeline: Pattern Engine → Feature Builder → Probability Engine → Risk Engine

Usage:
    python -m backtest.run_new_pipeline
    python -m backtest.run_new_pipeline --days 90 --timeframe 1h
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
logger.disable("strategy.signal_engine")
logger.disable("indicators.engine")
logger.disable("strategy.pattern_engine")
logger.disable("risk.engine")
logger.disable("strategy.probability_engine")
logger.disable("strategy.trade_engine")

import numpy as np
import pandas as pd

from data.exchange_client import exchange_client
from indicators.engine import IndicatorEngine, IndicatorValues
from strategy.pattern_engine import pattern_engine, ICTSetup
from strategy.feature_builder import feature_builder
from strategy.probability_engine import probability_engine, TradeProbability
from risk.engine import risk_engine, PortfolioState, RiskDecision
from liquidity.sweep import detect_sweeps
from liquidity.order_blocks import detect_order_blocks
from liquidity.fvg import detect_fvg
from liquidity.candle_quality import analyze_last_candle
from liquidity.ob_state import get_ob_tracker, get_ob_multiplier, OBState
from market_structure.structure import analyze_structure, calc_premium_discount_score, _detect_trend_from_df
from market_structure.htf_bias import get_htf_bias, HTFBias, extract_structure_dict
from config.settings import config

# ── Config ─────────────────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT", "ZRO/USDT"]

ASSET_TYPES = {
    "BTC/USDT": "major",
    "ETH/USDT": "major",
    "ZRO/USDT": "L1",
}
TIMEFRAME = "4h"
DAYS = 90
WARMUP = 80           # candles for indicator warmup
COOLDOWN_BARS = 3     # minimum bars between signals per symbol
COMMISSION_PCT = 0.06  # 0.06% taker fee per side
SLIPPAGE_PCT = 0.02   # 0.02% slippage per side
MAX_TRADE_DURATION = 72  # max candles to hold a trade (72h for 1h TF)


# ── Trade simulation ───────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    direction: str           # "BUY" / "SELL"
    entry_price: float
    entry_index: int
    entry_timestamp: str
    sl: float
    tp: float
    exit_price: float = 0.0
    exit_index: int = 0
    exit_timestamp: str = ""
    exit_reason: str = ""
    pnl_pct: float = 0.0
    net_pnl_pct: float = 0.0
    setup_type: str = ""
    p_tp: float = 0.0
    rr: float = 0.0
    mss_score: float = 0.0
    regime: str = ""


@dataclass
class SymbolResult:
    symbol: str
    timeframe: str
    trades: list[Trade] = field(default_factory=list)
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: dict = field(default_factory=dict)


def simulate_trade(
    direction: str,
    entry_price: float,
    sl: float,
    tp: float,
    df: pd.DataFrame,
    entry_idx: int,
    symbol: str,
    setup_type: str = "",
    p_tp: float = 0.0,
    mss_score: float = 0.0,
    regime: str = "",
) -> Trade:
    """Simulate a trade forward from entry, checking SL/TP on each candle."""
    trade = Trade(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        entry_index=entry_idx,
        entry_timestamp=str(df.index[entry_idx]),
        sl=sl,
        tp=tp,
        setup_type=setup_type,
        p_tp=p_tp,
        mss_score=mss_score,
        regime=regime,
    )

    for i in range(entry_idx + 1, min(entry_idx + MAX_TRADE_DURATION + 1, len(df))):
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        if direction == "BUY":
            if low <= sl:
                trade.exit_price = sl
                trade.exit_index = i
                trade.exit_timestamp = str(df.index[i])
                trade.exit_reason = "sl"
                break
            elif high >= tp:
                trade.exit_price = tp
                trade.exit_index = i
                trade.exit_timestamp = str(df.index[i])
                trade.exit_reason = "tp"
                break
        else:  # SELL
            if high >= sl:
                trade.exit_price = sl
                trade.exit_index = i
                trade.exit_timestamp = str(df.index[i])
                trade.exit_reason = "sl"
                break
            elif low <= tp:
                trade.exit_price = tp
                trade.exit_index = i
                trade.exit_timestamp = str(df.index[i])
                trade.exit_reason = "tp"
                break

    # If not closed by SL/TP, close at last available candle
    if not trade.exit_price:
        last_idx = min(entry_idx + MAX_TRADE_DURATION, len(df) - 1)
        trade.exit_price = float(df["close"].iloc[last_idx])
        trade.exit_index = last_idx
        trade.exit_timestamp = str(df.index[last_idx])
        trade.exit_reason = "timeout"

    # Calculate PnL
    if direction == "BUY":
        trade.pnl_pct = (trade.exit_price / entry_price - 1) * 100
    else:
        trade.pnl_pct = (1 - trade.exit_price / entry_price) * 100

    cost = COMMISSION_PCT * 2 + SLIPPAGE_PCT * 2
    trade.net_pnl_pct = trade.pnl_pct - cost

    # R:R
    risk = abs(entry_price - sl)
    if risk > 0:
        if direction == "BUY":
            reward = trade.exit_price - entry_price
        else:
            reward = entry_price - trade.exit_price
        trade.rr = reward / risk

    return trade


# ── Statistics ─────────────────────────────────────────────────────────
def compute_stats(result: SymbolResult) -> dict:
    trades = result.trades
    if not trades:
        return {"symbol": result.symbol, "total": 0}

    pnls = [t.net_pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total = len(trades)
    win_count = len(wins)
    wr = win_count / total * 100 if total else 0
    avg_pnl = np.mean(pnls) if pnls else 0
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

    # Sharpe (annualized, assuming 1h bars → ~8760 bars/year)
    if len(pnls) > 1:
        sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(8760) if np.std(pnls) > 0 else 0
    else:
        sharpe = 0

    # Max drawdown
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    dd = peak - cumulative
    max_dd = np.max(dd) if len(dd) > 0 else 0

    # Exposure time
    total_bars_in_trade = sum(t.exit_index - t.entry_index for t in trades)
    total_bars = len(trades) * (trades[-1].entry_index - trades[0].entry_index + 1) if len(trades) > 1 else 1
    exposure = total_bars_in_trade / max(total_bars, 1) * 100

    # Avg trade duration
    avg_duration = np.mean([t.exit_index - t.entry_index for t in trades])

    # By direction
    buys = [t for t in trades if t.direction == "BUY"]
    sells = [t for t in trades if t.direction == "SELL"]
    buy_wr = len([t for t in buys if t.net_pnl_pct > 0]) / len(buys) * 100 if buys else 0
    sell_wr = len([t for t in sells if t.net_pnl_pct > 0]) / len(sells) * 100 if sells else 0

    # By setup type
    reversals = [t for t in trades if t.setup_type == "reversal"]
    continuations = [t for t in trades if t.setup_type == "continuation"]
    rev_wr = len([t for t in reversals if t.net_pnl_pct > 0]) / len(reversals) * 100 if reversals else 0
    cont_wr = len([t for t in continuations if t.net_pnl_pct > 0]) / len(continuations) * 100 if continuations else 0

    return {
        "symbol": result.symbol,
        "total": total,
        "wins": win_count,
        "losses": total - win_count,
        "winrate": round(wr, 1),
        "avg_pnl": round(avg_pnl, 3),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "profit_factor": round(pf, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "total_pnl": round(sum(pnls), 2),
        "avg_rr": round(np.mean([t.rr for t in trades]), 2),
        "exposure_pct": round(exposure, 1),
        "avg_duration_bars": round(avg_duration, 1),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_wr": round(buy_wr, 1),
        "sell_wr": round(sell_wr, 1),
        "reversal_count": len(reversals),
        "continuation_count": len(continuations),
        "reversal_wr": round(rev_wr, 1),
        "continuation_wr": round(cont_wr, 1),
        "signals_generated": result.signals_generated,
        "signals_rejected": result.signals_rejected,
        "rejection_reasons": result.rejection_reasons,
    }


# ── HTF scoring from cache ───────────────────────────────────────────────
def _calc_htf_score_from_cache(htf_data: dict, direction: str) -> float:
    """Compute HTF alignment score from pre-fetched data."""
    if not htf_data:
        return 0.5
    bull = "bullish" if direction in ("buy", "bullish") else "bearish"
    same = 0
    opp = 0
    for tf, df in htf_data.items():
        trend = _detect_trend_from_df(df)
        if trend == bull:
            same += 1
        elif trend != "ranging":
            opp += 1
    total = len(htf_data)
    if same == total:
        return 1.0
    if same == total - 1 and opp == 1:
        return 0.8
    if same >= 1 and opp <= 1:
        return 0.5
    if opp >= same and same >= 1:
        return 0.2
    return 0.5


# ── Main backtest ──────────────────────────────────────────────────────
async def run_symbol(symbol: str, timeframe: str, candles: int) -> SymbolResult:
    """Run new pipeline backtest for a single symbol."""
    result = SymbolResult(symbol=symbol, timeframe=timeframe)

    logger.info(f"Fetching {candles} candles for {symbol} {timeframe}...")
    raw = None
    for attempt in range(3):
        try:
            raw = await exchange_client.fetch_ohlcv_paginated(
                symbol, timeframe, total_limit=candles, page_size=998,
            )
            break
        except Exception as e:
            logger.warning(f"  Fetch attempt {attempt+1}/3 failed for {symbol}: {e}")
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))

    if raw is None or len(raw) < WARMUP + 20:
        logger.warning(f"Not enough data for {symbol}: {len(raw) if raw is not None else 0}")
        return result

    df = raw.copy()
    logger.info(f"{symbol}: {len(df)} candles, {df.index[0]} → {df.index[-1]}")

    # Pre-fetch HTF data once per symbol (W1, D1, H4)
    htf_data = {}
    for htf in ["1w", "1d", "4h"]:
        try:
            htf_df = await exchange_client.fetch_ohlcv(symbol, htf, limit=50)
            if htf_df is not None and len(htf_df) >= 20:
                htf_data[htf] = htf_df
        except Exception:
            pass

    # Pre-compute indicators
    ind_engine = IndicatorEngine()
    atr_history = []
    cooldown = 0

    for i in range(WARMUP, len(df)):
        window = df.iloc[:i + 1].copy()

        # Skip if in cooldown
        if cooldown > 0:
            cooldown -= 1
            continue

        # Compute indicators
        try:
            ind = ind_engine.calculate(window, symbol, timeframe)
        except Exception:
            continue

        if ind is None or ind.atr is None or ind.atr <= 0:
            continue

        atr_history.append(ind.atr)
        if len(atr_history) > 100:
            atr_history.pop(0)

        # Current candle data
        current_price = float(ind.close)

        # ── Phase 1: Liquidity + Structure ──
        try:
            sweeps = detect_sweeps(window, lookback=50)
        except Exception:
            sweeps = []
        try:
            order_blocks = detect_order_blocks(window, lookback=100)
        except Exception:
            order_blocks = []

        # MSS classification params
        try:
            fvgs = detect_fvg(window, lookback=100)
        except Exception:
            fvgs = []
        try:
            candle_quality = analyze_last_candle(window, atr_value=ind.atr)
        except Exception:
            candle_quality = None

        # MSS classification params
        _disp_atr = 0.0
        _reclaim = 0
        if candle_quality and ind.atr and ind.atr > 0:
            _disp_atr = candle_quality.body_atr_ratio if hasattr(candle_quality, 'body_atr_ratio') else 0.0
        valid_sw = [s for s in sweeps if s.is_valid] if sweeps else []
        if valid_sw:
            _reclaim = valid_sw[0].reclaim_candles

        try:
            structure = analyze_structure(
                window, lookback=50,
                sweeps=sweeps,
                displacement_atr=_disp_atr,
                reclaim_bars=_reclaim,
                atr_value=ind.atr if ind.atr else 0.0,
            )
        except Exception:
            structure = None

        # ── Phase 2: Pattern Engine ──
        setup = pattern_engine.detect(
            sweeps=sweeps,
            order_blocks=order_blocks,
            structure=structure,
            fvgs=fvgs,
            candle_quality=candle_quality,
            current_price=current_price,
            atr=ind.atr,
        )

        if not setup.detected:
            result.signals_rejected += 1
            reason = setup.rejection_reason or "no_setup"
            result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1
            continue

        result.signals_generated += 1

        # ── Phase 1.45: HTF Bias Hard Gate ──
        _htf_bias_penalty = 1.0
        try:
            _struct_1d = extract_structure_dict(structure) if structure else None
            htf_bias = get_htf_bias(
                df_1d=None, df_4h=None,
                structure_1d=_struct_1d, structure_4h=None,
            )
            if htf_bias != HTFBias.NEUTRAL:
                direction_map = {"buy": HTFBias.BULLISH, "sell": HTFBias.BEARISH}
                setup_bias = direction_map.get(setup.direction)
                if setup_bias != htf_bias:
                    if setup.setup_type == "continuation":
                        result.signals_rejected += 1
                        result.rejection_reasons[f"htf_bias_{setup.direction}_vs_{htf_bias.value}"] = \
                            result.rejection_reasons.get(f"htf_bias_{setup.direction}_vs_{htf_bias.value}", 0) + 1
                        continue
                    elif setup.setup_type == "reversal":
                        _htf_bias_penalty = 0.85
        except Exception:
            pass

        # ── Asset-type directional filter: block SHORT on L1 tokens ──
        if setup.direction.lower() == "sell" and ASSET_TYPES.get(symbol) == "L1":
            result.signals_rejected += 1
            result.rejection_reasons["l1_short_blocked"] = \
                result.rejection_reasons.get("l1_short_blocked", 0) + 1
            continue

        # ── Phase 1.5: Build Trade Plan (SL/TP) ──
        try:
            from strategy.trade_engine import trade_engine
            trade_plan = trade_engine.build_trade_plan(
                ind=ind,
                direction=setup.direction,
                structure=structure,
                order_blocks=order_blocks,
                sweeps=sweeps,
                fvgs=fvgs,
                df=window,
                timeframe=timeframe,
            )
            sl = trade_plan.sl
            tp = trade_plan.tp
        except Exception as e:
            logger.debug(f"Trade plan failed: {e}")
            result.signals_rejected += 1
            result.rejection_reasons["trade_plan_failed"] = result.rejection_reasons.get("trade_plan_failed", 0) + 1
            continue

        if sl is None or tp is None:
            result.signals_rejected += 1
            result.rejection_reasons["sl_tp_failed"] = result.rejection_reasons.get("sl_tp_failed", 0) + 1
            continue

        # ── Phase 3: HTF Alignment + Premium/Discount scores ──
        htf_score = None
        pd_score = None
        try:
            htf_score = _calc_htf_score_from_cache(htf_data, setup.direction)
        except Exception:
            pass
        try:
            pd_score = calc_premium_discount_score(window, setup.direction)
        except Exception:
            pass

        # ── Phase 3.5: Feature Builder ──
        try:
            features = feature_builder.build(
                setup=setup,
                ind=ind,
                structure=structure,
                regime=None,
                vol_regime=None,
                mtf_aligned=False,
                mtf_count=0,
                context_score=0.0,
                fear_greed=None,
                funding_rate=None,
                sl=sl,
                tp=tp,
                entry_price=current_price,
                candle_quality=candle_quality,
                htf_alignment_score=htf_score,
                premium_discount_score=pd_score,
                htf_bias_penalty=_htf_bias_penalty,
            )
        except Exception as e:
            logger.debug(f"Feature builder failed: {e}")
            continue

        # ── Phase 4: Probability Engine ──
        try:
            probability = probability_engine.predict(features)
        except Exception as e:
            logger.debug(f"Probability engine failed: {e}")
            continue

        if probability.p_tp < 0.40:
            result.signals_rejected += 1
            result.rejection_reasons["low_p_tp"] = result.rejection_reasons.get("low_p_tp", 0) + 1
            continue

        # ── Phase 5: Risk Engine ──
        try:
            portfolio = PortfolioState(
                active_count=0,
                total_risk_pct=0.0,
            )
            decision = risk_engine.evaluate(
                features=features,
                probability=probability,
                portfolio=portfolio,
                entry_price=current_price,
                sl=sl,
                tp=tp,
                mss_quality=setup.mss_score if setup.has_mss else 0.0,
                sl_source=trade_plan.sl_source,
            )
        except Exception as e:
            logger.debug(f"Risk engine failed: {e}")
            continue

        if not decision.should_trade:
            result.signals_rejected += 1
            reason = decision.rejection_reason or "risk_blocked"
            result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1
            continue

        final_sl = decision.sl_price if decision.sl_price else sl
        final_tp = decision.tp_price if decision.tp_price else tp

        # ── Execute simulated trade ──
        trade = simulate_trade(
            direction=setup.direction.upper(),
            entry_price=current_price,
            sl=final_sl,
            tp=final_tp,
            df=df,
            entry_idx=i,
            symbol=symbol,
            setup_type=setup.setup_type or "unknown",
            p_tp=probability.p_tp,
            mss_score=setup.mss_score if setup.has_mss else 0.0,
            regime=structure.trend if structure else "unknown",
        )

        result.trades.append(trade)
        cooldown = COOLDOWN_BARS

        logger.debug(
            f"  {symbol} [{setup.setup_type}] {setup.direction.upper()} "
            f"@ {current_price:.2f} SL={decision.sl_price:.2f} TP={decision.tp_price:.2f} "
            f"→ {trade.exit_reason} PnL={trade.net_pnl_pct:+.2f}%"
        )

    return result


async def main():
    candles = DAYS * 24 + WARMUP + 50  # 90d * 24h + warmup + buffer

    logger.info(f"═══ New Pipeline Backtest ═══")
    logger.info(f"Symbols: {', '.join(SYMBOLS)}")
    logger.info(f"Timeframe: {TIMEFRAME} | Days: {DAYS} | Candles: {candles}")
    logger.info(f"Commission: {COMMISSION_PCT}% | Slippage: {SLIPPAGE_PCT}%")
    logger.info("")

    # Connect to exchange
    await exchange_client.connect()
    logger.info("Exchange connected.")
    logger.info("")

    results = []
    all_trades = []

    for idx, symbol in enumerate(SYMBOLS):
        if idx > 0:
            await asyncio.sleep(3)  # rate limit buffer between symbols
        t0 = time.time()
        res = await run_symbol(symbol, TIMEFRAME, candles)
        elapsed = time.time() - t0
        stats = compute_stats(res)
        results.append(stats)
        all_trades.extend(res.trades)

        logger.info(
            f"  {symbol}: {stats['total']} trades | "
            f"WR={stats.get('winrate', 0)}% | "
            f"PF={stats.get('profit_factor', 0)} | "
            f"PnL={stats.get('total_pnl', 0):+.2f}% | "
            f"({elapsed:.1f}s)"
        )

    # ── Aggregate stats ────────────────────────────────────────────────
    logger.info("")
    logger.info("═══ AGGREGATE ═══")

    total_trades = sum(s["total"] for s in results)
    total_wins = sum(s.get("wins", 0) for s in results)
    total_losses = sum(s.get("losses", 0) for s in results)
    overall_wr = total_wins / total_trades * 100 if total_trades else 0

    all_pnls = [t.net_pnl_pct for t in all_trades]
    total_pnl = sum(all_pnls)
    avg_pnl = np.mean(all_pnls) if all_pnls else 0

    wins_pnl = [p for p in all_pnls if p > 0]
    losses_pnl = [p for p in all_pnls if p <= 0]
    overall_pf = (sum(wins_pnl) / abs(sum(losses_pnl))) if losses_pnl and sum(losses_pnl) != 0 else float("inf")

    if len(all_pnls) > 1 and np.std(all_pnls) > 0:
        overall_sharpe = np.mean(all_pnls) / np.std(all_pnls) * np.sqrt(8760)
    else:
        overall_sharpe = 0

    # Setup type breakdown
    rev_trades = [t for t in all_trades if t.setup_type == "reversal"]
    cont_trades = [t for t in all_trades if t.setup_type == "continuation"]
    cont_buys = [t for t in cont_trades if t.direction == "BUY"]
    cont_sells = [t for t in cont_trades if t.direction == "SELL"]

    def _calc_group_stats(trades):
        if not trades:
            return {"count": 0, "wr": 0, "pf": 0, "avg_rr": 0}
        pnls = [t.net_pnl_pct for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(trades) * 100
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
        avg_rr = np.mean([t.rr for t in trades]) if trades else 0
        return {"count": len(trades), "wr": round(wr, 1), "pf": round(pf, 2), "avg_rr": round(avg_rr, 2)}

    rev_stats = _calc_group_stats(rev_trades)
    cont_buy_stats = _calc_group_stats(cont_buys)
    cont_sell_stats = _calc_group_stats(cont_sells)
    cont_all_stats = _calc_group_stats(cont_trades)

    logger.info(f"  Total trades:  {total_trades}")
    logger.info(f"  Wins/Losses:   {total_wins}/{total_losses}")
    logger.info(f"  Winrate:       {overall_wr:.1f}%")
    logger.info(f"  Profit Factor: {overall_pf:.2f}")
    logger.info(f"  Sharpe Ratio:  {overall_sharpe:.2f}")
    logger.info(f"  Total PnL:     {total_pnl:+.2f}%")
    logger.info(f"  Avg PnL/trade: {avg_pnl:+.3f}%")
    logger.info("")
    logger.info("  Setup Type Breakdown:")
    logger.info(f"    {'Type':<25} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Avg R':>6}")
    logger.info(f"    {'─'*25} {'─'*6} {'─'*6} {'─'*6} {'─'*6}")
    logger.info(f"    {'MSS Reversal':<25} {rev_stats['count']:>6} {rev_stats['wr']:>5.1f}% {rev_stats['pf']:>6.2f} {rev_stats['avg_rr']:>6.2f}")
    logger.info(f"    {'BOS Continuation LONG':<25} {cont_buy_stats['count']:>6} {cont_buy_stats['wr']:>5.1f}% {cont_buy_stats['pf']:>6.2f} {cont_buy_stats['avg_rr']:>6.2f}")
    logger.info(f"    {'BOS Continuation SHORT':<25} {cont_sell_stats['count']:>6} {cont_sell_stats['wr']:>5.1f}% {cont_sell_stats['pf']:>6.2f} {cont_sell_stats['avg_rr']:>6.2f}")
    logger.info(f"    {'BOS Continuation ALL':<25} {cont_all_stats['count']:>6} {cont_all_stats['wr']:>5.1f}% {cont_all_stats['pf']:>6.2f} {cont_all_stats['avg_rr']:>6.2f}")

    logger.info("")
    logger.info("═══ PER-SYMBOL ═══")
    header = f"{'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL%':>8} {'Sharpe':>7} {'MaxDD':>6} {'Rev':>4} {'Cont':>4}"
    logger.info(header)
    logger.info("─" * len(header))
    for s in results:
        logger.info(
            f"{s['symbol']:<12} {s['total']:>6} {s.get('winrate', 0):>5.1f}% "
            f"{s.get('profit_factor', 0):>6.2f} {s.get('total_pnl', 0):>+7.2f}% "
            f"{s.get('sharpe', 0):>7.2f} {s.get('max_drawdown', 0):>5.2f}% "
            f"{s.get('reversal_count', 0):>4} {s.get('continuation_count', 0):>4}"
        )

    # Rejection reasons
    logger.info("")
    logger.info("═══ REJECTION REASONS ═══")
    agg_reasons = {}
    for s in results:
        for reason, count in s.get("rejection_reasons", {}).items():
            # Handle dict or object
            if isinstance(count, dict):
                for r, c in count.items():
                    agg_reasons[r] = agg_reasons.get(r, 0) + c
            else:
                agg_reasons[reason] = agg_reasons.get(reason, 0) + count
    for reason, count in sorted(agg_reasons.items(), key=lambda x: -x[1]):
        logger.info(f"  {reason:<30} {count:>5}")

    # Save report
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports", "new_pipeline_backtest.md",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    def _group_stats(trades):
        if not trades:
            return {"count": 0, "wr": 0.0, "pf": 0.0, "avg_rr": 0.0, "pnl": 0.0}
        pnls = [t.net_pnl_pct for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(trades) * 100
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
        avg_rr = float(np.mean([t.rr for t in trades]))
        return {
            "count": len(trades),
            "wr": round(wr, 1),
            "pf": round(pf, 2),
            "avg_rr": round(avg_rr, 2),
            "pnl": round(sum(pnls), 2),
        }

    lines = []
    w = lines.append
    w(f"# New Pipeline Backtest — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    w(f"**Symbols:** {', '.join(SYMBOLS)}")
    w(f"**Timeframe:** {TIMEFRAME} | **Days:** {DAYS}")
    w(f"**Commission:** {COMMISSION_PCT}% | **Slippage:** {SLIPPAGE_PCT}%\n")

    # ── Aggregate ──
    w("## Aggregate\n")
    w("| Metric | Value |")
    w("|---|---|")
    w(f"| Total trades | {total_trades} |")
    w(f"| Winrate | {overall_wr:.1f}% |")
    w(f"| Profit Factor | {overall_pf:.2f} |")
    w(f"| Sharpe Ratio | {overall_sharpe:.2f} |")
    w(f"| Total PnL (net) | {total_pnl:+.2f}% |\n")

    # ── Setup Type Breakdown (aggregate) ──
    w("## Setup Type Breakdown (aggregate)\n")
    w("| Setup | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|")
    for label, grp in [
        ("MSS Reversal", rev_trades),
        ("BOS Continuation LONG", cont_buys),
        ("BOS Continuation SHORT", cont_sells),
        ("BOS Continuation ALL", cont_trades),
    ]:
        s = _group_stats(grp)
        w(f"| {label} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── Per-Symbol Overview ──
    w("\n## Per-Symbol Overview\n")
    w("| Symbol | Trades | WR% | PF | PnL% | Sharpe | MaxDD | Rev | Cont |")
    w("|---|---|---|---|---|---|---|---|---|")
    for s in results:
        w(
            f"| {s['symbol']} | {s['total']} | {s.get('winrate', 0)}% | "
            f"{s.get('profit_factor', 0)} | {s.get('total_pnl', 0):+.2f}% | "
            f"{s.get('sharpe', 0)} | {s.get('max_drawdown', 0):.2f}% | "
            f"{s.get('reversal_count', 0)} | {s.get('continuation_count', 0)} |"
        )

    # ── Per-Symbol × Setup Type ──
    w("\n## Per-Symbol × Setup Type\n")
    w("| Symbol | Setup | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|---|")
    for symbol in SYMBOLS:
        sym_trades = [t for t in all_trades if t.symbol == symbol]
        for label, filt in [
            ("MSS Reversal", lambda t: t.setup_type == "reversal"),
            ("BOS LONG", lambda t: t.setup_type == "continuation" and t.direction == "BUY"),
            ("BOS SHORT", lambda t: t.setup_type == "continuation" and t.direction == "SELL"),
        ]:
            grp = [t for t in sym_trades if filt(t)]
            s = _group_stats(grp)
            if s["count"] > 0:
                w(f"| {symbol} | {label} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── MSS Score Bucket Attribution ──
    w("\n## MSS Score Bucket Attribution\n")
    buckets = [(0, 30), (30, 50), (50, 70), (70, 100)]
    w("| Bucket | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|")
    for lo, hi in buckets:
        grp = [t for t in all_trades if lo <= t.mss_score < hi]
        s = _group_stats(grp)
        if s["count"] > 0:
            w(f"| {lo}-{hi} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── Regime Attribution ──
    w("\n## Regime Attribution\n")
    w("| Regime | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|")
    for regime_label in ["bullish", "bearish", "ranging"]:
        grp = [t for t in all_trades if t.regime == regime_label]
        s = _group_stats(grp)
        if s["count"] > 0:
            w(f"| {regime_label} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── Regime × Setup Type ──
    w("\n## Regime × Setup Type\n")
    w("| Regime | Setup | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|---|")
    for regime_label in ["bullish", "bearish", "ranging"]:
        for setup_label, filt in [
            ("MSS Reversal", lambda t: t.setup_type == "reversal"),
            ("BOS LONG", lambda t: t.setup_type == "continuation" and t.direction == "BUY"),
            ("BOS SHORT", lambda t: t.setup_type == "continuation" and t.direction == "SELL"),
        ]:
            grp = [t for t in all_trades if t.regime == regime_label and filt(t)]
            s = _group_stats(grp)
            if s["count"] > 0:
                w(f"| {regime_label} | {setup_label} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── Asset Type Clustering ──
    w("\n## Asset Type Clustering\n")
    w("| Asset Type | Symbols | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|---|")
    type_groups = {}
    for symbol in SYMBOLS:
        at = ASSET_TYPES.get(symbol, "other")
        type_groups.setdefault(at, []).append(symbol)
    for at, syms in sorted(type_groups.items()):
        grp = [t for t in all_trades if t.symbol in syms]
        s = _group_stats(grp)
        w(f"| {at} | {', '.join(syms)} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── MSS by Asset Type ──
    w("\n## MSS Reversal by Asset Type\n")
    w("| Asset Type | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|")
    for at, syms in sorted(type_groups.items()):
        grp = [t for t in all_trades if t.symbol in syms and t.setup_type == "reversal"]
        s = _group_stats(grp)
        if s["count"] > 0:
            w(f"| {at} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── BOS by Asset Type × Direction ──
    w("\n## BOS Continuation by Asset Type × Direction\n")
    w("| Asset Type | Dir | Trades | WR% | PF | Avg R | PnL% |")
    w("|---|---|---|---|---|---|---|")
    for at, syms in sorted(type_groups.items()):
        for direction, label in [("BUY", "LONG"), ("SELL", "SHORT")]:
            grp = [t for t in all_trades if t.symbol in syms and t.setup_type == "continuation" and t.direction == direction]
            s = _group_stats(grp)
            if s["count"] > 0:
                w(f"| {at} | {label} | {s['count']} | {s['wr']}% | {s['pf']} | {s['avg_rr']:+.2f} | {s['pnl']:+.2f}% |")

    # ── Rejection Reasons ──
    w("\n## Rejection Reasons\n")
    w("| Reason | Count |")
    w("|---|---|")
    for reason, count in sorted(agg_reasons.items(), key=lambda x: -x[1]):
        w(f"| {reason} | {count} |")

    report_text = "\n".join(lines) + "\n"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    logger.info(f"\nReport saved: {report_path}")
    print(report_text)

    await exchange_client.close()


if __name__ == "__main__":
    asyncio.run(main())
