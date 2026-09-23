"""
risk/engine.py — Risk Engine (Layer 3)

Capital protection + position sizing.

Hard gates (MUST pass):
- R:R minimum
- SL absolute limits
- Portfolio risk
- Max active signals
- Data integrity

Soft adjustments (affect sizing, not blocking):
- Volatility scaling
- SL distance quality
- Probability-based Kelly sizing
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from loguru import logger

from config.settings import config


@dataclass
class PortfolioState:
    """Current portfolio state for risk calculations."""
    active_count: int = 0
    total_risk_pct: float = 0.0
    max_active_signals: int = 3
    max_portfolio_risk_pct: float = 3.0


@dataclass
class RiskDecision:
    """Output of the Risk Engine."""
    should_trade: bool
    risk_pct: float = 0.0  # % of capital to risk
    rr_ratio: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    rejection_reason: Optional[str] = None

    # Sizing details (for logging/analytics)
    kelly_fraction: float = 0.0
    volatility_adjustment: float = 1.0
    probability_confidence: float = 0.0


class RiskEngine:
    """Evaluates whether a trade can be executed and sizes the position.

    Only checks capital protection constraints. Does NOT evaluate setup quality.
    """

    def __init__(
        self,
        min_rr_ratio: float = 1.5,
        sl_absolute_min_pct: float = 0.25,
        sl_absolute_max_pct: float = 5.0,
        base_risk_pct: float = 1.0,
        min_risk_pct: float = 0.1,
        max_risk_pct: float = 1.0,
        max_active_signals: int = 3,
        max_portfolio_risk_pct: float = 3.0,
        sl_min_atr_multiplier: float = 1.5,
    ):
        self.min_rr_ratio = min_rr_ratio
        self.sl_absolute_min_pct = sl_absolute_min_pct
        self.sl_absolute_max_pct = sl_absolute_max_pct
        self.base_risk_pct = base_risk_pct
        self.min_risk_pct = min_risk_pct
        self.max_risk_pct = max_risk_pct
        self.max_active_signals = max_active_signals
        self.max_portfolio_risk_pct = max_portfolio_risk_pct
        self.sl_min_atr_multiplier = sl_min_atr_multiplier

    def evaluate(
        self,
        portfolio: PortfolioState,
        entry_price: float,
        sl: float,
        tp: float,
        atr_pct: float = 0.0,
        p_tp: float = 0.5,
        confidence: float = 0.5,
        mss_quality: float = 0.0,
        atr: float = 0.0,
        sl_source: Optional[str] = None,
        *,
        direction: Optional[str] = None,
    ) -> RiskDecision:
        """Evaluate risk and size the position.

        Args:
            portfolio: current portfolio state
            entry_price: entry price
            sl: stop loss price
            tp: take profit price
            atr_pct: ATR as percentage of price (for volatility scaling)
            p_tp: probability of take profit [0, 1]
            confidence: model confidence [0, 1]
            mss_quality: MSS score from Pattern Engine [0, 100]
            atr: raw ATR value
            sl_source: source of SL calculation (bos, ob, fractal, atr)
            direction: 'buy' or 'sell' (inferred from SL if omitted)

        Returns:
            RiskDecision with should_trade, risk_pct, and details.
        """
        import math

        # Validate before arithmetic: comparisons against NaN do not reject it.
        values = (entry_price, sl, tp, atr_pct, p_tp, confidence, mss_quality, atr)
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            return RiskDecision(False, rejection_reason="non-finite risk input")
        if not 0 <= p_tp <= 1 or not 0 <= confidence <= 1:
            return RiskDecision(False, rejection_reason="probability/confidence outside [0, 1]")
        if direction is None:
            direction = "buy" if sl < entry_price else "sell"
        if direction not in ("buy", "sell"):
            return RiskDecision(False, rejection_reason="invalid direction")
        if atr_pct < 0 or atr < 0 or not 0 <= mss_quality <= 100:
            return RiskDecision(False, rejection_reason="invalid sizing input")
        if not math.isfinite(portfolio.total_risk_pct) or portfolio.total_risk_pct < 0:
            return RiskDecision(False, rejection_reason="invalid portfolio risk")
        if not math.isfinite(portfolio.max_portfolio_risk_pct) or portfolio.max_portfolio_risk_pct <= 0:
            return RiskDecision(False, rejection_reason="invalid portfolio cap")
        if portfolio.active_count >= portfolio.max_active_signals:
            return RiskDecision(False, rejection_reason="max active signals")

        # === DATA VALIDITY ===

        if entry_price <= 0 or sl <= 0 or tp <= 0:
            return RiskDecision(
                should_trade=False,
                rejection_reason="invalid price data",
            )

        geometry_valid = (
            sl < entry_price < tp if direction == "buy" else tp < entry_price < sl
        )
        if not geometry_valid:
            return RiskDecision(False, rejection_reason=f"invalid {direction} SL/entry/TP geometry")

        risk_dist = abs(entry_price - sl)
        reward_dist = abs(tp - entry_price)

        if risk_dist <= 0:
            return RiskDecision(
                should_trade=False,
                rejection_reason="zero risk distance",
            )

        rr_ratio = reward_dist / risk_dist
        sl_distance_pct = risk_dist / entry_price * 100

        # === SOFT GATES (log violations, don't block — Kelly sizing handles them) ===

        _structural_sources = {
            "sweep_extreme", "ob_boundary", "swing_point",
            "structure_break", "structural",
            "bos_level", "ob", "fractal", "bos",
        }
        _is_structural = sl_source and sl_source in _structural_sources

        if rr_ratio < self.min_rr_ratio:
            logger.info(f"Risk hard gate: RR={rr_ratio:.2f} < {self.min_rr_ratio} (BLOCKED)")
            return RiskDecision(
                should_trade=False,
                rr_ratio=round(rr_ratio, 2),
                rejection_reason=f"RR {rr_ratio:.2f} < min {self.min_rr_ratio}",
            )

        if sl_distance_pct < self.sl_absolute_min_pct:
            logger.info(f"Risk soft gate: SL tight {sl_distance_pct:.2f}% < {self.sl_absolute_min_pct}% (proceeding via Kelly)")

        if not _is_structural and sl_distance_pct > self.sl_absolute_max_pct:
            logger.info(f"Risk soft gate: SL wide {sl_distance_pct:.2f}% > {self.sl_absolute_max_pct}% (proceeding via Kelly)")

        if atr > 0 and entry_price > 0 and not _is_structural:
            atr_pct_calc = atr / entry_price * 100
            min_sl_from_atr = atr_pct_calc * self.sl_min_atr_multiplier
            if sl_distance_pct < min_sl_from_atr:
                logger.info(f"Risk soft gate: SL tight vs ATR {sl_distance_pct:.2f}% < {self.sl_min_atr_multiplier}x ATR (proceeding via Kelly)")

        # === POSITION SIZING ===

        # Kelly-inspired: f = (p * b - q) / b
        p = p_tp
        q = 1 - p
        b = rr_ratio
        kelly = (p * b - q) / b if b > 0 else 0
        if kelly <= 0 or confidence <= 0:
            return RiskDecision(False, rr_ratio=round(rr_ratio, 2),
                                rejection_reason="non-positive Kelly allocation")
        kelly = min(kelly, 0.20)  # cap at 20% (half-Kelly)

        # Scale by model confidence
        kelly *= confidence

        # Final risk = min(kelly, base_risk)
        risk_pct = min(kelly * 100, self.base_risk_pct)

        # Volatility adjustment
        vol_adj = 1.0
        if atr_pct > 4.0:
            vol_adj = 0.5
        elif atr_pct > 2.5:
            vol_adj = 0.75
        risk_pct *= vol_adj

        # MSS quality soft adjustment (0-100 → 0.8x-1.1x)
        mss_adj = 1.0
        if mss_quality > 0:
            mss_adj = 0.8 + (mss_quality / 100.0) * 0.3
            mss_adj = max(0.8, min(1.1, mss_adj))
        risk_pct *= mss_adj

        # SL distance quality (soft)
        if sl_distance_pct < 1.0:
            risk_pct *= 1.1  # bonus for tight SL
        elif sl_distance_pct > 3.0:
            risk_pct *= 0.8  # penalty for wide SL

        # Clamp
        risk_pct = round(max(self.min_risk_pct, min(risk_pct, self.max_risk_pct)), 4)
        if not math.isfinite(risk_pct) or risk_pct <= 0:
            return RiskDecision(False, rejection_reason="invalid calculated risk")
        if round(portfolio.total_risk_pct + risk_pct, 10) > portfolio.max_portfolio_risk_pct:
            return RiskDecision(False, rr_ratio=round(rr_ratio, 2),
                                rejection_reason="portfolio risk including new trade exceeds cap")

        logger.info(
            f"Risk decision: risk={risk_pct:.2f}% | "
            f"RR={rr_ratio:.2f} | SL={sl_distance_pct:.2f}% | "
            f"P(TP)={p_tp:.1%} | Kelly={kelly:.3f} | "
            f"vol_adj={vol_adj:.2f} | mss_adj={mss_adj:.2f}"
        )

        return RiskDecision(
            should_trade=True,
            risk_pct=round(risk_pct, 4),
            rr_ratio=round(rr_ratio, 2),
            sl_price=sl,
            tp_price=tp,
            kelly_fraction=round(kelly, 4),
            volatility_adjustment=vol_adj,
            probability_confidence=confidence,
        )


# Singleton
risk_engine = RiskEngine(
    min_rr_ratio=config.trading.min_rr_threshold,
    sl_absolute_min_pct=config.risk_engine.sl_absolute_min_pct,
    sl_absolute_max_pct=config.risk_engine.sl_absolute_max_pct,
    base_risk_pct=config.risk_engine.base_risk_pct,
    min_risk_pct=config.risk_engine.min_risk_pct,
    max_risk_pct=config.risk_engine.max_risk_pct,
    max_active_signals=config.max_active_signals,
    max_portfolio_risk_pct=config.max_portfolio_risk_pct,
)
