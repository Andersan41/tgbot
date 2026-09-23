"""
analytics/performance.py — Performance analytics for closed trades.

Computes winrate, profit factor, expectancy, max drawdown, MFE/MAE,
and filter counterfactual analysis from SignalOutcome + Signal + DecisionTrace.

All functions operate on CLOSED trades only (HIT_TP / HIT_SL).
EXPIRED trades are counted separately.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

from loguru import logger
from sqlalchemy import select, desc

from storage.database import db, Signal, SignalOutcome, DecisionTrace


# ── Session detection (matches feature_builder._SESSION_MAP) ────────────

_SESSION_MAP = {
    "asian": (0, 7),
    "london": (7, 12),
    "overlap": (12, 16),
    "new_york": (16, 21),
}


def _detect_session(dt: datetime) -> str:
    hour = dt.hour
    for session, (start, end) in _SESSION_MAP.items():
        if start <= hour < end:
            return session
    return "off_hours"


def _parse_period(period: str | None) -> Optional[timedelta]:
    """Parse period string like '7d', '30d', '24h', 'full'."""
    if period is None or period == "full":
        return None
    period = period.strip().lower()
    if period.endswith("d"):
        return timedelta(days=int(period[:-1]))
    if period.endswith("h"):
        return timedelta(hours=int(period[:-1]))
    if period.endswith("w"):
        return timedelta(weeks=int(period[:-1]))
    return None


# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class WinrateStats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    winrate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0  # expectancy in R-multiples
    net_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    avg_rr: float = 0.0


@dataclass
class SegmentStats:
    segment: str
    stats: WinrateStats


@dataclass
class MfeMaeRecord:
    signal_id: int
    symbol: str
    timeframe: str
    direction: str
    outcome: str  # HIT_TP / HIT_SL
    entry_price: float
    mfe_pct: float
    mae_pct: float
    pnl_pct: float
    rr_ratio: float


@dataclass
class MfeMaeReport:
    total: int = 0
    tp_records: list[MfeMaeRecord] = field(default_factory=list)
    sl_records: list[MfeMaeRecord] = field(default_factory=list)
    # TP stats
    tp_median_mfe: float = 0.0
    tp_median_mae: float = 0.0
    tp_avg_mfe: float = 0.0
    tp_avg_mae: float = 0.0
    # SL stats
    sl_median_mfe: float = 0.0
    sl_median_mae: float = 0.0
    sl_avg_mfe: float = 0.0
    sl_avg_mae: float = 0.0
    # MFE in R for losing trades
    sl_mfe_in_r_median: float = 0.0
    sl_mfe_in_r_avg: float = 0.0


@dataclass
class FilterCounterfactual:
    gate: str
    blocked_count: int  # trades blocked by this gate (that would pass others)
    would_add_tp: int  # HIT_TP trades that would be added
    would_add_sl: int  # HIT_SL trades that would be added
    add_wr: float  # winrate of would-add trades
    baseline_wr: float
    baseline_pf: float
    new_wr: float
    new_pf: float
    wr_delta: float
    pf_delta: float
    expectancy_delta_r: float
    verdict: str  # REMOVABLE / ESSENTIAL / NEUTRAL / MIXED / NO_DATA


# ── Legacy gates from audit section 3 ─────────────────────────────────

LEGACY_FILTERS = [
    "btc_global_trend",
    "confirm_tf",
    "distance_filter",
    "tp_path",
    "mtf_alignment",
    "btc_correlation",
    "eth_correlation",
    "volatility",
    "context_block",
    "context_min_verdict",
    "news",
    "sl_distance",
    "rr_guard",
    "no_trade_zones",
    "dynamic_risk",
    "confidence_v2",
    "compression_block",
]


# ── Core functions ─────────────────────────────────────────────────────

async def _load_closed_trades(
    period: Optional[timedelta] = None,
) -> list[tuple[SignalOutcome, Signal]]:
    """Load closed trades (HIT_TP + HIT_SL) optionally filtered by period."""
    async with db._session_factory() as session:
        query = (
            select(SignalOutcome, Signal)
            .join(Signal, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.status.in_(["HIT_TP", "HIT_SL"]))
        )
        if period is not None:
            cutoff = datetime.now(timezone.utc) - period
            query = query.where(SignalOutcome.closed_at >= cutoff)
        query = query.order_by(SignalOutcome.closed_at.asc())
        result = await session.execute(query)
        return [(o, s) for o, s in result.all()]


async def _load_all_outcomes(
    period: Optional[timedelta] = None,
) -> list[tuple[SignalOutcome, Signal]]:
    """Load ALL closed outcomes (HIT_TP + HIT_SL + EXPIRED + MANUAL_CLOSE) optionally filtered."""
    async with db._session_factory() as session:
        query = (
            select(SignalOutcome, Signal)
            .join(Signal, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.status.in_(["HIT_TP", "HIT_SL", "EXPIRED", "MANUAL_CLOSE"]))
        )
        if period is not None:
            cutoff = datetime.now(timezone.utc) - period
            query = query.where(SignalOutcome.closed_at >= cutoff)
        query = query.order_by(SignalOutcome.closed_at.asc())
        result = await session.execute(query)
        return [(o, s) for o, s in result.all()]


def _compute_stats(pnls: list[float], sl_distances: list[float] | None = None) -> WinrateStats:
    """Compute core metrics from lists of PnL values."""
    if not pnls:
        return WinrateStats()

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total = len(pnls)
    win_count = len(wins)
    loss_count = len(losses)
    winrate = win_count / total * 100 if total > 0 else 0.0

    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    avg_win = gross_profit / win_count if win_count > 0 else 0.0
    avg_loss = gross_loss / loss_count if loss_count > 0 else 0.0

    # Expectancy in R: WR * avg_R_win - (1-WR) * avg_R_loss
    # Using avg_win/avg_loss as proxy for R-multiples (normalized by avg SL)
    if sl_distances and len(sl_distances) == len(pnls):
        r_values = []
        for pnl, sl_dist in zip(pnls, sl_distances):
            if sl_dist > 0:
                r_values.append(pnl / sl_dist)  # PnL in R-multiples
            else:
                r_values.append(0.0)
        r_wins = [r for r, p in zip(r_values, pnls) if p > 0]
        r_losses = [r for r, p in zip(r_values, pnls) if p < 0]
        avg_r_win = sum(r_wins) / len(r_wins) if r_wins else 0.0
        avg_r_loss = abs(sum(r_losses)) / len(r_losses) if r_losses else 0.0
        expectancy_r = winrate / 100 * avg_r_win - (1 - winrate / 100) * avg_r_loss
    else:
        # Fallback: normalize by average absolute PnL
        avg_abs_loss = avg_loss if avg_loss > 0 else 1.0
        avg_r_win = avg_win / avg_abs_loss if avg_abs_loss > 0 else 0.0
        avg_r_loss = 1.0
        expectancy_r = winrate / 100 * avg_r_win - (1 - winrate / 100) * avg_r_loss

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return WinrateStats(
        total=total,
        wins=win_count,
        losses=loss_count,
        winrate=round(winrate, 1),
        profit_factor=round(pf, 2),
        expectancy_r=round(expectancy_r, 3),
        net_pnl_pct=round(sum(pnls), 2),
        max_drawdown_pct=round(max_dd, 2),
        avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2),
        best_trade_pct=round(max(pnls), 2),
        worst_trade_pct=round(min(pnls), 2),
        avg_rr=round(avg_r_win, 2) if sl_distances else 0.0,
    )


async def overall_stats(period: str | None = None) -> WinrateStats:
    """Compute overall performance stats for a period.

    Args:
        period: '7d', '30d', '24h', 'full', or None (=all time)
    """
    td = _parse_period(period)

    # Load outcomes
    all_outcomes = await _load_all_outcomes(td)
    closed = [(o, s) for o, s in all_outcomes if o.status in ("HIT_TP", "HIT_SL", "EXPIRED", "MANUAL_CLOSE")]
    expired = sum(1 for o, s in all_outcomes if o.status == "EXPIRED")

    pnls = [o.pnl_pct for o, s in closed if o.pnl_pct is not None]
    sl_distances = []
    for o, s in closed:
        if s.sl and s.close_price and s.close_price > 0:
            sl_distances.append(abs(s.close_price - s.sl) / s.close_price * 100)
        else:
            sl_distances.append(0.0)

    stats = _compute_stats(pnls, sl_distances)
    stats.expired = expired
    return stats


async def segmented_stats(
    by: Literal["symbol", "timeframe", "direction", "session", "regime"],
    period: str | None = None,
) -> dict[str, WinrateStats]:
    """Compute stats segmented by a dimension.

    Args:
        by: 'symbol', 'timeframe', 'direction', 'session', or 'regime'
        period: period string or None
    """
    td = _parse_period(period)
    closed = await _load_closed_trades(td)

    groups: dict[str, list[tuple[SignalOutcome, Signal, float]]] = {}

    # For regime: load regime map upfront
    regime_map: dict[int, str] = {}
    if by == "regime":
        async with db._session_factory() as session:
            traces = await session.execute(
                select(DecisionTrace.signal_id, DecisionTrace.regime)
                .where(DecisionTrace.regime.isnot(None))
            )
            for signal_id, regime in traces.all():
                regime_map[signal_id] = regime or "unknown"

    for outcome, signal in closed:
        if outcome.pnl_pct is None:
            continue

        if by == "symbol":
            key = signal.symbol
        elif by == "timeframe":
            key = signal.timeframe
        elif by == "direction":
            key = signal.signal_type
        elif by == "session":
            ts = signal.sent_at or signal.created_at
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            key = _detect_session(ts) if ts else "unknown"
        elif by == "regime":
            key = regime_map.get(signal.id, "unknown")
        else:
            key = "unknown"

        groups.setdefault(key, []).append((outcome, signal, outcome.pnl_pct))

    result: dict[str, WinrateStats] = {}
    for key, items in groups.items():
        pnls = [p for _, _, p in items]
        sl_dists = []
        for o, s, _ in items:
            if s.sl and s.close_price and s.close_price > 0:
                sl_dists.append(abs(s.close_price - s.sl) / s.close_price * 100)
            else:
                sl_dists.append(0.0)
        result[key] = _compute_stats(pnls, sl_dists)

    return result


# ── MFE/MAE Analysis ──────────────────────────────────────────────────

async def mfe_mae_analysis(period: str | None = None) -> MfeMaeReport:
    """Analyze Maximum Favorable/Adverse Excursion for closed trades."""
    td = _parse_period(period)
    closed = await _load_closed_trades(td)

    records: list[MfeMaeRecord] = []
    sig_map: dict[int, Signal] = {}
    for outcome, signal in closed:
        sig_map[signal.id] = signal
        if outcome.pnl_pct is None:
            continue
        if signal.mfe_pct is None or signal.mae_pct is None:
            continue
        if signal.close_price and signal.close_price > 0 and signal.sl:
            sl_dist = abs(signal.close_price - signal.sl) / signal.close_price * 100
            rr = abs(signal.tp - signal.close_price) / abs(signal.close_price - signal.sl) if signal.tp and signal.sl and signal.close_price != signal.sl else 0.0
        else:
            rr = 0.0

        records.append(MfeMaeRecord(
            signal_id=signal.id,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            direction=signal.signal_type,
            outcome=outcome.status,
            entry_price=signal.close_price,
            mfe_pct=signal.mfe_pct,
            mae_pct=signal.mae_pct,
            pnl_pct=outcome.pnl_pct,
            rr_ratio=rr,
        ))

    tp_records = [r for r in records if r.outcome == "HIT_TP"]
    sl_records = [r for r in records if r.outcome == "HIT_SL"]

    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        if n % 2 == 0:
            return (s[n // 2 - 1] + s[n // 2]) / 2
        return s[n // 2]

    report = MfeMaeReport(total=len(records), tp_records=tp_records, sl_records=sl_records)

    if tp_records:
        report.tp_median_mfe = round(_median([r.mfe_pct for r in tp_records]), 2)
        report.tp_median_mae = round(_median([r.mae_pct for r in tp_records]), 2)
        report.tp_avg_mfe = round(sum(r.mfe_pct for r in tp_records) / len(tp_records), 2)
        report.tp_avg_mae = round(sum(r.mae_pct for r in tp_records) / len(tp_records), 2)

    if sl_records:
        report.sl_median_mfe = round(_median([r.mfe_pct for r in sl_records]), 2)
        report.sl_median_mae = round(_median([r.mae_pct for r in sl_records]), 2)
        report.sl_avg_mfe = round(sum(r.mfe_pct for r in sl_records) / len(sl_records), 2)
        report.sl_avg_mae = round(sum(r.mae_pct for r in sl_records) / len(sl_records), 2)

        # MFE in R: MFE / SL_distance (how far price went in our favor before hitting SL)
        mfe_r = []
        for rec in sl_records:
            sig = sig_map.get(rec.signal_id)
            if sig and sig.sl and rec.entry_price and rec.entry_price > 0:
                sl_dist = abs(rec.entry_price - sig.sl) / rec.entry_price * 100
                if sl_dist > 0:
                    mfe_r.append(rec.mfe_pct / sl_dist)
        if mfe_r:
            report.sl_mfe_in_r_median = round(_median(mfe_r), 2)
            report.sl_mfe_in_r_avg = round(sum(mfe_r) / len(mfe_r), 2)

    return report


# ── Filter Counterfactual ──────────────────────────────────────────────

async def filter_counterfactual(period: str | None = None) -> list[FilterCounterfactual]:
    """For each legacy filter, compute counterfactual impact.

    Uses DecisionTrace gate columns to determine which trades were blocked
    by each gate, then looks up outcomes for those trades.
    """
    td = _parse_period(period)

    # Load all traces with outcomes
    async with db._session_factory() as session:
        query = select(DecisionTrace)
        if td is not None:
            cutoff = datetime.now(timezone.utc) - td
            query = query.where(DecisionTrace.timestamp >= cutoff)
        result = await session.execute(query)
        traces = list(result.scalars().all())

    if not traces:
        return []

    # Baseline: traces that generated a signal
    baseline_signals = [t for t in traces if t.signal_generated]
    baseline_wins = sum(1 for t in baseline_signals if t.outcome == "HIT_TP")
    baseline_losses = sum(1 for t in baseline_signals if t.outcome == "HIT_SL")
    baseline_closed = baseline_wins + baseline_losses
    baseline_wr = baseline_wins / baseline_closed * 100 if baseline_closed > 0 else 0.0

    baseline_pnls = [t.pnl_pct for t in baseline_signals if t.pnl_pct is not None]
    gp = sum(p for p in baseline_pnls if p > 0)
    gl = abs(sum(p for p in baseline_pnls if p < 0))
    baseline_pf = round(gp / gl, 2) if gl > 0 else (float("inf") if gp > 0 else 0.0)

    # Expectancy in R (baseline)
    baseline_expectancy = 0.0
    if baseline_signals:
        rr_vals = []
        for t in baseline_signals:
            if t.rr_ratio and t.rr_ratio > 0 and t.pnl_pct is not None:
                rr_vals.append(t.pnl_pct / (100 / t.rr_ratio) if t.rr_ratio else 0.0)
        if rr_vals:
            baseline_expectancy = sum(rr_vals) / len(rr_vals)

    results: list[FilterCounterfactual] = []

    for gate in LEGACY_FILTERS:
        col_name = f"gate_{gate}"

        # Check if this column exists on DecisionTrace
        if not hasattr(DecisionTrace, col_name):
            results.append(FilterCounterfactual(
                gate=gate, blocked_count=0, would_add_tp=0, would_add_sl=0,
                add_wr=0.0, baseline_wr=round(baseline_wr, 1),
                baseline_pf=baseline_pf, new_wr=round(baseline_wr, 1),
                new_pf=baseline_pf, wr_delta=0.0, pf_delta=0.0,
                expectancy_delta_r=0.0, verdict="NO_DATA",
            ))
            continue

        # Find traces blocked by this gate that would pass all OTHER gates
        would_add = []
        gate_order = [
            "cooldown", "portfolio_risk", "btc_global_trend", "indicators",
            "confirm_tf", "signal_engine", "distance_filter", "tp_path",
            "mtf_alignment", "btc_correlation", "eth_correlation", "volatility",
            "context_timeout", "context_block", "context_min_verdict",
            "news", "sl_distance", "rr_guard", "no_trade_zones",
            "dynamic_risk", "confidence_v2", "dedup", "compression_block",
        ]

        for t in traces:
            if t.signal_generated:
                continue
            # Check if blocked by this gate
            gate_val = getattr(t, col_name, None)
            if gate_val is not False:
                continue
            # Check if would pass all other gates
            would_pass = True
            for other_gate in gate_order:
                if other_gate == gate:
                    continue
                other_col = f"gate_{other_gate}"
                if not hasattr(DecisionTrace, other_col):
                    continue
                other_val = getattr(t, other_col, None)
                if other_val is False:
                    would_pass = False
                    break
            if would_pass:
                would_add.append(t)

        if not would_add:
            results.append(FilterCounterfactual(
                gate=gate, blocked_count=0, would_add_tp=0, would_add_sl=0,
                add_wr=0.0, baseline_wr=round(baseline_wr, 1),
                baseline_pf=baseline_pf, new_wr=round(baseline_wr, 1),
                new_pf=baseline_pf, wr_delta=0.0, pf_delta=0.0,
                expectancy_delta_r=0.0, verdict="NO_DATA",
            ))
            continue

        add_tp = sum(1 for t in would_add if t.outcome == "HIT_TP")
        add_sl = sum(1 for t in would_add if t.outcome == "HIT_SL")
        add_closed = add_tp + add_sl
        add_wr = add_tp / add_closed * 100 if add_closed > 0 else 0.0

        new_total = baseline_closed + add_closed
        new_wins = baseline_wins + add_tp
        new_wr = new_wins / new_total * 100 if new_total > 0 else baseline_wr

        add_pnls = [t.pnl_pct for t in would_add if t.pnl_pct is not None]
        all_pnls = list(baseline_pnls) + add_pnls
        new_gp = sum(p for p in all_pnls if p > 0)
        new_gl = abs(sum(p for p in all_pnls if p < 0))
        new_pf = round(new_gp / new_gl, 2) if new_gl > 0 else baseline_pf

        # Expectancy delta
        add_rr = []
        for t in would_add:
            if t.rr_ratio and t.rr_ratio > 0 and t.pnl_pct is not None:
                add_rr.append(t.pnl_pct / (100 / t.rr_ratio) if t.rr_ratio else 0.0)
        add_expectancy = sum(add_rr) / len(add_rr) if add_rr else 0.0
        expectancy_delta = add_expectancy - baseline_expectancy

        wr_delta = round(new_wr - baseline_wr, 1)
        pf_delta = round(new_pf - baseline_pf, 2)

        # Verdict
        if add_closed == 0:
            verdict = "NO_DATA"
        elif abs(wr_delta) < 0.5 and abs(pf_delta) < 0.05:
            verdict = "NEUTRAL"
        elif wr_delta > 0.5 and new_pf >= baseline_pf:
            verdict = "REMOVABLE"
        elif wr_delta < -1.0 or new_pf < baseline_pf - 0.1:
            verdict = "ESSENTIAL"
        else:
            verdict = "MIXED"

        results.append(FilterCounterfactual(
            gate=gate,
            blocked_count=len(would_add),
            would_add_tp=add_tp,
            would_add_sl=add_sl,
            add_wr=round(add_wr, 1),
            baseline_wr=round(baseline_wr, 1),
            baseline_pf=baseline_pf,
            new_wr=round(new_wr, 1),
            new_pf=new_pf,
            wr_delta=wr_delta,
            pf_delta=pf_delta,
            expectancy_delta_r=round(expectancy_delta, 3),
            verdict=verdict,
        ))

    return results


# ── Formatting helpers ─────────────────────────────────────────────────

def format_overall_stats(s: WinrateStats, period_label: str = "all time") -> str:
    if s.total == 0:
        return f"📊 Нет закрытых сделок ({period_label})"
    return (
        f"📊 <b>Статистика ({period_label})</b>\n\n"
        f"Всего: <b>{s.total}</b> (PnL+: {s.wins} | PnL−: {s.losses} | EX: {s.expired})\n"
        f"Winrate: <b>{s.winrate:.1f}%</b>\n"
        f"Profit Factor: <b>{s.profit_factor:.2f}</b>\n"
        f"Expectancy: <b>{s.expectancy_r:+.3f}R</b>\n"
        f"Net PnL: <b>{s.net_pnl_pct:+.2f}%</b>\n"
        f"Max Drawdown: <b>{s.max_drawdown_pct:.2f}%</b>\n"
        f"Avg Win: <b>{s.avg_win_pct:+.2f}%</b> | Avg Loss: <b>{s.avg_loss_pct:+.2f}%</b>\n"
        f"Best: <b>{s.best_trade_pct:+.2f}%</b> | Worst: <b>{s.worst_trade_pct:+.2f}%</b>"
    )


def format_segmented_stats(stats: dict[str, WinrateStats], label: str) -> str:
    if not stats:
        return f"📊 Нет данных по сегментам ({label})"

    lines = [f"📊 <b>Сегментация: {label}</b>\n"]
    lines.append("| Сегмент | Сделок | WR | PF | Exp(R) | Net PnL |")
    lines.append("|---------|--------|-----|-----|--------|---------|")

    for seg, s in sorted(stats.items(), key=lambda x: -x[1].winrate):
        if s.total == 0:
            continue
        lines.append(
            f"| {seg} | {s.total} | {s.winrate:.0f}% | {s.profit_factor:.1f} "
            f"| {s.expectancy_r:+.2f}R | {s.net_pnl_pct:+.1f}% |"
        )

    return "\n".join(lines)


def format_mfe_mae(r: MfeMaeReport) -> str:
    if r.total == 0:
        return "📊 MFE/MAE: нет данных (нужны mfe_pct/mae_pct в signal)"

    lines = ["📊 <b>MFE/MAE Анализ</b>\n"]

    if r.tp_records:
        lines.append(f"<b>HIT_TP ({len(r.tp_records)} сделок):</b>")
        lines.append(f"  MFE: медиана={r.tp_median_mfe:.2f}% сред={r.tp_avg_mfe:.2f}%")
        lines.append(f"  MAE: медиана={r.tp_median_mae:.2f}% сред={r.tp_avg_mae:.2f}%")

    if r.sl_records:
        lines.append(f"\n<b>HIT_SL ({len(r.sl_records)} сделок):</b>")
        lines.append(f"  MFE: медиана={r.sl_median_mfe:.2f}% сред={r.sl_avg_mfe:.2f}%")
        lines.append(f"  MAE: медиана={r.sl_median_mae:.2f}% сред={r.sl_avg_mae:.2f}%")
        lines.append(f"  MFE в R: медиана={r.sl_mfe_in_r_median:.2f}R сред={r.sl_avg_mfe:.2f}R")
        lines.append(f"  → Упущенная прибыль: проигравшие сделки уходили в среднем на {r.sl_avg_mfe:.2f}% в нашу сторону")

    return "\n".join(lines)


def format_counterfactual(results: list[FilterCounterfactual]) -> str:
    if not results:
        return "📊 Counterfactual: нет данных"

    lines = ["📊 <b>Counterfactual: Legacy Filters</b>\n"]
    lines.append("| Фильтр | Blocked | Add TP | Add SL | Add WR | ΔWR | Verdict |")
    lines.append("|---------|---------|--------|--------|--------|-----|---------|")

    for r in sorted(results, key=lambda x: x.wr_delta):
        if r.verdict == "NO_DATA":
            emoji = "⬜"
        elif r.verdict == "REMOVABLE":
            emoji = "✅"
        elif r.verdict == "ESSENTIAL":
            emoji = "❌"
        elif r.verdict == "MIXED":
            emoji = "⚠️"
        else:
            emoji = "➖"

        lines.append(
            f"| {r.gate} | {r.blocked_count} | {r.would_add_tp} | {r.would_add_sl} "
            f"| {r.add_wr:.0f}% | {r.wr_delta:+.1f}% | {emoji} {r.verdict} |"
        )

    # Summary
    removable = [r for r in results if r.verdict == "REMOVABLE"]
    essential = [r for r in results if r.verdict == "ESSENTIAL"]
    no_data = [r for r in results if r.verdict == "NO_DATA"]

    lines.append("")
    if removable:
        lines.append(f"✅ <b>Кандидаты на удаление:</b> {', '.join(r.gate for r in removable)}")
    if essential:
        lines.append(f"❌ <b>Не трогать:</b> {', '.join(r.gate for r in essential)}")
    if no_data:
        lines.append(f"⬜ <b>Нет данных:</b> {', '.join(r.gate for r in no_data)}")

    return "\n".join(lines)


def format_stats_telegram(s: WinrateStats, period_label: str) -> str:
    """Compact Telegram format."""
    if s.total == 0:
        return f"📊 Нет сделок ({period_label})"
    return (
        f"📊 <b>{period_label}</b>: {s.total} сделок | "
        f"WR <b>{s.winrate:.0f}%</b> | PF <b>{s.profit_factor:.1f}</b> | "
        f"Exp <b>{s.expectancy_r:+.2f}R</b> | "
        f"PnL <b>{s.net_pnl_pct:+.1f}%</b> | DD <b>{s.max_drawdown_pct:.1f}%</b>"
    )


def format_segmented_telegram(stats: dict[str, WinrateStats], label: str) -> str:
    """Compact segmented Telegram format."""
    if not stats:
        return f"📊 Нет данных ({label})"
    lines = [f"📊 <b>{label}:</b>"]
    for seg, s in sorted(stats.items(), key=lambda x: -x[1].winrate):
        if s.total == 0:
            continue
        lines.append(f"  {seg}: {s.total} trades, WR {s.winrate:.0f}%, PF {s.profit_factor:.1f}")
    return "\n".join(lines)
