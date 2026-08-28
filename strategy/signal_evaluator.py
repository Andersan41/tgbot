"""
strategy/signal_evaluator.py — Shared signal evaluation (single source of truth).

Used by BOTH the live scanner (scheduler/scanner.py) and the backtest engine
(backtest/engine.py) so that live and backtest produce identical decisions for
identical inputs:

  - estimate_p_tp()          → inline P(TP) estimation (scanner Phase 3)
  - apply_symbol_overrides() → per-symbol override gates (scanner Phase 1.46)
  - htf_opposition()         → HTF bias opposition / hard-gate decision
                               (scanner Phase 1.45)
  - entry_zone_touched()     → require_entry_zone gate: did price actually
                               trade in the FVG entry zone (scanner Phase 1.44)

Anything live-only (DB cooldown/dedup, MTF/context/news fetching, regime,
portfolio state) is left to the caller so the backtest can replay the same
logic with historical data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from config.settings import config

# setup.direction ("buy"/"sell") → HTF bias direction ("bullish"/"bearish")
_DIR2HTF = {"buy": "bullish", "sell": "bearish"}

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}


def get_cooldown_minutes(timeframe: str, base_minutes: int, multiplier: float) -> int:
    """Effective cooldown = max(base_minutes, timeframe_minutes × multiplier).

    Single source of truth — used by the live scanner (dedup + persisted
    cooldown) AND the backtest engine so the two can never drift apart.
    """
    tf_minutes = _TF_MINUTES.get(timeframe, 60)
    return max(base_minutes, int(tf_minutes * multiplier))


def dedup_block_window(
    last_signal_time: datetime,
    now: datetime,
    last_direction: str,
    new_direction: str,
    timeframe: str,
    base_cooldown_minutes: int,
    cooldown_tf_multiplier: float,
) -> Optional[int]:
    """Live scanner Phase-6 dedup gate decision (shared with backtest).

    Returns the cooldown window (minutes) that blocks the new signal, or None
    if it passes. Mirrors scanner.py exactly — same-direction re-entry blocked
    within the effective cooldown, opposite-direction within half of it.
    """
    cooldown_min = get_cooldown_minutes(timeframe, base_cooldown_minutes, cooldown_tf_multiplier)
    elapsed = now - last_signal_time
    if last_direction == new_direction:
        if elapsed < timedelta(minutes=cooldown_min):
            return cooldown_min
        return None
    cross = cooldown_min // 2
    if elapsed < timedelta(minutes=cross):
        return cross
    return None


def entry_zone_touched(
    direction: str,
    fvgs: list,
    bar_high: float,
    bar_low: float,
) -> bool:
    """Shared `require_entry_zone` gate — did price actually trade in the FVG zone?

    Mirrors the FVG selection in `trade_engine.build_trade_plan` (first ACTIVE
    FVG matching the trade direction) and the fill-touch semantics in
    `liquidity/fvg.py:_is_fvg_filled_np`:

      BUY  (bullish FVG): price retraced from above  → bar_low  <= fvg.top
      SELL (bearish FVG): price retraced from below  → bar_high >= fvg.bottom

    IMPORTANT (no look-ahead): `detect_fvg` marks an FVG `filled` when ANY
    candle after it (including the current one) touches the zone. So an FVG
    that is still `is_active` at the moment of the signal means price has NOT
    reached the zone yet — trade_engine would book a phantom fill at its
    median. This gate rejects exactly those signals.

    If there is no active matching FVG (entry falls back to candle close), the
    gate passes — there is nothing to chase.

    Direction logic is deliberate: swapped conditions would block the opposite
    (correct) entries. Matches `_is_fvg_filled` for both FVG types.
    """
    for f in fvgs or []:
        f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
        if f.is_active and f_dir == direction:
            if direction == "buy":
                return bar_low <= f.top
            return bar_high >= f.bottom
    return True


def estimate_p_tp(
    *,
    setup,
    context_score: float = 0.0,
    htf_result=None,
    htf_penalty: float = 1.0,
    mtf_aligned: bool = False,
) -> Tuple[float, float]:
    """Inline rule-based P(TP) estimate shared by live scanner & backtest.

    Mirrors scanner Phase 3 with two fixes:
      - HTF alignment boost now actually matches (bullish/bearish vs buy/sell)
      - the HTF opposition penalty is applied to P(TP) (was computed but unused)

    Returns ``(p_tp, confidence)``.
    """
    components = setup.components_count if getattr(setup, "detected", False) else 0
    p_tp = 0.45
    if components >= 4:
        p_tp += 0.15
    elif components >= 3:
        p_tp += 0.08
    if context_score > 0:
        p_tp += min(context_score * 0.1, 0.10)
    elif context_score < 0:
        p_tp += max(context_score * 0.1, -0.10)
    htf_dir = getattr(htf_result, "direction", None) if htf_result is not None else None
    if htf_dir in ("bullish", "bearish") and _DIR2HTF.get(setup.direction) == htf_dir:
        p_tp += 0.05
    if mtf_aligned:
        p_tp += 0.03
    if setup.mss_score > 70:
        p_tp += 0.05
    p_tp *= htf_penalty
    p_tp = max(0.15, min(0.85, p_tp))
    confidence = min(0.85, p_tp)
    return round(p_tp, 4), round(confidence, 4)


def apply_symbol_overrides(
    *,
    symbol: str,
    ind,
    setup,
    trade_plan=None,
    atr_pct: Optional[float] = None,
) -> Tuple[bool, str]:
    """Per-symbol override gates (scanner Phase 1.46), shared with backtest.

    Returns ``(blocked, reason)``. ``trade_plan`` may be None (e.g. when called
    before the plan exists); gates that need SL distance are skipped then.

    Uses real fields (trade_plan.sl_distance_pct, setup.overall_quality) instead
    of the non-existent setup.sl_distance_pct / setup.overall_setup_quality.
    """
    overrides = config.trading.symbol_overrides.get(symbol, {})
    if not overrides:
        return False, ""

    min_adx = overrides.get("adx_min")
    adx = getattr(ind, "adx", None)
    if min_adx is not None and adx is not None and adx < min_adx:
        return True, f"symbol ADX {adx:.1f} < {min_adx}"

    max_sl = overrides.get("max_sl_pct")
    sl_pct = trade_plan.sl_distance_pct if trade_plan is not None else None
    if max_sl is not None and sl_pct is not None and sl_pct > max_sl:
        return True, f"symbol SL {sl_pct:.1f}% > {max_sl}%"

    if atr_pct is None and ind.atr and ind.close > 0:
        atr_pct = ind.atr / ind.close * 100
    max_atr = overrides.get("max_atr_pct")
    if max_atr is not None and atr_pct is not None and atr_pct > max_atr:
        return True, f"symbol ATR% {atr_pct:.2f}% > {max_atr}%"

    min_q = overrides.get("min_quality")
    quality = getattr(setup, "overall_quality", 0.0)
    if min_q is not None and quality < min_q:
        return True, f"symbol quality {quality:.0f} < {min_q}"

    blocked = overrides.get("block_setup_types", []) or []
    if blocked:
        st = f"{setup.direction.upper()}_{setup.setup_type}"
        if st in blocked:
            return True, f"symbol blocked: {st}"

    return False, ""


def htf_opposition(setup, htf_dir: Optional[str]) -> Tuple[bool, bool]:
    """HTF bias opposition decision (scanner Phase 1.45), shared with backtest.

    Returns ``(opposed, hard_block)``:
      - opposed:     setup direction opposes HTF bias → P(TP) penalty applies
      - hard_block:  continuation vs HTF bias AND config.htf_hard_gate → block
    """
    if htf_dir not in ("bullish", "bearish"):
        return False, False
    setup_bias = _DIR2HTF.get(setup.direction)
    opposed = setup_bias != htf_dir
    hard_block = opposed and setup.setup_type == "continuation" and config.htf_hard_gate
    return opposed, hard_block