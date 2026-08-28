"""
strategy/trade_engine.py — Trade Engine: ICT-based trade planning.

Replaces _calculate_sl_tp() with a liquidity-first approach:
1. Map liquidity (where are the stops?)
2. Find the idea (what's the setup?)
3. Find invalidation (where does the idea break?)
4. Find targets (where is the liquidity going?)
5. Calculate RR (is it worth it?)
6. Choose entry (optimize for RR)

ICT flow: Liquidity → Idea → Invalidation → Target → Entry → RR → Execute?
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd
from loguru import logger

from config.settings import config
from indicators.engine import IndicatorValues
from strategy.signal_engine import SignalType
from strategy.trade_plan import TradePlan, TargetScore
from strategy.invalidation import (
    Invalidation, find_invalidation_buy, find_invalidation_sell,
)
from liquidity.pool import LiquidityMap, build_liquidity_map, LiquidityLevel
from market_structure.structure import StructureState


class TradeEngine:
    """Builds TradePlans using ICT liquidity-first methodology."""

    def build_trade_plan(
        self,
        ind: IndicatorValues,
        direction: str,  # "buy" or "sell"
        structure: Optional[StructureState] = None,
        order_blocks: Optional[list] = None,
        sweeps: Optional[list] = None,
        fvgs: Optional[list] = None,
        df: Optional[pd.DataFrame] = None,
        timeframe: str = "1h",
    ) -> TradePlan:
        """Build a complete trade plan.

        This is the main entry point — replaces _calculate_sl_tp().
        """
        cfg = config.trading
        entry = float(ind.close)
        atr = ind.atr if ind.atr and ind.atr > 0 else entry * cfg.atr_fallback_pct / 100
        signal = SignalType.BUY if direction == "buy" else SignalType.SELL

        # ═══ FVG ENTRY: use median (50%) of active FVG as entry price ═══
        if fvgs:
            fvg_proximity_pct = config.pattern_engine.fvg_proximity_pct
            for f in fvgs:
                f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
                if f.is_active and f_dir == direction:
                    fvg_median = (f.top + f.bottom) / 2.0
                    dist_pct = abs(fvg_median - entry) / entry * 100
                    if dist_pct > fvg_proximity_pct:
                        logger.debug(
                            f"FVG entry skipped: median {fvg_median:.4f} is "
                            f"{dist_pct:.1f}% from price {entry:.4f} "
                            f"(max {fvg_proximity_pct}%)"
                        )
                        continue
                    entry = round(fvg_median, 8)
                    logger.debug(
                        f"FVG entry: using median {entry:.4f} "
                        f"(top={f.top:.4f}, bottom={f.bottom:.4f}, type={f.type}, "
                        f"dist={dist_pct:.1f}%)"
                    )
                    break

        # Per-TF ATR overrides
        atr_sl = cfg.atr_multiplier_sl
        atr_tp = cfg.atr_multiplier_tp
        if timeframe and cfg.atr_multipliers_per_tf:
            tf_override = cfg.atr_multipliers_per_tf.get(timeframe, {})
            if "sl" in tf_override:
                atr_sl = float(tf_override["sl"])
            if "tp" in tf_override:
                atr_tp = float(tf_override["tp"])

        # ═══ Step 1: Build Liquidity Map ═══

        swing_highs = getattr(structure, "swing_points", []) or []
        swing_lows = getattr(structure, "swing_points", []) or []
        recent_highs = getattr(structure, "recent_highs", []) or []
        recent_lows = getattr(structure, "recent_lows", []) or []

        # For liquidity map, we use recent highs/lows as swing points
        # (they're already detected by market_structure)
        from market_structure.structure import SwingPoint
        from datetime import datetime

        sh_points = [SwingPoint(price=p, timestamp=datetime.now(), type="high") for p in recent_highs]
        sl_points = [SwingPoint(price=p, timestamp=datetime.now(), type="low") for p in recent_lows]

        liq_map = build_liquidity_map(
            symbol=ind.symbol,
            timeframe=timeframe,
            current_price=entry,
            swing_highs=sh_points,
            swing_lows=sl_points,
            order_blocks=order_blocks or [],
            fvgs=fvgs or [],
            sweeps=sweeps or [],
            df=df,
        )

        # ═══ Step 2: Find Invalidation (SL) ═══

        # Collect levels for invalidation
        sweep_lows = [s.sweep_low for s in (sweeps or []) if s.type == "bullish"]
        sweep_highs = [s.sweep_high for s in (sweeps or []) if s.type == "bearish"]
        ob_lows = [ob.low for ob in (order_blocks or []) if ob.type == "bullish"]
        ob_highs = [ob.high for ob in (order_blocks or []) if ob.type == "bearish"]
        bos_level = structure.last_bos.level if structure and structure.last_bos else None

        if signal == SignalType.BUY:
            invalidation = find_invalidation_buy(
                entry=entry,
                sweep_lows=sweep_lows,
                ob_lows=ob_lows,
                swing_lows=recent_lows,
                bos_level=bos_level,
                atr=atr,
            )
        else:
            invalidation = find_invalidation_sell(
                entry=entry,
                sweep_highs=sweep_highs,
                ob_highs=ob_highs,
                swing_highs=recent_highs,
                bos_level=bos_level,
                atr=atr,
            )

        if invalidation is None:
            return TradePlan(
                direction=direction,
                symbol=ind.symbol,
                timeframe=timeframe,
                entry_price=entry,
                is_valid=False,
                rejection_reason="no invalidation level found",
            )

        # SL = invalidation level + buffer (ATR-based)
        sl_buffer = atr * 0.15  # 15% of ATR as buffer
        if signal == SignalType.BUY:
            sl = round(invalidation.level - sl_buffer, 8)
        else:
            sl = round(invalidation.level + sl_buffer, 8)

        # ═══ SL SAFETY: ensure SL is beyond current candle's wick ═══
        # If the current candle's low already breached the SL, the outcome
        # tracker would immediately resolve it as HIT_SL on the same bar.
        # Push SL below candle low (BUY) or above candle high (SELL).
        # Buffer = spread + tick_size + ATR * 5%
        if df is not None and len(df) > 0:
            last_candle = df.iloc[-1]
            candle_low = float(last_candle["low"])
            candle_high = float(last_candle["high"])

            # Spread buffer: ~0.01% of entry (typical taker spread)
            spread_buffer = entry * 0.0001
            # Tick size: get from exchange market info if available
            tick_buffer = 0.0
            try:
                from data.exchange_client import exchange_client
                market = exchange_client._exchange.markets.get(ind.symbol, {})
                price_precision = market.get("precision", {}).get("price")
                if price_precision is not None:
                    tick_buffer = 10 ** (-price_precision)
            except Exception:
                pass
            # ATR buffer: 5% of ATR
            atr_buffer = atr * 0.05

            total_buffer = spread_buffer + tick_buffer + atr_buffer

            if signal == SignalType.BUY and sl >= candle_low:
                sl = round(candle_low - total_buffer, 8)
                logger.debug(
                    f"SL adjusted below candle low: {sl:.4f} "
                    f"(candle_low={candle_low:.4f}, spread={spread_buffer:.6f}, "
                    f"tick={tick_buffer:.6f}, atr_buf={atr_buffer:.6f})"
                )
            elif signal == SignalType.SELL and sl <= candle_high:
                sl = round(candle_high + total_buffer, 8)
                logger.debug(
                    f"SL adjusted above candle high: {sl:.4f} "
                    f"(candle_high={candle_high:.4f}, spread={spread_buffer:.6f}, "
                    f"tick={tick_buffer:.6f}, atr_buf={atr_buffer:.6f})"
                )

        # ═══ Step 3: Find Targets (TP) ═══

        targets = self._find_targets(
            signal=signal,
            entry=entry,
            atr=atr,
            liq_map=liq_map,
            invalidation_level=invalidation.level,
            atr_tp=atr_tp,
        )

        # Diagnostic: log liquidity map composition
        _liq_above = len(liq_map.targets_above)
        _liq_below = len(liq_map.targets_below)
        _liq_total = len(liq_map.levels)
        _eq_count = len(liq_map.equal_levels)
        _ext_count = len(liq_map.external_levels)
        _sweep_count = len([l for l in liq_map.levels if l.type.startswith("swept")])
        _ob_count = len([l for l in liq_map.levels if l.type.startswith("ob_")])
        _fvg_count = len([l for l in liq_map.levels if l.type.startswith("fvg_")])
        logger.debug(
            f"Liquidity map: {_liq_total} levels "
            f"(eq={_eq_count} ext={_ext_count} sweep={_sweep_count} ob={_ob_count} fvg={_fvg_count}) "
            f"above={_liq_above} below={_liq_below}"
        )

        if not targets:
            # No valid targets — _find_targets already added ATR fallback
            logger.debug(
                f"No liquidity targets found for {direction} at {entry:.4f} "
                f"(liq_map has {_liq_total} levels, above={_liq_above} below={_liq_below})"
            )

        # ═══ Step 4: Choose Best Target ═══

        best = max(targets, key=lambda t: t.score)
        tp = best.level
        tp_source = best.type

        # ═══ Step 5: Calculate Final RR ═══

        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        rr = tp_dist / sl_dist if sl_dist > 0 else 0

        # ═══ Step 6: Validate ═══

        min_rr = cfg.min_rr_threshold
        is_valid = True
        rejection = ""

        if rr < min_rr:
            # Don't block — just note it. TradeEngine provides the plan,
            # the Risk Engine decides whether to execute.
            rejection = f"RR {rr:.1f} < min {min_rr}"

        # Liquidity summary
        liq_above = len(liq_map.targets_above)
        liq_below = len(liq_map.targets_below)
        liq_summary = f"{liq_above} targets above, {liq_below} below"

        return TradePlan(
            direction=direction,
            symbol=ind.symbol,
            timeframe=timeframe,
            entry_price=entry,
            entry_zone=(entry - atr * 0.3, entry + atr * 0.3),
            invalidation=invalidation,
            targets=sorted(targets, key=lambda t: t.score, reverse=True),
            sl=sl,
            tp=tp,
            sl_source=invalidation.type,
            tp_source=tp_source,
            rr_ratio=round(rr, 2),
            sl_distance_pct=round(sl_dist / entry * 100, 2),
            tp_distance_pct=round(tp_dist / entry * 100, 2),
            liquidity_summary=liq_summary,
            is_valid=is_valid,
            rejection_reason=rejection,
        )

    def _find_targets(
        self,
        signal: SignalType,
        entry: float,
        atr: float,
        liq_map: LiquidityMap,
        invalidation_level: float,
        atr_tp: float,
    ) -> list[TargetScore]:
        """Find and score all potential TP targets.

        Priority chain (via type bonus):
          1. External Liquidity (old_high/old_low — highest bonus)
          2. Equal Highs/Lows (equal_high/equal_low)
          3. Opposing OB (ob_bullish/ob_bearish)
          4. Active FVG (fvg_bullish/fvg_bearish)
          5. Swept levels
          6. ATR fallback
        """
        type_bonus = {
            "old_high": 2.0,
            "old_low": 2.0,
            "equal_high": 1.8,
            "equal_low": 1.8,
            "ob_bullish": 1.5,
            "ob_bearish": 1.5,
            "fvg_bullish": 1.2,
            "fvg_bearish": 1.2,
            "swept_high": 0.5,
            "swept_low": 0.5,
        }

        targets = []
        min_distance = atr * 0.5  # minimum TP distance = 0.5 ATR (relaxed from 1.0)

        if signal == SignalType.BUY:
            candidates = liq_map.targets_above
        else:
            candidates = liq_map.targets_below

        _filtered_close = 0
        for level in candidates:
            dist = abs(level.level - entry)
            if dist < min_distance:
                _filtered_close += 1
                continue  # too close

            # Distance as %
            dist_pct = dist / entry * 100

            # RR if this is the target
            sl_dist = abs(entry - invalidation_level)
            rr = dist / max(sl_dist, atr * 0.5)

            # Path clarity (simplified — check if any opposing levels in between)
            path_clear = self._check_path(entry, level.level, liq_map, signal)

            # Apply type bonus for priority
            bonus = type_bonus.get(level.type, 1.0)
            adjusted_strength = level.strength * bonus

            targets.append(TargetScore(
                level=level.level,
                type=level.type,
                strength=adjusted_strength,
                distance_pct=round(dist_pct, 2),
                rr_ratio=round(rr, 2),
                path_clear=path_clear,
                source_label=f"{level.type} @ {level.level:.4f}",
            ))

        if not targets and candidates:
            logger.debug(
                f"No targets passed min_distance filter: "
                f"{len(candidates)} candidates, {_filtered_close} too close "
                f"(min={min_distance:.2f}, entry={entry:.4f})"
            )

        # ATR fallback: if no liquidity targets found, use ATR-scaled targets
        if not targets:
            if signal == SignalType.BUY:
                tp_atr = round(entry + atr * atr_tp, 8)
            else:
                tp_atr = round(entry - atr * atr_tp, 8)
            sl_dist = abs(entry - invalidation_level)
            rr_atr = abs(tp_atr - entry) / max(sl_dist, atr * 0.5)
            targets.append(TargetScore(
                level=tp_atr,
                type="atr",
                strength=0.3,
                distance_pct=round(abs(tp_atr - entry) / entry * 100, 2),
                rr_ratio=round(rr_atr, 2),
                path_clear=True,
                source_label=f"ATR fallback ({atr_tp}x)",
            ))

        return targets

    def _check_path(
        self,
        entry: float,
        target: float,
        liq_map: LiquidityMap,
        signal: SignalType,
    ) -> bool:
        """Check if the path from entry to target is clear of opposing liquidity."""
        # Simplified: check if there's a strong opposing level in between
        if signal == SignalType.BUY:
            # For BUY, opposing = bearish levels above entry but below target
            opposing = [
                l for l in liq_map.levels
                if l.level > entry and l.level < target
                and l.type in ("ob_bearish", "fvg_bearish")
                and l.strength > 0.5
            ]
        else:
            # For SELL, opposing = bullish levels below entry but above target
            opposing = [
                l for l in liq_map.levels
                if l.level < entry and l.level > target
                and l.type in ("ob_bullish", "fvg_bullish")
                and l.strength > 0.5
            ]

        return len(opposing) == 0


# Singleton
trade_engine = TradeEngine()
