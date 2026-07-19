"""
strategy/signal_engine.py — Data types + SL/TP calculation.

ICT Core: all indicator-based gates removed.
Pattern Engine (pattern_engine.py) is the sole source of trading signals.
"""
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Literal, Optional, Dict, Any

from loguru import logger

from config.settings import config
from indicators.engine import IndicatorValues
from scoring.confidence_v2 import ConfidenceResult, FactorScore


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_SIGNAL = "NO_SIGNAL"


@dataclass
class SignalResult:
    signal: SignalType
    symbol: str
    timeframe: str
    close: float
    entry_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    score: int = 0
    sr_levels: Optional[Dict[str, Dict[str, List[float]]]] = None
    level_warnings: List[str] = field(default_factory=list)
    _context_score: Optional[float] = None
    _max_context_score: float = 1.0
    _confirmed_tf: Optional[str] = None
    _context_items: List[str] = field(default_factory=list)
    _structure_trend: Optional[str] = None
    _structure_bos: Optional[str] = None
    _tp_path_score: int = 0
    _tp_path_blocked: bool = False
    _distance_filter_blocked: bool = False
    _confidence_v2: Optional[ConfidenceResult] = None
    _ema_alignment_info: str = ""
    _rsi_strength: float = 0.0
    _factor_strengths: Dict[str, float] = field(default_factory=dict)
    _weighted_score: float = 0.0
    _has_trigger: bool = False
    _has_leading_trigger: bool = False
    _regime: Optional[str] = None
    _regime_blocked: bool = False
    _sl_source: Optional[Literal["bos", "atr"]] = None
    tp_source: str = ""  # external_liquidity | opposing_ob | active_fvg | swing | atr
    _swing_highs_1h: List[float] = field(default_factory=list)
    _swing_lows_1h: List[float] = field(default_factory=list)
    _rejection_reason: Optional[str] = None

    # Phase 2 — HTF Bias V2 + Premium/Discount
    _htf_result: Optional[Any] = None  # HTFBiasResult
    _zone_type: Optional[str] = None   # "premium" / "discount" / "equilibrium"
    _fib_level: Optional[float] = None
    _zone_quality_multiplier: float = 1.0

    @property
    def is_actionable(self) -> bool:
        return self.signal != SignalType.NO_SIGNAL

    @property
    def score_verdict(self) -> str:
        """Quality label from component count (ICT setups)."""
        if self._regime_blocked:
            return "blocked"
        s = self.score
        if s >= 6:
            return "strong"
        elif s >= 4:
            return "moderate"
        else:
            return "weak"

    @property
    def score_verdict_display(self) -> str:
        return {
            "strong": "СИЛЬНЫЙ", "moderate": "УМЕРЕННЫЙ",
            "weak": "СЛАБЫЙ", "blocked": "ЗАБЛОКИРОВАН",
        }.get(self.score_verdict, "СЛАБЫЙ")

    @property
    def confidence_verdict(self) -> str:
        if self._regime_blocked:
            return "ЗАБЛОКИРОВАН"
        if self._confidence_v2 is not None:
            q = self._confidence_v2.quality
            return {"strong": "СИЛЬНЫЙ", "moderate": "УМЕРЕННЫЙ", "weak": "СЛАБЫЙ"}.get(q, "СЛАБЫЙ")
        return self.score_verdict_display

    @property
    def verdict(self) -> str:
        """Backward-compatible alias — used in DB and outcome_tracker."""
        return self.confidence_verdict

    @property
    def confidence(self) -> float:
        if self._confidence_v2 is not None:
            return self._confidence_v2.confidence_pct
        max_score = 7
        tech_pct = self.score / max_score
        if self._context_score is not None:
            market_pct = (self._context_score + 1.0) / 2.0
            blend = config.scoring.tech_confidence_blend
            confidence = (tech_pct * blend) + (market_pct * (1.0 - blend))
        else:
            confidence = tech_pct
        return round(confidence * 100, 1)

    def format_message(self) -> str:
        if self.signal == SignalType.BUY:
            signal_word = "ПОКУПКА"
        elif self.signal == SignalType.SELL:
            signal_word = "ПРОДАЖА"
        else:
            signal_word = ""
        header = f"{self.signal.value} — {signal_word}" if signal_word else self.signal.value
        emoji = "\U0001f7e2" if self.signal == SignalType.BUY else "\U0001f534"
        lines = [
            f"{emoji} {header} — {self.symbol}",
        ]

        # HTF Context (Phase 2)
        if self._htf_result is not None:
            hr = self._htf_result
            strength_label = hr.strength.value.upper() if hasattr(hr.strength, 'value') else str(hr.strength)
            tf_checks = []
            if hr.weekly_bias != 'neutral':
                tf_checks.append(f"W1{'\u2713' if hr.weekly_bias == hr.direction else '\u2717'}")
            if hr.daily_bias != 'neutral':
                tf_checks.append(f"D1{'\u2713' if hr.daily_bias == hr.direction else '\u2717'}")
            if hr.h4_bias != 'neutral':
                tf_checks.append(f"H4{'\u2713' if hr.h4_bias == hr.direction else '\u2717'}")
            tf_str = " ".join(tf_checks) if tf_checks else "neutral"
            lines.append(f"HTF Context: {strength_label} {hr.direction.upper()} ({tf_str})")

        # Zone info (Phase 2)
        if self._zone_type is not None:
            zone_upper = self._zone_type.upper()
            fib_str = f" (fib {self._fib_level:.2f})" if self._fib_level is not None else ""
            lines.append(f"Zone: {zone_upper}{fib_str}")

        lines.append(f"Таймфрейм: {self.timeframe.upper()}")

        entry = self.entry_price if self.entry_price is not None else self.close
        lines.append(f"Entry: <code>{entry}</code>")

        if self.sl is not None:
            sl_pct = (self.sl - entry) / entry * 100 if entry else 0
            lines.append(f"SL: <code>{self.sl}</code> ({sl_pct:+.2f}%)")
        if self.tp is not None:
            tp_pct = (self.tp - entry) / entry * 100 if entry else 0
            lines.append(f"TP: <code>{self.tp}</code> ({tp_pct:+.2f}%)")
        if self.sl is not None and self.tp is not None and entry:
            rr = abs(self.tp - entry) / abs(entry - self.sl) if entry != self.sl else 0
            lines.append(f"RR: 1:{rr:.1f}")

        # Zone quality multiplier
        if self._zone_quality_multiplier != 1.0:
            lines.append(f"Zone Quality: {self._zone_quality_multiplier:.1f}x ({self._zone_type or 'neutral'} entry)")

        if self._confidence_v2 is not None:
            quality_map = {"strong": "высокая", "moderate": "средняя", "weak": "низкая"}
            q = quality_map.get(self._confidence_v2.quality, self._confidence_v2.quality)
            lines.append(f"Confidence: {self.confidence:.0f}/100")
        else:
            lines.append(f"Confidence: {self.confidence:.0f}/100")
        return "\n".join(lines)


def _calculate_sl_tp(
    ind: IndicatorValues,
    signal: SignalType,
    structure: Any = None,
    entry: Optional[float] = None,
    timeframe: Optional[str] = None,
    order_blocks: Optional[list] = None,
    sweeps: Optional[list] = None,
    fvgs: Optional[list] = None,
    df: Any = None,
) -> tuple[float, float, Literal["ob", "fractal", "bos", "atr"]]:
    """Calculate SL/TP using ICT priority chain: OB > Fractal > BOS > ATR.

    SL Priority:
        1. Order Block zone (OB.low for BUY, OB.high for SELL + buffer)
        2. Fractal/Swing Point (swing_low for BUY, swing_high for SELL + buffer)
        3. BOS level (bos.level * 0.995/1.005)
        4. ATR fallback (entry ± ATR * multiplier)

    TP Priority:
        1. Opposing Order Block (nearest bearish OB for BUY, bullish for SELL)
        2. Active FVG (bearish FVG.bottom for BUY, bullish FVG.top for SELL)
        3. Swing structure (nearest swing_high for BUY, swing_low for SELL)
        4. ATR fallback (entry ± ATR * multiplier)
    """
    cfg = config.trading
    atr = ind.atr if ind.atr and ind.atr > 0 else ind.close * cfg.atr_fallback_pct / 100
    ep = entry if entry is not None else ind.close
    buffer_pct = cfg.stop_hunt_buffer_pct / 100  # convert % to decimal
    max_ob_dist = cfg.max_ob_distance_pct / 100

    # Per-TF ATR multiplier overrides
    atr_sl = cfg.atr_multiplier_sl
    atr_tp = cfg.atr_multiplier_tp
    if timeframe and cfg.atr_multipliers_per_tf:
        tf_override = cfg.atr_multipliers_per_tf.get(timeframe, {})
        if "sl" in tf_override:
            atr_sl = float(tf_override["sl"])
        if "tp" in tf_override:
            atr_tp = float(tf_override["tp"])

    # ═══ SL Priority Chain ═══

    sl = None
    sl_source = "atr"

    # 1. Order Block — SL behind the OB zone
    if order_blocks and signal == SignalType.BUY:
        # For BUY: find nearest bullish OB below entry
        candidates = [
            ob for ob in order_blocks
            if ob.type == "bullish" and ob.low < ep
            and (ep - ob.low) / ep <= max_ob_dist
        ]
        if candidates:
            # Pick the one closest to entry (highest low)
            best_ob = max(candidates, key=lambda ob: ob.low)
            sl = round(best_ob.low * (1 - buffer_pct), 8)
            sl_source = "ob"
    elif order_blocks and signal == SignalType.SELL:
        # For SELL: find nearest bearish OB above entry
        candidates = [
            ob for ob in order_blocks
            if ob.type == "bearish" and ob.high > ep
            and (ob.high - ep) / ep <= max_ob_dist
        ]
        if candidates:
            best_ob = min(candidates, key=lambda ob: ob.high)
            sl = round(best_ob.high * (1 + buffer_pct), 8)
            sl_source = "ob"

    # 2. Fractal / Swing Point — SL behind the最近的 fractal
    if sl is None and structure:
        swing_lows = getattr(structure, "recent_lows", []) or []
        swing_highs = getattr(structure, "recent_highs", []) or []

        if signal == SignalType.BUY and swing_lows:
            # Find nearest swing low below entry
            valid_lows = [p for p in swing_lows if p < ep]
            if valid_lows:
                best_low = max(valid_lows)  # closest to entry
                sl = round(best_low * (1 - buffer_pct), 8)
                sl_source = "fractal"
        elif signal == SignalType.SELL and swing_highs:
            # Find nearest swing high above entry
            valid_highs = [p for p in swing_highs if p > ep]
            if valid_highs:
                best_high = min(valid_highs)  # closest to entry
                sl = round(best_high * (1 + buffer_pct), 8)
                sl_source = "fractal"

    # 3. BOS level — SL behind the BOS
    if sl is None and structure and structure.last_bos:
        bos = structure.last_bos
        if signal == SignalType.BUY and bos.type == "bullish":
            candidate_sl = round(bos.level * (1 - buffer_pct), 8)
            if candidate_sl < ep:
                sl = candidate_sl
                sl_source = "bos"
        elif signal == SignalType.SELL and bos.type == "bearish":
            candidate_sl = round(bos.level * (1 + buffer_pct), 8)
            if candidate_sl > ep:
                sl = candidate_sl
                sl_source = "bos"

    # 4. ATR fallback
    if sl is None:
        if signal == SignalType.BUY:
            sl = round(ep - atr * atr_sl, 8)
        else:
            sl = round(ep + atr * atr_sl, 8)
        sl_source = "atr"

    # ═══ TP Priority Chain ═══

    tp = None
    tp_source = "atr"
    min_tp_distance = atr * 1.5  # Minimum TP distance = 1.5 ATR

    # 1. External Liquidity (EQH/EQL)
    sl_dist = abs(ep - sl) if sl else atr * atr_sl
    if signal == SignalType.BUY:
        from liquidity.external import find_external_liquidity
        ext_tp = find_external_liquidity(df, 'long', ep, sl_dist) if df is not None else None
        if ext_tp is not None:
            tp = round(ext_tp, 8)
            tp_source = "external_liquidity"
    elif signal == SignalType.SELL:
        from liquidity.external import find_external_liquidity
        ext_tp = find_external_liquidity(df, 'short', ep, sl_dist) if df is not None else None
        if ext_tp is not None:
            tp = round(ext_tp, 8)
            tp_source = "external_liquidity"

    # 2. Opposing Order Block — TP at the OB zone
    if tp is None and order_blocks and signal == SignalType.BUY:
        targets = [
            ob for ob in order_blocks
            if ob.type == "bearish" and ob.high > ep + min_tp_distance
        ]
        if targets:
            best_target = min(targets, key=lambda ob: ob.high)
            tp = round(best_target.midpoint, 8)
            tp_source = "ob"
    elif tp is None and order_blocks and signal == SignalType.SELL:
        targets = [
            ob for ob in order_blocks
            if ob.type == "bullish" and ob.low < ep - min_tp_distance
        ]
        if targets:
            best_target = max(targets, key=lambda ob: ob.low)
            tp = round(best_target.midpoint, 8)
            tp_source = "ob"

    # 3. Active FVG — TP at the FVG boundary
    if tp is None and fvgs:
        if signal == SignalType.BUY:
            fvg_targets = [
                fvg for fvg in fvgs
                if fvg.type == "bearish" and not fvg.filled and fvg.bottom > ep + min_tp_distance
            ]
            if fvg_targets:
                best_fvg = min(fvg_targets, key=lambda f: f.bottom)
                tp = round(best_fvg.bottom, 8)
                tp_source = "fvg"
        elif signal == SignalType.SELL:
            fvg_targets = [
                fvg for fvg in fvgs
                if fvg.type == "bullish" and not fvg.filled and fvg.top < ep - min_tp_distance
            ]
            if fvg_targets:
                best_fvg = max(fvg_targets, key=lambda f: f.top)
                tp = round(best_fvg.top, 8)
                tp_source = "fvg"

    # 4. Swing structure — TP at the opposing swing point
    if tp is None and structure:
        swing_highs = getattr(structure, "recent_highs", []) or []
        swing_lows = getattr(structure, "recent_lows", []) or []

        if signal == SignalType.BUY and swing_highs:
            valid_highs = [p for p in swing_highs if p > ep + min_tp_distance]
            if valid_highs:
                tp = round(min(valid_highs), 8)
                tp_source = "fractal"
        elif signal == SignalType.SELL and swing_lows:
            valid_lows = [p for p in swing_lows if p < ep - min_tp_distance]
            if valid_lows:
                tp = round(max(valid_lows), 8)
                tp_source = "fractal"

    # 5. ATR fallback
    if tp is None:
        if signal == SignalType.BUY:
            tp = round(ep + atr * atr_tp, 8)
        else:
            tp = round(ep - atr * atr_tp, 8)
        tp_source = "atr"

    # ═══ Validation ═══

    # Ensure SL is on the correct side of entry
    if signal == SignalType.BUY and sl >= ep:
        sl = round(ep - atr * atr_sl, 8)
        sl_source = "atr"
    elif signal == SignalType.SELL and sl <= ep:
        sl = round(ep + atr * atr_sl, 8)
        sl_source = "atr"

    # Ensure TP is on the correct side of entry
    if signal == SignalType.BUY and tp <= ep:
        tp = round(ep + atr * atr_tp, 8)
        tp_source = "atr"
    elif signal == SignalType.SELL and tp >= ep:
        tp = round(ep - atr * atr_tp, 8)
        tp_source = "atr"

    return sl, tp, sl_source


# FIX S3: candle close confirmation thresholds
CLOSE_CONFIRMATION_BUY_MIN = 0.6
CLOSE_CONFIRMATION_SELL_MAX = 0.4


class SignalEngine:
    def evaluate(
        self,
        ind: IndicatorValues,
        regime: Any = None,
        sweeps: Optional[list] = None,
        order_blocks: Optional[list] = None,
        entry_price: Optional[float] = None,
        **kwargs: Any,
    ) -> SignalResult:
        _gate_log: dict[str, bool] = {}

        _structure_provided = "structure" in kwargs
        structure = kwargs.get("structure")
        mtf_aligned = kwargs.get("mtf_aligned", None)
        mtf_direction = kwargs.get("mtf_direction", None)
        cfg = config.trading
        regime_name = regime.regime if regime else None

        critical = {
            "rsi": ind.rsi, "adx": ind.adx,
            "ema_fast": ind.ema_fast, "ema_slow": ind.ema_slow,
            "ema_trend": ind.ema_trend, "macd_hist": ind.macd_hist,
            "dmi_plus": ind.dmi_plus, "dmi_minus": ind.dmi_minus,
            "volume": ind.volume, "volume_sma": ind.volume_sma,
            "atr": ind.atr, "close": ind.close,
        }
        none_fields = []
        for name, val in critical.items():
            if val is None:
                none_fields.append(name)
            elif isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                none_fields.append(name)
        if none_fields:
            _gate_log["data_valid"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=[f"Incomplete (None/NaN: {', '.join(none_fields)})"],
                _regime=regime_name,
            )
        _gate_log["data_valid"] = True

        if regime_name == "compression":
            _gate_log["regime_compression_deferred"] = True
        else:
            _gate_log["regime"] = True

        effective_adx_min = 18 if regime_name == "compression" else cfg.adx_min
        if cfg.adx_filter_enabled and ind.adx < effective_adx_min:
            _gate_log["adx"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=[f"ADX={ind.adx:.1f} < {effective_adx_min} (filter)"],
                _regime=regime_name,
            )
        _gate_log["adx"] = True

        has_leading_trigger = False
        leading_reasons: List[str] = []
        leading_trigger_direction: Optional[str] = None

        if _structure_provided and structure and structure.last_bos:
            bos = structure.last_bos
            if bos.type == "bullish":
                has_leading_trigger = True
                leading_trigger_direction = "bullish"
                leading_reasons.append(f"BOS bullish (leading trigger) at {bos.level}")
            elif bos.type == "bearish":
                has_leading_trigger = True
                leading_trigger_direction = "bearish"
                leading_reasons.append(f"BOS bearish (leading trigger) at {bos.level}")

        if sweeps:
            for sw in sweeps:
                if getattr(sw, "is_valid", False):
                    has_leading_trigger = True
                    leading_trigger_direction = sw.type
                    leading_reasons.append(f"Sweep {sw.type} (leading trigger) at {sw.swept_level}")
                    break

        vol_above_avg = ind.volume > ind.volume_sma * cfg.volume_factor
        if ind.volume_delta_pct is not None and vol_above_avg:
            delta = ind.volume_delta_pct
            if delta > cfg.delta_bullish:
                has_leading_trigger = True
                leading_trigger_direction = "bullish"
                leading_reasons.append(f"Delta: +{delta:.0f}% (bullish delta, leading trigger)")
            elif delta < cfg.delta_bearish:
                has_leading_trigger = True
                leading_trigger_direction = "bearish"
                leading_reasons.append(f"Delta: {delta:.0f}% (bearish delta, leading trigger)")

        _pre_direction = 'buy' if (
            ind.ema_fast is not None and ind.ema_slow is not None
            and ind.ema_fast > ind.ema_slow
        ) else 'sell'

        order_block_confirmation = False
        if order_blocks:
            for ob in order_blocks:
                if getattr(ob, "is_valid", False):
                    ob_midpoint = getattr(ob, "midpoint", None)
                    if ob_midpoint is not None and ind.close > 0:
                        distance_pct = abs(ind.close - ob_midpoint) / ind.close * 100
                        if distance_pct < 2.0:
                            order_block_confirmation = True
                            ob_type = getattr(ob, "type", "unknown")
                            if (_pre_direction == "buy" and ob_type == "bullish") or \
                               (_pre_direction == "sell" and ob_type == "bearish"):
                                leading_reasons.append(
                                    f"Order Block {ob_type} confirmed at {ob_midpoint:.4f}"
                                )
                            break

        ema_cross_type = None
        if ind.ema_bullish_cross:
            ema_cross_type = "bullish"
        elif ind.ema_bearish_cross:
            ema_cross_type = "bearish"

        macd_cross_type = None
        macd_significant = False
        if ind.close > 0:
            macd_norm = abs(ind.macd_hist / ind.close) * 100
            if macd_norm >= cfg.min_macd_pct:
                macd_significant = True
                if ind.macd_hist > 0 and ind.macd_hist_prev <= 0:
                    macd_cross_type = "bullish"
                elif ind.macd_hist < 0 and ind.macd_hist_prev >= 0:
                    macd_cross_type = "bearish"

        direction = None
        if has_leading_trigger:
            if leading_trigger_direction:
                direction = "buy" if leading_trigger_direction == "bullish" else "sell"
            else:
                for r in leading_reasons:
                    if "bullish" in r.lower():
                        direction = "buy"
                        break
                    elif "bearish" in r.lower():
                        direction = "sell"
                        break
        if direction is None:
            if ema_cross_type == "bullish" or macd_cross_type == "bullish":
                direction = "buy"
            elif ema_cross_type == "bearish" or macd_cross_type == "bearish":
                direction = "sell"
        if direction is None:
            ema_direction = "buy" if ind.ema_fast > ind.ema_slow else "sell"
            if ind.adx < cfg.adx_strong:
                spread_pct = abs(ind.ema_fast - ind.ema_slow) / ind.ema_slow * 100
                if spread_pct < cfg.min_ema_spread_pct * 2:
                    _log_gates(_gate_log, ind)
                    return SignalResult(
                        signal=SignalType.NO_SIGNAL,
                        symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                        reasons=[f"No clear direction: ADX={ind.adx:.1f} < {cfg.adx_strong}, "
                                 f"EMA spread={spread_pct:.2f}% too narrow"],
                        _regime=regime_name,
                    )
            direction = ema_direction

        st_str = _strength_supertrend(ind, direction)
        ema_str = _strength_ema(ind, direction)
        macd_str = _strength_macd(ind, direction)
        rsi_str = _strength_rsi(ind, direction)
        vol_str = _strength_volume(ind, direction)
        adx_str = _strength_adx(ind)
        dmi_str = _strength_dmi(ind, direction)

        weights = _get_weights()
        total_weight = sum(weights.values())
        weighted_score = sum(
            {"Supertrend": st_str, "EMA": ema_str, "MACD": macd_str,
             "RSI": rsi_str, "Volume": vol_str, "ADX": adx_str, "DMI": dmi_str}[name]
            * weights[name]
            for name in weights
        )

        factor_strengths = {
            "Supertrend": st_str, "EMA": ema_str, "MACD": macd_str,
            "RSI": rsi_str, "Volume": vol_str, "ADX": adx_str, "DMI": dmi_str,
            "BUY": weighted_score / total_weight if total_weight else 0.0,
            "SELL": -weighted_score / total_weight if total_weight else 0.0,
        }

        if st_str < -0.3:
            _gate_log["supertrend_alignment"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=[f"Supertrend misaligned (strength={st_str:.1f}, direction={direction})"],
                _regime=regime_name,
                _factor_strengths=factor_strengths, _weighted_score=weighted_score,
            )
        _gate_log["supertrend_alignment"] = True

        breakout_reasons: List[str] = []
        if cfg.compression_enabled and regime_name == "compression":
            has_strong_trigger = False
            if _structure_provided and structure and structure.last_bos:
                has_strong_trigger = True
            if sweeps:
                for sw in sweeps:
                    if getattr(sw, "is_valid", False):
                        has_strong_trigger = True
                        break

            vol_strong = ind.volume > ind.volume_sma * cfg.compression_volume_factor
            supertrend_ok = (
                (direction == "buy" and ind.supertrend_direction == 1)
                or (direction == "sell" and ind.supertrend_direction == -1)
            )
            atr_pct = (ind.atr / ind.close * 100) if ind.close > 0 else 0
            atr_expanding = atr_pct >= 0.3

            if has_strong_trigger and vol_strong and supertrend_ok and atr_expanding:
                breakout_reasons.append(
                    f"Breakout mode: strong trigger + ATR={atr_pct:.2f}% + "
                    f"vol={ind.volume / ind.volume_sma:.1f}x + Supertrend aligned"
                )
                _gate_log["regime_breakout"] = True
            else:
                _gate_log["regime"] = False
                _log_gates(_gate_log, ind)
                return SignalResult(
                    signal=SignalType.NO_SIGNAL,
                    symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                    reasons=["Compression regime - breakout conditions not met"],
                    _regime=regime_name, _regime_blocked=True,
                )

        has_trigger = has_leading_trigger

        if not has_trigger and _structure_provided and structure is None:
            if ema_cross_type is not None or macd_cross_type is not None:
                has_trigger = True
                leading_reasons.append(
                    "EMA/MACD cross (fallback trigger - structure unavailable)"
                )

        if not has_trigger and _structure_provided:
            vol_above_avg = ind.volume > ind.volume_sma * cfg.volume_factor
            supertrend_aligned = (
                (direction == "buy" and ind.supertrend_direction == 1)
                or (direction == "sell" and ind.supertrend_direction == -1)
            )
            ema_aligned_check = (
                (direction == "buy" and ind.ema_fast > ind.ema_slow > ind.ema_trend)
                or (direction == "sell" and ind.ema_fast < ind.ema_slow < ind.ema_trend)
            )
            if (
                supertrend_aligned
                and ema_aligned_check
                and ind.adx >= cfg.adx_strong
                and vol_above_avg
            ):
                htf_ok = True
                if mtf_aligned and mtf_direction:
                    if (direction == "buy" and mtf_direction != "bullish") or \
                       (direction == "sell" and mtf_direction != "bearish"):
                        htf_ok = False
                if htf_ok:
                    has_trigger = True
                    leading_reasons.append(
                        f"Momentum entry (Supertrend {'bull' if direction == 'buy' else 'bear'} + "
                        f"EMA aligned + ADX={ind.adx:.1f} + volume)"
                    )

        if cfg.trigger_required and not has_trigger:
            _gate_log["trigger"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=["No trigger (no cross/sweep/BOS/delta)"],
                _has_trigger=False, _has_leading_trigger=has_leading_trigger,
                _regime=regime_name,
                _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                _rsi_strength=rsi_str,
            )
        _gate_log["trigger"] = True

        ema_aligned = False
        ema_spread_pct = 0.0
        if direction == "buy":
            if ind.ema_fast > ind.ema_slow > ind.ema_trend:
                ema_aligned = True
                ema_spread_pct = (ind.ema_fast - ind.ema_slow) / ind.ema_slow * 100
        else:
            if ind.ema_fast < ind.ema_slow < ind.ema_trend:
                ema_aligned = True
                ema_spread_pct = (ind.ema_slow - ind.ema_fast) / ind.ema_fast * 100

        min_spread = cfg.min_ema_spread_pct
        ema_alignment_info = ""

        if cfg.ema_alignment_enabled and not ema_aligned:
            _gate_log["ema_alignment"] = False
            if direction == "buy":
                ema_alignment_info = f"BUY: fast={ind.ema_fast} slow={ind.ema_slow} trend={ind.ema_trend}"
            else:
                ema_alignment_info = f"SELL: fast={ind.ema_fast} slow={ind.ema_slow} trend={ind.ema_trend}"
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=["EMA alignment not met"],
                _ema_alignment_info=ema_alignment_info,
                _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                _rsi_strength=rsi_str,
                _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                _regime=regime_name,
            )
        _gate_log["ema_alignment"] = True

        if cfg.ema_spread_enabled and ema_spread_pct < min_spread:
            _gate_log["ema_spread"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=[f"EMA spread too narrow: {ema_spread_pct:.2f}% < {min_spread:.2f}%"],
                _ema_alignment_info="",
                _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                _rsi_strength=rsi_str,
                _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                _regime=regime_name,
            )
        _gate_log["ema_spread"] = True

        if cfg.ema_slope_check:
            current_spread = ind.ema_fast - ind.ema_slow
            prev_spread = ind.ema_fast_prev - ind.ema_slow_prev
            if direction == "buy":
                if current_spread > 0 and current_spread < prev_spread * 0.95:
                    _gate_log["ema_slope"] = False
                    _log_gates(_gate_log, ind)
                    return SignalResult(
                        signal=SignalType.NO_SIGNAL,
                        symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                        reasons=["EMA slope weakening (>5%)"],
                        _ema_alignment_info=f"spread={ema_spread_pct:.2f}%",
                        _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                        _rsi_strength=rsi_str,
                        _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                        _regime=regime_name,
                    )
            else:
                if current_spread < 0 and abs(current_spread) < abs(prev_spread) * 0.95:
                    _gate_log["ema_slope"] = False
                    _log_gates(_gate_log, ind)
                    return SignalResult(
                        signal=SignalType.NO_SIGNAL,
                        symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                        reasons=["EMA slope weakening (>5%)"],
                        _ema_alignment_info=f"spread={ema_spread_pct:.2f}%",
                        _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                        _rsi_strength=rsi_str,
                        _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                        _regime=regime_name,
                    )
        _gate_log["ema_slope"] = True

        ema_alignment_info = f"spread={ema_spread_pct:.2f}%"

        reasons: List[str] = []
        if regime_name == "compression" and breakout_reasons:
            reasons.extend(breakout_reasons)
        reasons.extend(leading_reasons)

        if abs(st_str) > 0.5:
            if st_str > 0:
                reasons.append("Supertrend aligned")
            else:
                reasons.append("Supertrend NOT aligned (against signal)")

        if ema_aligned and ema_cross_type is not None:
            dir_word = "cross up" if ema_cross_type == "bullish" else "cross down"
            reasons.append(f"EMA{cfg.ema_fast} crossed EMA{cfg.ema_slow} {dir_word}")

        if macd_significant:
            macd_norm = abs(ind.macd_hist / ind.close) * 100 if ind.close > 0 else 0
            dir_word = "bullish" if ind.macd_hist > 0 else "bearish"
            reasons.append(f"MACD {dir_word} (norm={macd_norm:.2f}%)")

        if direction == "buy":
            if ind.rsi <= cfg.rsi_oversold:
                reasons.append(f"RSI={ind.rsi:.1f} - oversold, bounce expected")
            elif ind.rsi < cfg.rsi_bull_min:
                reasons.append(f"RSI={ind.rsi:.1f} - bullish zone")
        elif direction == "sell":
            if ind.rsi >= cfg.rsi_overbought:
                reasons.append(f"RSI={ind.rsi:.1f} - overbought, reversal expected")
            elif ind.rsi > cfg.rsi_bear_max:
                reasons.append(f"RSI={ind.rsi:.1f} - bearish zone")

        if ind.adx >= cfg.adx_strong:
            reasons.append(f"ADX={ind.adx:.1f} (strong trend)")

        if vol_above_avg and ind.volume_delta_pct is not None:
            delta = ind.volume_delta_pct
            if delta > cfg.delta_bullish:
                reasons.append(f"Delta: +{delta:.0f}% (bullish)")
            elif delta < cfg.delta_bearish:
                reasons.append(f"Delta: {delta:.0f}% (bearish)")

        if direction == "sell":
            if ind.dmi_minus > ind.dmi_plus:
                reasons.append(f"DMI- > DMI+ (+{ind.dmi_minus - ind.dmi_plus:.1f})")
        else:
            if ind.dmi_plus > ind.dmi_minus:
                reasons.append(f"DMI+ > DMI- (+{ind.dmi_plus - ind.dmi_minus:.1f})")

        supporting_reasons = [r for r in reasons if _reason_supports_direction(r, direction)]
        score = len(supporting_reasons)
        min_score = config.scoring.min_score_for_signal

        if cfg.min_score_enabled and score < min_score:
            _gate_log["min_score"] = False
            _log_gates(_gate_log, ind)
            return SignalResult(
                signal=SignalType.NO_SIGNAL,
                symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                reasons=reasons, score=score,
                _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                _rsi_strength=rsi_str, _ema_alignment_info=ema_alignment_info,
                _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                _regime=regime_name,
            )
        _gate_log["min_score"] = True

        signal_type = SignalType.BUY if direction == "buy" else SignalType.SELL

        is_sweep_setup = False
        if sweeps:
            for sw in sweeps:
                if getattr(sw, "is_valid", False) and (
                    (direction == "buy" and getattr(sw, "type", "").lower() == "bullish") or
                    (direction == "sell" and getattr(sw, "type", "").lower() == "bearish")
                ):
                    is_sweep_setup = True
                    break

        if cfg.candle_close_enabled and not is_sweep_setup:
            candle_range = ind.high - ind.low
            if candle_range > 0:
                close_position = (ind.close - ind.low) / candle_range
                if direction == "buy" and close_position < CLOSE_CONFIRMATION_BUY_MIN:
                    _gate_log["candle_close"] = False
                    _log_gates(_gate_log, ind)
                    return SignalResult(
                        signal=SignalType.NO_SIGNAL,
                        symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                        reasons=[f"Candle close confirmation failed: close at {close_position:.0%} of range"],
                        _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                        _rsi_strength=rsi_str, _ema_alignment_info=ema_alignment_info,
                        _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                        _regime=regime_name,
                    )
                if direction == "sell" and close_position > CLOSE_CONFIRMATION_SELL_MAX:
                    _gate_log["candle_close"] = False
                    _log_gates(_gate_log, ind)
                    return SignalResult(
                        signal=SignalType.NO_SIGNAL,
                        symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
                        reasons=[f"Candle close confirmation failed: close at {close_position:.0%} of range"],
                        _factor_strengths=factor_strengths, _weighted_score=weighted_score,
                        _rsi_strength=rsi_str, _ema_alignment_info=ema_alignment_info,
                        _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
                        _regime=regime_name,
                    )
        _gate_log["candle_close"] = True

        sl, tp, sl_source = _calculate_sl_tp(ind, signal_type, structure, entry=entry_price)

        _log_gates(_gate_log, ind, passed=True)

        conf_v2 = None
        try:
            conf_v2 = ConfidenceResult(
                factors=[
                    FactorScore.make("Supertrend", 15, st_str),
                    FactorScore.make("EMA", 15, ema_str),
                    FactorScore.make("MACD", 10, macd_str),
                    FactorScore.make("RSI", 10, rsi_str),
                    FactorScore.make("Volume", 15, vol_str),
                    FactorScore.make("ADX", 10, adx_str),
                    FactorScore.make("DMI", 10, dmi_str),
                ],
                total_score=round(weighted_score, 2),
                quality="strong" if score >= 6 else "moderate" if score >= 4 else "weak",
                recommendation=signal_type.value,
            )
        except Exception:
            pass

        return SignalResult(
            signal=signal_type,
            symbol=ind.symbol, timeframe=ind.timeframe, close=ind.close,
            sl=sl, tp=tp, reasons=reasons, score=score,
            entry_price=entry_price if entry_price is not None else ind.close,
            _factor_strengths=factor_strengths, _weighted_score=weighted_score,
            _rsi_strength=rsi_str, _ema_alignment_info=ema_alignment_info,
            _has_trigger=has_trigger, _has_leading_trigger=has_leading_trigger,
            _confidence_v2=conf_v2, _regime=regime_name, _regime_blocked=False,
            _structure_trend=structure.trend if _structure_provided and structure else None,
            _structure_bos=structure.last_bos.type if _structure_provided and structure and structure.last_bos else None,
            _sl_source=sl_source,
        )

    def evaluate_confirm(self, ind: IndicatorValues, direction: str) -> bool:
        if ind is None:
            return True

        fast = ind.ema_fast
        slow = ind.ema_slow

        ema_aligned = False
        if fast is not None and slow is not None:
            try:
                if direction == 'buy':
                    ema_aligned = fast > slow
                else:
                    ema_aligned = fast < slow
            except TypeError:
                ema_aligned = False

        if direction == 'buy':
            st_aligned = ind.supertrend_direction is None or ind.supertrend_direction == 1
        else:
            st_aligned = ind.supertrend_direction is None or ind.supertrend_direction == -1

        return ema_aligned or st_aligned


def _log_gates(gates: dict[str, bool], ind: IndicatorValues, passed: bool = False) -> None:
    if passed:
        logger.debug(
            f"GATE_STATS pass symbol={ind.symbol} tf={ind.timeframe} "
            f"gates={'|'.join(f'{k}=1' for k, v in gates.items())}"
        )
    else:
        failed = [k for k, v in gates.items() if not v]
        if failed:
            logger.debug(
                f"GATE_STATS reject symbol={ind.symbol} tf={ind.timeframe} "
                f"failed={'|'.join(failed)} "
                f"passed={'|'.join(f'{k}=1' for k, v in gates.items() if v)}"
            )


def _get_weights() -> Dict[str, int]:
    s = config.scoring
    return {
        "Supertrend": s.w_supertrend, "EMA": s.w_ema,
        "MACD": s.w_macd, "RSI": s.w_rsi,
        "Volume": s.w_volume, "ADX": s.w_adx, "DMI": s.w_dmi,
    }


def _reason_supports_direction(reason: str, direction: str) -> bool:
    reason_lower = reason.lower()
    if direction == "buy":
        if any(w in reason_lower for w in ["bearish", "sell"]):
            return False
        if "not aligned" in reason_lower or "against" in reason_lower:
            return False
    else:
        if any(w in reason_lower for w in ["bullish", "buy"]):
            return False
        if "not aligned" in reason_lower or "against" in reason_lower:
            return False
    return True


def _strength_supertrend(ind: IndicatorValues, direction: str) -> float:
    aligned = (
        (direction == "buy" and ind.supertrend_direction == 1) or
        (direction == "sell" and ind.supertrend_direction == -1)
    )
    if aligned:
        adx_factor = min(1.0, max(0.3, (ind.adx - config.trading.adx_min) / 30))
        return adx_factor
    else:
        adx_factor = min(1.0, max(0.3, (ind.adx - config.trading.adx_min) / 30))
        return -adx_factor


def _strength_ema(ind: IndicatorValues, direction: str) -> float:
    if direction == "buy" and ind.ema_fast > ind.ema_slow > ind.ema_trend:
        spread = (ind.ema_fast - ind.ema_slow) / ind.ema_slow * 100
        return min(1.0, spread / config.trading.ema_strength_cap)
    if direction == "sell" and ind.ema_fast < ind.ema_slow < ind.ema_trend:
        spread = (ind.ema_slow - ind.ema_fast) / ind.ema_fast * 100
        return -min(1.0, spread / config.trading.ema_strength_cap)
    return -1.0


def _strength_macd(ind: IndicatorValues, direction: str) -> float:
    if ind.close <= 0:
        return 0.0
    norm = abs(ind.macd_hist / ind.close) * 100
    if norm < config.trading.min_macd_pct:
        return 0.0

    if config.trading.macd_slope_check:
        if direction == "buy" and ind.macd_hist < ind.macd_hist_prev:
            return 0.0
        if direction == "sell" and ind.macd_hist > ind.macd_hist_prev:
            return 0.0

    raw = (ind.macd_hist / ind.close * 100) * config.trading.macd_score_multiplier
    if direction == "sell":
        raw = -raw
    return max(-1.0, min(1.0, raw))


def _strength_rsi(ind: IndicatorValues, direction: str) -> float:
    rsi = ind.rsi
    os_ = config.trading.rsi_oversold
    ob_ = config.trading.rsi_overbought
    bull_min_ = config.trading.rsi_bull_min
    bear_max_ = config.trading.rsi_bear_max

    if direction == "buy":
        if rsi <= os_:
            return 1.0
        elif rsi < bull_min_:
            return 0.5
        elif rsi < ob_:
            return 0.0
        else:
            return -1.0
    else:
        if rsi >= ob_:
            return 1.0
        elif rsi > bear_max_:
            return 0.5
        elif rsi > os_:
            return 0.0
        else:
            return -1.0


def _strength_volume(ind: IndicatorValues, direction: str) -> float:
    vol_above = ind.volume > ind.volume_sma * config.trading.volume_factor
    if not vol_above:
        return -0.3
    vol_ratio = ind.volume / ind.volume_sma if ind.volume_sma > 0 else 1.0
    if ind.volume_delta_pct is not None:
        delta = ind.volume_delta_pct
        delta_factor = min(1.0, abs(delta) / 30.0)
        s = min(1.0, 0.3 + 0.4 * (vol_ratio - 1.0) + 0.3 * delta_factor)
        return s if ((direction == "buy" and delta > 0) or (direction == "sell" and delta < 0)) else -s * 0.5
    return min(1.0, 0.3 + 0.4 * (vol_ratio - 1.0))


def _strength_adx(ind: IndicatorValues) -> float:
    adx_min = config.trading.adx_min
    if ind.adx < adx_min:
        return 0.0
    strength = (ind.adx - adx_min) / config.trading.adx_strength_range
    return max(-1.0, min(1.0, strength))


def _strength_dmi(ind: IndicatorValues, direction: str) -> float:
    diff = ind.dmi_plus - ind.dmi_minus
    raw = (diff / config.trading.dmi_norm_divisor) * config.trading.dmi_strength_multiplier
    if direction == "sell":
        raw = -raw
    return max(-1.0, min(1.0, raw))


signal_engine = SignalEngine()
