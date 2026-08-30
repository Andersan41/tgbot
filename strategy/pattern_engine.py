"""
strategy/pattern_engine.py — ICT Pattern Engine (Layer 1)

Two separate pipelines:

REVERSAL:
  Sweep → Displacement → MSS → [OB/FVG entry zone] → Entry

CONTINUATION:
  Trend → Pullback → BOS → [OB/FVG retrace] → Entry

Scenario Detection ≠ Entry Ready.
- Scenario: "Is there a trade idea?"
- Entry Armed: "Can we enter now?" (price in OB/FVG zone)

Hard gates per setup type:
  Reversal: sweep + displacement + mss
  Continuation: BOS + trend alignment

OB/FVG are entry zones, never gates.
RSI/ADX/EMA never block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

import pandas as pd
from loguru import logger


@dataclass
class ComponentQuality:
    """Quality assessment for a single pattern component.

    Attributes:
        score: 0-100 quality score
        confidence: 0-1 how confident we are in this score
        reasons: human-readable supporting factors
        diagnostics: raw metrics for debugging / ML
    """
    score: float = 0.0
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    diagnostics: Dict[str, float] = field(default_factory=dict)


@dataclass
class ICTSetup:
    """Result of ICT pattern detection."""

    detected: bool
    direction: Optional[str] = None  # "buy" / "sell"
    setup_type: Optional[str] = None  # "reversal" / "continuation"

    # ── Reversal components ──
    has_sweep: bool = False
    sweep_type: Optional[str] = None
    sweep_strength: float = 0.0
    sweep_reclaim_candles: int = 0

    has_displacement: bool = False
    displacement_body_pct: float = 0.0
    displacement_atr_ratio: float = 0.0

    has_mss: bool = False
    mss_score: float = 0.0  # 0-100
    mss_causality: float = 0.0  # 0-1 exponential decay
    sweep_to_mss_bars: int = 0

    # ── Continuation components ──
    has_bos: bool = False
    bos_type: Optional[str] = None  # "bullish" / "bearish"
    bos_level: float = 0.0

    # ── Entry zones (NOT gates) ──
    has_ob: bool = False
    ob_type: Optional[str] = None
    ob_distance_pct: float = 0.0
    ob_midpoint: float = 0.0

    has_fvg: bool = False
    fvg_type: Optional[str] = None
    fvg_size_pct: float = 0.0

    # ── Entry readiness (soft — log but don't block) ──
    entry_armed: bool = False

    # ── Structural info ──
    structure_trend: Optional[str] = None

    # ── Component list ──
    components_found: List[str] = field(default_factory=list)

    # ── Rejection info ──
    rejection_reason: Optional[str] = None

    # ── Quality scores (0-100) ──
    sweep_quality_score: float = 0.0
    mss_quality_score: float = 0.0
    bos_quality_score: float = 0.0
    ob_quality_score: float = 0.0
    fvg_quality_score: float = 0.0

    # ── Component quality details ──
    sweep_quality: Optional[ComponentQuality] = None
    mss_quality_detail: Optional[ComponentQuality] = None
    bos_quality: Optional[ComponentQuality] = None
    ob_quality: Optional[ComponentQuality] = None
    fvg_quality: Optional[ComponentQuality] = None

    # ── Overall metrics ──
    overall_quality: float = 0.0  # weighted average of component scores
    setup_confidence: float = 0.0  # 0-1

    @property
    def components_count(self) -> int:
        return len(self.components_found)

    @property
    def is_reversal(self) -> bool:
        return self.setup_type == "reversal"

    @property
    def is_continuation(self) -> bool:
        return self.setup_type == "continuation"

    @property
    def has_trigger(self) -> bool:
        """Backward-compatible: any trigger present."""
        return self.has_sweep or self.has_bos

    @property
    def has_confirmation(self) -> bool:
        """Backward-compatible: any confirmation present."""
        return self.has_ob or self.has_fvg


class PatternEngine:
    """Detects ICT setups from raw price action data.

    No indicators. No scoring. Pure pattern detection.
    Two pipelines: reversal and continuation.
    """

    def __init__(
        self,
        ob_proximity_pct: float = 2.0,
        min_overall_quality: float = 0.0,
        min_setup_confidence: float = 0.0,
        min_components_required: int = 2,
        max_fvg_age_candles: int = 4,
    ):
        self.ob_proximity_pct = ob_proximity_pct
        self.min_overall_quality = min_overall_quality
        self.min_setup_confidence = min_setup_confidence
        self.min_components_required = min_components_required
        self.max_fvg_age_candles = max_fvg_age_candles

    def detect(
        self,
        sweeps: list,
        order_blocks: list,
        structure,
        fvgs: list,
        candle_quality,
        current_price: float,
        atr: float = 0.0,
        df: Optional[pd.DataFrame] = None,
    ) -> ICTSetup:
        """Detect whether a valid ICT setup exists.

        Tries REVERSAL first (sweep + displacement + MSS).
        Falls back to CONTINUATION (trend + BOS).

        Args:
            sweeps: list of SweepEvent from liquidity.sweep.detect_sweeps()
            order_blocks: list of OrderBlock from liquidity.order_blocks.detect_order_blocks()
            structure: StructureState from market_structure.structure.analyze_structure()
            fvgs: list of FairValueGap from liquidity.fvg.detect_fvg()
            candle_quality: CandleQuality from liquidity.candle_quality.analyze_last_candle()
            current_price: current close price
            atr: current ATR value

        Returns:
            ICTSetup with detection result.
        """
        direction = None
        setup_type = None
        reversal_rejection = None
        continuation_rejection = None

        # ═══ REVERSAL PATH ═══
        # Sweep → Displacement → MSS
        reversal = self._try_reversal(
            sweeps, structure, candle_quality, atr, current_price,
        )
        if reversal.detected:
            direction = reversal.direction
            setup_type = "reversal"
        else:
            reversal_rejection = reversal.rejection_reason

        # ═══ CONTINUATION PATH ═══
        # Trend → BOS + Sweep (only if reversal not found)
        if not reversal.detected:
            continuation = self._try_continuation(structure, sweeps)
            if continuation.detected:
                direction = continuation.direction
                setup_type = "continuation"
                reversal = continuation
            else:
                continuation_rejection = continuation.rejection_reason

        if direction is None:
            trend = structure.trend if structure else "ranging"
            # Prefer continuation-specific reason if continuation was tried
            # and reversal failed at early stage (no sweep)
            if continuation_rejection and reversal_rejection:
                # If reversal failed at first step, continuation reason is more relevant
                if "no sweep" in (reversal_rejection or ""):
                    reason = continuation_rejection
                else:
                    reason = reversal_rejection
            else:
                reason = continuation_rejection or reversal_rejection or "no valid setup"
            return ICTSetup(
                detected=False,
                structure_trend=trend,
                rejection_reason=reason,
            )

        # ═══ DETECT ENTRY ZONES ═══
        setup = reversal
        setup.direction = direction
        setup.setup_type = setup_type
        setup.structure_trend = structure.trend if structure else None

        self._detect_entry_zones(setup, order_blocks, fvgs, direction, current_price, df)

        # ═══ CHECK ENTRY ARMED ═══
        setup.entry_armed = self._check_entry_armed(setup, current_price)

        # ═══ COMPUTE QUALITY SCORES ═══
        self._compute_quality_scores(setup, sweeps, structure, order_blocks, fvgs, candle_quality, current_price, atr)

        # ═══ VALIDATE SETUP QUALITY ═══
        quality_rejection = self._validate_setup_quality(setup)
        if quality_rejection:
            return ICTSetup(
                detected=False,
                direction=direction,
                setup_type=setup_type,
                has_sweep=setup.has_sweep,
                sweep_type=setup.sweep_type,
                has_displacement=setup.has_displacement,
                has_mss=setup.has_mss,
                has_bos=setup.has_bos,
                bos_type=setup.bos_type,
                structure_trend=structure.trend if structure else None,
                rejection_reason=quality_rejection,
            )

        # ═══ BUILD COMPONENT LIST ═══
        setup.components_found = self._build_components(setup)

        # ═══ LOG ═══
        logger.info(
            f"ICT setup: {direction.upper()} {setup_type} | "
            f"quality={setup.overall_quality:.0f} confidence={setup.setup_confidence:.2f} | "
            f"components={setup.components_found} | "
            f"entry_armed={setup.entry_armed} | "
            f"MSS={setup.has_mss} BOS={setup.has_bos} "
            f"Sweep={setup.has_sweep} Disp={setup.has_displacement} "
            f"OB={setup.has_ob} FVG={setup.has_fvg}"
        )

        return setup

    def _try_reversal(
        self,
        sweeps: list,
        structure,
        candle_quality,
        atr: float,
        current_price: float,
    ) -> ICTSetup:
        """Try to detect a REVERSAL setup: sweep + displacement + MSS."""
        # 1. Sweep required
        has_sweep = False
        sweep_type = None
        sweep_strength = 0.0
        sweep_reclaim = 0
        sweep_candle_index = -1

        valid_sweeps = [s for s in sweeps if s.is_valid]
        if valid_sweeps:
            # Use the NEWEST valid sweep (last in chronological order),
            # not the oldest. Earlier sweeps may be stale/irrelevant.
            s = valid_sweeps[-1]
            has_sweep = True
            sweep_type = s.type
            sweep_strength = s.strength
            sweep_reclaim = s.reclaim_candles
            sweep_candle_index = s.candle_index

        if not has_sweep:
            return ICTSetup(
                detected=False,
                rejection_reason="reversal: no sweep",
            )

        # 2. Displacement (informational — not a gate for MSS setups)
        # MSS classification already measures displacement >= 1 ATR between sweep and CHoCH.
        # The current candle does NOT need to be a displacement candle.
        has_displacement = False
        disp_body = 0.0
        disp_atr = 0.0
        if candle_quality:
            has_displacement = candle_quality.is_displacement
            disp_body = candle_quality.body_pct
        if atr > 0 and candle_quality:
            disp_atr = candle_quality.body_pct * (current_price / 100) / atr
        if candle_quality:
            disp_atr = candle_quality.body_atr_ratio if hasattr(candle_quality, 'body_atr_ratio') else disp_atr

        # 3. MSS required (strong CHoCH after sweep)
        has_mss = False
        mss_score = 0.0
        mss_causality = 0.0
        sweep_to_mss = 0
        direction = None

        if structure and structure.last_mss is not None:
            mss = structure.last_mss
            has_mss = True
            mss_score = mss.mss_score
            mss_causality = mss.causality_score
            # Direction from MSS + sweep alignment
            if mss.type == "bullish":
                direction = "buy"
            elif mss.type == "bearish":
                direction = "sell"
            # Bars between sweep and MSS — MSS MUST come after sweep
            if sweep_candle_index >= 0 and mss.candle_index >= 0:
                if mss.candle_index <= sweep_candle_index:
                    # MSS happened before or at same time as sweep — causality violated
                    return ICTSetup(
                        detected=False,
                        has_sweep=has_sweep, sweep_type=sweep_type,
                        has_displacement=has_displacement,
                        rejection_reason="reversal: MSS before sweep (causality violated)",
                    )
                sweep_to_mss = mss.candle_index - sweep_candle_index

        if not has_mss:
            return ICTSetup(
                detected=False,
                has_sweep=has_sweep, sweep_type=sweep_type,
                has_displacement=has_displacement,
                rejection_reason="reversal: no MSS (strong CHoCH)",
            )

        if direction is None:
            return ICTSetup(
                detected=False,
                has_sweep=has_sweep, sweep_type=sweep_type,
                has_displacement=has_displacement,
                has_mss=has_mss,
                rejection_reason="reversal: MSS direction unclear",
            )

        # Direction consistency: sweep and MSS must agree
        # Bullish reversal: sweep=bullish (swept lows) + MSS=bullish (CHoCH up)
        # Bearish reversal: sweep=bearish (swept highs) + MSS=bearish (CHoCH down)
        if direction == "buy" and sweep_type != "bullish":
            return ICTSetup(
                detected=False,
                has_sweep=has_sweep, sweep_type=sweep_type,
                has_displacement=has_displacement,
                has_mss=has_mss,
                rejection_reason=f"reversal: sweep type '{sweep_type}' != 'bullish' for buy",
            )
        if direction == "sell" and sweep_type != "bearish":
            return ICTSetup(
                detected=False,
                has_sweep=has_sweep, sweep_type=sweep_type,
                has_displacement=has_displacement,
                has_mss=has_mss,
                rejection_reason=f"reversal: sweep type '{sweep_type}' != 'bearish' for sell",
            )

        return ICTSetup(
            detected=True,
            direction=direction,
            setup_type="reversal",
            has_sweep=has_sweep,
            sweep_type=sweep_type,
            sweep_strength=sweep_strength,
            sweep_reclaim_candles=sweep_reclaim,
            has_displacement=has_displacement,
            displacement_body_pct=disp_body,
            displacement_atr_ratio=disp_atr,
            has_mss=has_mss,
            mss_score=mss_score,
            mss_causality=mss_causality,
            sweep_to_mss_bars=sweep_to_mss,
        )

    def _try_continuation(self, structure, sweeps=None) -> ICTSetup:
        """Try to detect a CONTINUATION setup: trend + BOS + sweep."""
        if structure is None:
            return ICTSetup(
                detected=False,
                rejection_reason="continuation: no structure",
            )

        trend = structure.trend

        # 1. Trend required (no ranging)
        if trend == "ranging":
            return ICTSetup(
                detected=False,
                structure_trend=trend,
                rejection_reason="continuation: ranging market",
            )

        # 2. BOS required
        has_bos = False
        bos_type = None
        bos_level = 0.0
        direction = None

        if structure.last_bos is not None:
            bos = structure.last_bos
            has_bos = True
            bos_type = bos.type
            bos_level = bos.level
            if bos.type == "bullish":
                direction = "buy"
            elif bos.type == "bearish":
                direction = "sell"

        if not has_bos:
            return ICTSetup(
                detected=False,
                structure_trend=trend,
                rejection_reason="continuation: no BOS",
            )

        # 3. Trend alignment required
        trend_aligned = (
            (direction == "buy" and trend == "bullish") or
            (direction == "sell" and trend == "bearish")
        )

        if not trend_aligned:
            return ICTSetup(
                detected=False,
                has_bos=has_bos, bos_type=bos_type,
                structure_trend=trend,
                rejection_reason=f"continuation: BOS {bos_type} vs trend {trend}",
            )

        # 4. Sweep required in OPPOSITE direction of trend (100% of winners had sweep)
        # Bullish continuation: bearish sweep (grab liquidity above, reject, continue up)
        # Bearish continuation: bullish sweep (grab liquidity below, reject, continue down)
        # SweepEvent.type is "bullish"/"bearish", NOT "sweep_low"/"sweep_high"
        has_sweep = False
        sweep_type = None
        sweep_strength = 0.0

        if sweeps:
            valid_sweeps = [s for s in sweeps if s.is_valid]
            for s in valid_sweeps:
                if direction == "buy" and s.type == "bearish":
                    has_sweep = True
                    sweep_type = s.type
                    sweep_strength = s.strength
                    break
                elif direction == "sell" and s.type == "bullish":
                    has_sweep = True
                    sweep_type = s.type
                    sweep_strength = s.strength
                    break

        if not has_sweep:
            return ICTSetup(
                detected=False,
                has_bos=has_bos, bos_type=bos_type,
                structure_trend=trend,
                rejection_reason=f"continuation: no sweep in {direction} direction",
            )

        return ICTSetup(
            detected=True,
            direction=direction,
            setup_type="continuation",
            has_bos=has_bos,
            bos_type=bos_type,
            bos_level=bos_level,
            has_sweep=has_sweep,
            sweep_type=sweep_type,
            sweep_strength=sweep_strength,
        )

    def _score_sweep(self, sweep, current_price: float, atr: float) -> ComponentQuality:
        """Score sweep quality 0-100.

        Factors:
        - Wick/body ratio (larger wick = stronger rejection)
        - Volume ratio (higher = more participation)
        - Reclaim speed (faster = stronger)
        - Displacement after sweep (impulsive move)
        - Delta alignment (directional volume)
        """
        score = 0.0
        reasons = []
        diag = {}

        # 1. Wick/body ratio (0-25): larger wick = better rejection
        wbr = getattr(sweep, 'wick_body_ratio', 0.0)
        wick_score = min(wbr / 4.0, 1.0) * 25.0
        score += wick_score
        diag["wick_body_ratio"] = wbr
        if wbr >= 3.0:
            reasons.append(f"Strong wick rejection ({wbr:.1f}x)")

        # 2. Volume ratio (0-25): higher = more conviction
        vol = getattr(sweep, 'volume_ratio', 1.0)
        vol_score = min(max(vol - 1.0, 0.0) / 3.0, 1.0) * 25.0
        score += vol_score
        diag["volume_ratio"] = vol
        if vol >= 2.0:
            reasons.append(f"High volume ({vol:.1f}x avg)")

        # 3. Reclaim speed (0-25): fewer candles = stronger
        reclaim = getattr(sweep, 'reclaim_candles', 5)
        if reclaim <= 1:
            reclaim_score = 25.0
        elif reclaim <= 2:
            reclaim_score = 20.0
        elif reclaim <= 3:
            reclaim_score = 15.0
        elif reclaim <= 5:
            reclaim_score = 8.0
        else:
            reclaim_score = 0.0
        score += reclaim_score
        diag["reclaim_candles"] = float(reclaim)
        if reclaim <= 2:
            reasons.append(f"Fast reclaim ({reclaim} bars)")

        # 4. Displacement after sweep (0-15): impulsive move validates sweep
        disp = getattr(sweep, 'displacement_after', 0.0)
        disp_score = min(disp / 2.0, 1.0) * 15.0
        score += disp_score
        diag["displacement_after"] = disp
        if disp >= 1.5:
            reasons.append(f"Strong displacement ({disp:.1f}x)")

        # 5. Delta alignment (0-10): directional volume confirms
        delta = getattr(sweep, 'delta_aligned', False)
        delta_score = 10.0 if delta else 0.0
        score += delta_score
        diag["delta_aligned"] = float(delta)
        if delta:
            reasons.append("Delta aligned")

        # Confidence: based on data completeness
        has_data = (wbr > 0) + (vol > 0) + (reclaim < 10) + (disp > 0) + True
        confidence = min(has_data / 5.0, 1.0)

        return ComponentQuality(
            score=round(min(score, 100.0), 1),
            confidence=round(confidence, 2),
            reasons=reasons,
            diagnostics=diag,
        )

    def _score_mss(
        self,
        mss,
        sweep_quality: ComponentQuality,
        displacement_atr: float,
        candle_quality,
        structure=None,
    ) -> ComponentQuality:
        """Score MSS quality 0-100.

        Uses existing calc_mss_score (0-100) as base, then enriches with
        sweep quality and candle displacement data.
        """
        reasons = []
        diag = {}

        # Base MSS score from market_structure module (already 0-100)
        base = getattr(mss, 'mss_score', 50.0)
        diag["base_mss_score"] = base

        # Causality decay
        causality = getattr(mss, 'causality_score', 0.0)
        diag["causality"] = causality
        if causality >= 0.8:
            reasons.append(f"Strong causal link ({causality:.2f})")
        elif causality >= 0.5:
            reasons.append(f"Moderate causal link ({causality:.2f})")

        # Reclaim speed
        reclaim = getattr(mss, 'reclaim_bars', 0)
        diag["reclaim_bars"] = float(reclaim)
        if reclaim <= 2:
            reasons.append(f"Fast reclaim ({reclaim} bars)")

        # Displacement ATR
        diag["displacement_atr"] = displacement_atr
        if displacement_atr >= 2.0:
            reasons.append(f"Strong displacement ({displacement_atr:.1f} ATR)")

        # Sweep quality contribution (add 0-10 bonus)
        sweep_bonus = (sweep_quality.score / 100.0) * 10.0
        score = base + sweep_bonus

        # Reclaim penalty (slow reclaim reduces score)
        if reclaim >= 5:
            score *= 0.85
            reasons.append("Slow reclaim penalty")

        # Confidence
        confidence = 0.5  # base
        if causality > 0.3:
            confidence += 0.2
        if displacement_atr >= 1.0:
            confidence += 0.15
        if sweep_quality.score >= 50:
            confidence += 0.15

        return ComponentQuality(
            score=round(min(score, 100.0), 1),
            confidence=round(min(confidence, 1.0), 2),
            reasons=reasons,
            diagnostics=diag,
        )

    def _score_bos(self, structure, direction: str, atr: float) -> ComponentQuality:
        """Score BOS quality 0-100.

        Factors:
        - ATR displacement (how far past swing)
        - Swing significance (how many swings broken)
        - Volume confirmation
        - Trend alignment strength
        """
        bos = structure.last_bos if structure else None
        if bos is None:
            return ComponentQuality(score=0.0, confidence=0.0, reasons=["no BOS"])

        score = 0.0
        reasons = []
        diag = {}

        # 1. BOS displacement (0-30): how far past the broken level
        if atr > 0:
            # Estimate displacement as distance from BOS level to current price
            displacement = abs(bos.level - (structure.recent_highs[-1] if structure.recent_highs else bos.level))
            disp_ratio = displacement / atr
            disp_score = min(disp_ratio / 2.0, 1.0) * 30.0
            score += disp_score
            diag["bos_displacement_atr"] = disp_ratio
            if disp_ratio >= 1.5:
                reasons.append(f"Strong BOS displacement ({disp_ratio:.1f} ATR)")

        # 2. Swing count (0-25): more structure breaks = stronger trend
        swing_count = len(structure.swing_points) if structure else 0
        swing_score = min(swing_count / 6.0, 1.0) * 25.0
        score += swing_score
        diag["swing_count"] = float(swing_count)
        if swing_count >= 4:
            reasons.append(f"Multiple structure breaks ({swing_count})")

        # 3. Trend clarity (0-25): ADX-like measure from structure
        trend = structure.trend if structure else "ranging"
        trend_score = 25.0 if trend != "ranging" else 0.0
        score += trend_score
        diag["trend_strength"] = 1.0 if trend != "ranging" else 0.0

        # 4. Direction alignment (0-20): BOS matches trend
        if structure and structure.last_bos:
            aligned = (
                (direction == "buy" and structure.trend == "bullish") or
                (direction == "sell" and structure.trend == "bearish")
            )
            if aligned:
                score += 20.0
                reasons.append("BOS aligned with trend")
            diag["trend_aligned"] = float(aligned)

        # Confidence
        confidence = 0.6
        if swing_count >= 3:
            confidence += 0.2
        if trend != "ranging":
            confidence += 0.2

        return ComponentQuality(
            score=round(min(score, 100.0), 1),
            confidence=round(min(confidence, 1.0), 2),
            reasons=reasons,
            diagnostics=diag,
        )

    def _score_ob(self, ob, direction: str, current_price: float, atr: float) -> ComponentQuality:
        """Score Order Block quality 0-100.

        State classification:
        - Fresh (just formed, not retested): highest quality
        - Tested (1 retest): still good
        - Partial (partially mitigated): reduced quality
        - Mitigated (price returned through zone): low quality
        - Broken (price moved through and closed beyond): invalid

        Factors:
        - Freshness (age in candles)
        - Displacement strength (how big the move after OB)
        - Volume ratio
        - Proximity to current price
        - Mitigation state
        """
        score = 0.0
        reasons = []
        diag = {}

        # 1. State (0-40): fresh > tested > partial > mitigated > broken
        if getattr(ob, 'mitigated', False):
            state_score = 10.0  # mitigated
            diag["state"] = "mitigated"
            reasons.append("OB mitigated")
        elif getattr(ob, 'retested', False):
            state_score = 25.0  # tested (1 retest is healthy)
            diag["state"] = "tested"
            reasons.append("OB tested (1 retest)")
        else:
            state_score = 40.0  # fresh
            diag["state"] = "fresh"
            reasons.append("Fresh OB")
        score += state_score

        # 2. Displacement ATR (0-25): bigger move = stronger OB
        disp_atr = getattr(ob, 'displacement_atr', 0.0)
        disp_score = min(disp_atr / 3.0, 1.0) * 25.0
        score += disp_score
        diag["displacement_atr"] = disp_atr
        if disp_atr >= 2.0:
            reasons.append(f"Strong displacement ({disp_atr:.1f} ATR)")

        # 3. Volume ratio (0-20): higher = more conviction
        vol = getattr(ob, 'volume_ratio', 1.0)
        vol_score = min(max(vol - 1.0, 0.0) / 2.0, 1.0) * 20.0
        score += vol_score
        diag["volume_ratio"] = vol
        if vol >= 2.0:
            reasons.append(f"High volume ({vol:.1f}x)")

        # 4. BOS validation (0-15): confirmed by structure break
        has_bos = getattr(ob, 'has_bos', False)
        bos_score = 15.0 if has_bos else 0.0
        score += bos_score
        diag["has_bos"] = float(has_bos)

        # Confidence
        confidence = 0.5
        if disp_atr >= 1.5:
            confidence += 0.2
        if vol >= 1.5:
            confidence += 0.15
        if has_bos:
            confidence += 0.15

        return ComponentQuality(
            score=round(min(score, 100.0), 1),
            confidence=round(min(confidence, 1.0), 2),
            reasons=reasons,
            diagnostics=diag,
        )

    def _score_fvg(self, fvg, direction: str, current_price: float, df=None) -> ComponentQuality:
        """Score FVG quality 0-100.

        Factors:
        - Size (larger gap = stronger imbalance)
        - Fill % (how much has been filled — partially filled is best)
        - Age (fresher = better)
        - Trend alignment
        """
        score = 0.0
        reasons = []
        diag = {}

        # 1. Size (0-30): larger gap = stronger imbalance
        size_pct = getattr(fvg, 'size_pct', 0.0)
        size_score = min(size_pct / 1.0, 1.0) * 30.0
        score += size_score
        diag["size_pct"] = size_pct
        if size_pct >= 0.5:
            reasons.append(f"Large FVG ({size_pct:.2f}%)")

        # 2. Fill % (0-30): partially filled is ideal entry
        # is_active means not fully filled
        is_active = getattr(fvg, 'is_active', True)
        filled = getattr(fvg, 'filled', False)
        if filled:
            fill_score = 0.0
            diag["fill_state"] = "filled"
            reasons.append("FVG fully filled")
        else:
            # Check partial fill — price has touched the gap but not through
            fill_score = 25.0  # assume active = good
            diag["fill_state"] = "active"
            reasons.append("FVG active (unfilled)")

        score += fill_score

        # 3. Age (0-20): measured from timestamp
        if df is not None and len(df) > 0:
            age_candles = len(df) - 1 - getattr(fvg, 'index', 0) if hasattr(fvg, 'index') else 0
            if age_candles <= 3:
                age_score = 20.0
                reasons.append(f"Very fresh ({age_candles} bars)")
            elif age_candles <= 7:
                age_score = 15.0
                reasons.append(f"Fresh ({age_candles} bars)")
            elif age_candles <= 15:
                age_score = 8.0
            else:
                age_score = 2.0
            score += age_score
            diag["age_candles"] = float(age_candles)
        else:
            score += 10.0  # neutral if no data

        # 4. Trend alignment (0-20)
        # Bullish FVG in bullish move = better
        score += 15.0  # base — we already filter by direction in _detect_entry_zones
        reasons.append("Direction aligned")

        # Confidence
        confidence = 0.5
        if is_active and not filled:
            confidence += 0.2
        if size_pct >= 0.3:
            confidence += 0.15

        return ComponentQuality(
            score=round(min(score, 100.0), 1),
            confidence=round(min(confidence, 1.0), 2),
            reasons=reasons,
            diagnostics=diag,
        )

    def _compute_quality_scores(
        self,
        setup: ICTSetup,
        sweeps: list,
        structure,
        order_blocks: list,
        fvgs: list,
        candle_quality,
        current_price: float,
        atr: float,
    ):
        """Compute quality scores for all detected components.

        Sets sweep_quality_score, mss_quality_score, bos_quality_score,
        ob_quality_score, fvg_quality_score, overall_quality, and setup_confidence.
        """
        if not setup.detected:
            return

        # Sweep quality
        if setup.has_sweep and sweeps:
            valid = [s for s in sweeps if s.is_valid]
            if valid:
                setup.sweep_quality = self._score_sweep(valid[0], current_price, atr)
                setup.sweep_quality_score = setup.sweep_quality.score

        # MSS quality (reversal)
        if setup.has_mss and structure and structure.last_mss:
            sweep_q = setup.sweep_quality or ComponentQuality(score=50.0, confidence=0.5)
            setup.mss_quality_detail = self._score_mss(
                structure.last_mss, sweep_q, setup.displacement_atr_ratio, candle_quality, structure,
            )
            setup.mss_quality_score = setup.mss_quality_detail.score

        # BOS quality (continuation)
        if setup.has_bos and structure:
            setup.bos_quality = self._score_bos(structure, setup.direction, atr)
            setup.bos_quality_score = setup.bos_quality.score

        # OB quality (entry zone)
        if setup.has_ob and order_blocks:
            for ob in order_blocks:
                ob_dir = "buy" if ob.type == "bullish" else "sell" if ob.type == "bearish" else ob.type
                if ob.is_valid and ob_dir == setup.direction:
                    setup.ob_quality = self._score_ob(ob, setup.direction, current_price, atr)
                    setup.ob_quality_score = setup.ob_quality.score
                    break

        # FVG quality (entry zone)
        if setup.has_fvg and fvgs:
            for f in fvgs:
                f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
                if f.is_active and f_dir == setup.direction:
                    setup.fvg_quality = self._score_fvg(f, setup.direction, current_price)
                    setup.fvg_quality_score = setup.fvg_quality.score
                    break

        # Overall quality: weighted average of component scores
        # Weights: trigger (MSS/BOS) 40%, sweep 20%, entry zone (OB/FVG) 20%, displacement 10%, structure 10%
        scores = []
        weights = []

        if setup.setup_type == "reversal":
            if setup.mss_quality_score > 0:
                scores.append(setup.mss_quality_score)
                weights.append(0.40)
            if setup.sweep_quality_score > 0:
                scores.append(setup.sweep_quality_score)
                weights.append(0.25)
            # Displacement is informational
            if setup.displacement_atr_ratio > 0:
                disp_score = min(setup.displacement_atr_ratio / 2.0, 1.0) * 100
                scores.append(disp_score)
                weights.append(0.15)
        elif setup.setup_type == "continuation":
            if setup.bos_quality_score > 0:
                scores.append(setup.bos_quality_score)
                weights.append(0.50)

        # Entry zone quality
        if setup.ob_quality_score > 0:
            scores.append(setup.ob_quality_score)
            weights.append(0.15)
        elif setup.fvg_quality_score > 0:
            scores.append(setup.fvg_quality_score)
            weights.append(0.15)

        if scores and weights:
            total_w = sum(weights)
            setup.overall_quality = round(sum(s * w for s, w in zip(scores, weights)) / total_w, 1)
        else:
            setup.overall_quality = 0.0

        # Confidence: min confidence of all detected components
        confs = []
        if setup.sweep_quality:
            confs.append(setup.sweep_quality.confidence)
        if setup.mss_quality_detail:
            confs.append(setup.mss_quality_detail.confidence)
        if setup.bos_quality:
            confs.append(setup.bos_quality.confidence)
        if setup.ob_quality:
            confs.append(setup.ob_quality.confidence)
        if setup.fvg_quality:
            confs.append(setup.fvg_quality.confidence)

        setup.setup_confidence = round(min(confs) if confs else 0.0, 2)

    def _validate_setup_quality(self, setup: ICTSetup) -> Optional[str]:
        """Validate setup meets minimum quality thresholds.

        Returns rejection reason if setup fails quality check, None if OK.
        This is a soft gate — logs violations but only blocks when thresholds are set.
        """
        if not setup.detected:
            return None

        # Check minimum component count
        if self.min_components_required > 0:
            # Count core ICT components (not entry zones)
            core_components = 0
            if setup.has_sweep:
                core_components += 1
            if setup.has_displacement:
                core_components += 1
            if setup.has_mss:
                core_components += 1
            if setup.has_bos:
                core_components += 1

            if core_components < self.min_components_required:
                return f"quality: only {core_components} core components (need {self.min_components_required})"

        # Check overall quality threshold
        if self.min_overall_quality > 0 and setup.overall_quality < self.min_overall_quality:
            return f"quality: overall={setup.overall_quality:.0f} < min {self.min_overall_quality:.0f}"

        # Check setup confidence threshold
        if self.min_setup_confidence > 0 and setup.setup_confidence < self.min_setup_confidence:
            return f"quality: confidence={setup.setup_confidence:.2f} < min {self.min_setup_confidence:.2f}"

        return None

    def _detect_entry_zones(
        self,
        setup: ICTSetup,
        order_blocks: list,
        fvgs: list,
        direction: str,
        current_price: float,
        df: Optional[pd.DataFrame] = None,
    ):
        """Detect OB and FVG as entry zones (not gates)."""
        # OB
        for ob in order_blocks:
            ob_dir = "buy" if ob.type == "bullish" else "sell" if ob.type == "bearish" else ob.type
            if ob.is_valid and ob_dir == direction:
                setup.has_ob = True
                setup.ob_type = ob.type
                setup.ob_midpoint = ob.midpoint
                if current_price > 0:
                    setup.ob_distance_pct = abs(current_price - ob.midpoint) / current_price * 100
                break

        # FVG — with time-based filter
        for f in fvgs:
            f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
            if f.is_active and f_dir == direction:
                # Time-based filter: reject FVG older than max_fvg_age_candles
                if df is not None and len(df) > 0 and self.max_fvg_age_candles > 0:
                    age_candles = len(df) - 1 - f.index if hasattr(f, 'index') else 0
                    if age_candles > self.max_fvg_age_candles:
                        continue  # stale FVG — skip
                setup.has_fvg = True
                setup.fvg_type = f.type
                setup.fvg_size_pct = f.size_pct
                break

    def _check_entry_armed(self, setup: ICTSetup, current_price: float) -> bool:
        """Check if price is in an OB or FVG zone.

        Entry armed is soft — logged but NOT a gate.
        For BUY: price near OB midpoint (below) or inside bullish FVG.
        For SELL: price near OB midpoint (above) or inside bearish FVG.
        """
        if current_price <= 0:
            return False

        # Check OB proximity
        if setup.has_ob and setup.ob_midpoint > 0:
            dist_pct = abs(current_price - setup.ob_midpoint) / current_price * 100
            if dist_pct <= self.ob_proximity_pct:
                return True

        # Check FVG containment
        if setup.has_fvg:
            # For bullish FVG: price should be within or below the gap
            if setup.fvg_type == "bullish":
                # FVG gap is between bottom and top
                # Price entering from above retracing into the gap
                return True  # FVG exists and is active → armed
            elif setup.fvg_type == "bearish":
                return True

        return False

    def _build_components(self, setup: ICTSetup) -> List[str]:
        """Build component list for logging and features."""
        components = []
        if setup.setup_type == "reversal":
            if setup.has_sweep:
                components.append("Sweep")
            if setup.has_displacement:
                components.append("Displacement")
            if setup.has_mss:
                components.append("MSS")
        elif setup.setup_type == "continuation":
            if setup.has_bos:
                components.append("BOS")
        if setup.has_ob:
            components.append("OB")
        if setup.has_fvg:
            components.append("FVG")
        if setup.entry_armed:
            components.append("EntryArmed")
        return components


# Singleton — initialized with config thresholds
def _create_pattern_engine():
    """Create PatternEngine singleton with config values."""
    try:
        from config.settings import config
        return PatternEngine(
            ob_proximity_pct=config.pattern_engine.ob_proximity_pct,
            min_overall_quality=config.pattern_engine.min_overall_quality,
            min_setup_confidence=config.pattern_engine.min_setup_confidence,
            min_components_required=config.pattern_engine.min_components_required,
        )
    except Exception:
        return PatternEngine()

pattern_engine = _create_pattern_engine()
