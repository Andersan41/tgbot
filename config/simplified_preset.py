"""
config/simplified_preset.py — Simplified core configuration.

Removes redundant gates, consolidates overlapping checks.
Goal: 15-18 gates from 32, keeping only independent, high-value filters.

REMOVED (redundant/unreachable):
- btc_global_trend: redundant with btc_correlation (4H is more relevant)
- no_trade_zones(ATR): unreachable after volatility gate
- no_trade_zones(market_structure): unreachable after signal_engine (requires BOS/sweep)
- no_trade_zones(tp_blocked): pass-through duplicate of tp_path
- context_block: consolidate into context_min_verdict
- ema_spread: weak predictor, removed as dead code in scanner
- candle_close: weak single-candle predictor
- min_score: weak reason-count proxy
- news: stub implementation, never blocks

KEPT (essential):
- cooldown
- portfolio_risk
- indicators
- compression_block (allow breakout mode)
- confirm_tf
- signal_engine (core trigger detection)
- distance_filter
- tp_path
- mtf_alignment
- btc_correlation (single BTC check)
- eth_correlation (for correlated tokens)
- volatility
- context_min_verdict (consolidated)
- sl_distance
- rr_guard
- dynamic_risk
- dedup

TOTAL: 17 gates (was 32)
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SimplifiedConfig:
    """Preset for simplified gate configuration.

    Apply via env: GATE_PRESET=simplified
    Or call apply_simplified_preset() to override config.
    """

    # signal_engine internal gates
    adx_filter_enabled: bool = True
    ema_alignment_enabled: bool = True
    ema_spread_enabled: bool = False   # REMOVED: weak predictor
    candle_close_enabled: bool = False  # REMOVED: weak predictor
    min_score_enabled: bool = False     # REMOVED: weak predictor
    compression_enabled: bool = True

    # scanner gates
    confirm_tf_enabled: bool = True
    tp_path_enabled: bool = False       # Keep disabled by default (needs SR data)
    mtf_enabled: bool = True

    # derivatives
    btc_correlation_enabled: bool = True
    btc_global_trend_filter: bool = False  # REMOVED: redundant with btc_correlation
    eth_correlation_enabled: bool = True

    # risk
    volatility_filter_enabled: bool = True
    dynamic_risk_enabled: bool = True

    # context
    context_enabled: bool = True
    context_min_verdict: str = "WEAK"
    context_block_on_blocked: bool = False  # REMOVED: consolidated into min_verdict

    # scoring
    confidence_v2_enabled: bool = True

    def to_env_dict(self) -> dict[str, str]:
        """Convert to environment variable overrides."""
        return {
            "ADX_FILTER_ENABLED": str(self.adx_filter_enabled).lower(),
            "EMA_ALIGNMENT_ENABLED": str(self.ema_alignment_enabled).lower(),
            "EMA_SPREAD_ENABLED": str(self.ema_spread_enabled).lower(),
            "CANDLE_CLOSE_ENABLED": str(self.candle_close_enabled).lower(),
            "MIN_SCORE_ENABLED": str(self.min_score_enabled).lower(),
            "COMPRESSION_ENABLED": str(self.compression_enabled).lower(),
            "CONFIRM_TF_ENABLED": str(self.confirm_tf_enabled).lower(),
            "TP_PATH_ENABLED": str(self.tp_path_enabled).lower(),
            "MTF_ENABLED": str(self.mtf_enabled).lower(),
            "BTC_CORRELATION_ENABLED": str(self.btc_correlation_enabled).lower(),
            "BTC_GLOBAL_TREND_FILTER": str(self.btc_global_trend_filter).lower(),
            "ETH_CORRELATION_ENABLED": str(self.eth_correlation_enabled).lower(),
            "VOLATILITY_FILTER_ENABLED": str(self.volatility_filter_enabled).lower(),
            "DYNAMIC_RISK_ENABLED": str(self.dynamic_risk_enabled).lower(),
            "CONTEXT_MIN_VERDICT": self.context_min_verdict,
            "CONTEXT_BLOCK_ON_BLOCKED": str(self.context_block_on_blocked).lower(),
        }

    def summary(self) -> str:
        """Human-readable summary of what's enabled/disabled."""
        lines = ["Simplified Gate Configuration:"]
        lines.append("")
        lines.append("  KEPT (17 gates):")
        for name in [
            "cooldown", "portfolio_risk", "indicators",
            "compression_block", "confirm_tf", "signal_engine",
            "distance_filter", "mtf_alignment",
            "btc_correlation", "eth_correlation",
            "volatility", "context_min_verdict",
            "sl_distance", "rr_guard", "dynamic_risk", "dedup",
        ]:
            lines.append(f"    + {name}")
        if self.tp_path_enabled:
            lines.append("    + tp_path")

        lines.append("")
        lines.append("  REMOVED (15 gates):")
        for name, reason in [
            ("btc_global_trend", "redundant with btc_correlation"),
            ("no_trade_zones(ATR)", "unreachable after volatility"),
            ("no_trade_zones(ranging)", "unreachable after signal_engine"),
            ("no_trade_zones(tp_blocked)", "pass-through of tp_path"),
            ("no_trade_zones(OI)", "weak predictor"),
            ("context_block", "consolidated into context_min_verdict"),
            ("ema_spread", "weak predictor"),
            ("candle_close", "weak predictor"),
            ("min_score", "weak predictor"),
            ("news", "stub, never blocks"),
            ("distance_filter", "never called from scanner"),
            ("require_ob_or_fvg", "dead config, never read"),
        ]:
            lines.append(f"    - {name}: {reason}")

        return "\n".join(lines)


# Presets
PRESETS = {
    "simplified": SimplifiedConfig(),
    "aggressive": SimplifiedConfig(
        # Even more aggressive: remove MTF, ETH, confirm_tf
        mtf_enabled=False,
        eth_correlation_enabled=False,
        confirm_tf_enabled=False,
    ),
}


def get_preset(name: str) -> SimplifiedConfig:
    """Get a named preset."""
    if name not in PRESETS:
        raise ValueError(f"Unknown preset: {name}. Available: {list(PRESETS.keys())}")
    return PRESETS[name]
