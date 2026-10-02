"""
analytics/daily_report.py — Ежедневный отчёт по сделкам.

Генерирует Markdown-файл reports/daily/YYYY-MM-DD.md со статистикой:
- Закрытые сделки за день
- Открытые сделки и текущий PnL
- Винрейт, средний PnL, profit factor
- R-множители (PnL / дистанция SL): средний R, суммарный R, PF в R
- Лучшая/худшая сделка
- Breakdown по TF
- WR по фильтрам (символ, направление, setup_type, режим, ADX-бакет) —
  живая нарезка исходов для ручной перестройки порогов фильтров
- Заблокировано гейтами (live-воронка из decision_traces.final_stage)

Usage:
    python -m analytics.daily_report [--days 1] [--output reports/daily/]
"""
from __future__ import annotations

import asyncio
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import db, Signal, SignalOutcome, DecisionTrace
from sqlalchemy import select, desc, func


REPORT_DIR = Path(__file__).resolve().parent.parent / "reports" / "daily"


async def get_daily_data(date: Optional[datetime] = None) -> dict:
    """Collect trading data for a specific day (UTC)."""
    if date is None:
        date = datetime.now(timezone.utc)

    day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with db._session_factory() as session:
        # Closed outcomes for this day
        result = await session.execute(
            select(SignalOutcome, Signal)
            .join(Signal, SignalOutcome.signal_id == Signal.id)
            .where(
                SignalOutcome.status != "OPEN",
                SignalOutcome.closed_at >= day_start,
                SignalOutcome.closed_at < day_end,
            )
            .order_by(SignalOutcome.closed_at.desc())
        )
        closed = [(outcome, signal) for outcome, signal in result.all()]

        # All open outcomes (for current state)
        result2 = await session.execute(
            select(SignalOutcome, Signal)
            .join(Signal, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.status == "OPEN")
            .order_by(Signal.created_at.desc())
        )
        open_trades = [(outcome, signal) for outcome, signal in result2.all()]

        # All closed outcomes (for all-time stats)
        result3 = await session.execute(
            select(SignalOutcome, Signal)
            .join(Signal, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.status != "OPEN")
            .order_by(SignalOutcome.closed_at.desc())
        )
        all_closed = [(outcome, signal) for outcome, signal in result3.all()]

        # Trace features (regime / ADX / setup_type) for the closed signals —
        # used by the WR-by-filter slices below.
        trace_by_signal: dict[int, dict] = {}
        signal_ids = [s.id for _, s in all_closed if getattr(s, "id", None) is not None]
        if signal_ids:
            result4 = await session.execute(
                select(
                    DecisionTrace.signal_id,
                    DecisionTrace.regime,
                    DecisionTrace.adx,
                    DecisionTrace.feature_snapshot,
                ).where(DecisionTrace.signal_id.in_(signal_ids))
            )
            for sid, regime, adx, snapshot in result4.all():
                setup_type = None
                if snapshot:
                    import json as _json
                    try:
                        setup_type = (_json.loads(snapshot) or {}).get("setup_type")
                    except _json.JSONDecodeError:
                        setup_type = None
                trace_by_signal[sid] = {
                    "regime": regime,
                    "adx": adx,
                    "setup_type": setup_type,
                }

        # Live funnel: how many candidates each gate blocked (all-time).
        result5 = await session.execute(
            select(DecisionTrace.final_stage, func.count(DecisionTrace.id))
            .where(
                DecisionTrace.signal_generated.is_(False),
                DecisionTrace.final_stage.isnot(None),
            )
            .group_by(DecisionTrace.final_stage)
        )
        gate_blocks = {stage: cnt for stage, cnt in result5.all()}

        passed_candidates = await session.execute(
            select(func.count(DecisionTrace.id)).where(
                DecisionTrace.signal_generated.is_(True)
            )
        )
        candidates_passed = int(passed_candidates.scalar_one())

    return {
        "date": day_start,
        "closed_today": closed,
        "open_trades": open_trades,
        "all_closed": all_closed,
        "trace_by_signal": trace_by_signal,
        "gate_blocks": gate_blocks,
        "candidates_passed": candidates_passed,
    }


def _calc_profit_factor(pnls: list[float]) -> float | None:
    """Calculate profit factor from list of PnL values.

    Returns None if no observed loss exists (undefined PF).
    """
    import math
    values = [float(p) for p in pnls if p is not None and math.isfinite(float(p))]
    gains = sum(p for p in values if p > 0)
    losses = -sum(p for p in values if p < 0)
    return gains / losses if losses > 0 else None


def _sl_distance_pct(signal) -> float:
    """Entry-to-SL distance in % — the R unit for this trade."""
    if not signal.close_price or signal.sl is None:
        return 0.0
    return abs(signal.close_price - signal.sl) / signal.close_price * 100


def _r_multiple(outcome, signal) -> float | None:
    """PnL in R-multiples: pnl_pct / initial SL distance (%).

    None when PnL is unknown or the trade has no usable SL distance
    (R is undefined for such a trade — it is excluded from R stats).
    """
    import math
    if outcome.pnl_pct is None:
        return None
    pnl = float(outcome.pnl_pct)
    if not math.isfinite(pnl):
        return None
    sl_pct = _sl_distance_pct(signal)
    if sl_pct <= 0:
        return None
    return pnl / sl_pct


def _r_stats(rows: list) -> dict:
    """Aggregate R-multiples over [(outcome, signal), ...] rows."""
    rs = [r for r in (_r_multiple(o, s) for o, s in rows) if r is not None]
    if not rs:
        return {"count": 0, "avg": None, "total": None, "pf": None}
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    gross_loss = sum(losses)
    return {
        "count": len(rs),
        "avg": sum(rs) / len(rs),
        "total": sum(rs),
        "pf": (sum(wins) / gross_loss) if gross_loss > 0 else None,
    }


def _fmt_r(value: float | None) -> str:
    return f"{value:+.2f}R" if value is not None else "—"


# ── WR по фильтрам (слайсы живых исходов) ────────────────────────────────

def _adx_bucket(adx: float | None) -> str:
    if adx is None:
        return "—"
    if adx < 20:
        return "<20"
    if adx < 25:
        return "20-25"
    if adx < 30:
        return "25-30"
    return ">=30"


def _slice_stats(rows: list) -> dict:
    """WR / avg PnL / PF / R for one slice of [(outcome, signal), ...]."""
    pnls = [
        float(o.pnl_pct) for o, s in rows
        if o.pnl_pct is not None and math.isfinite(float(o.pnl_pct))
    ]
    wins = sum(p > 0 for p in pnls)
    return {
        "n": len(rows),
        "wr": (wins / len(pnls) * 100) if pnls else None,
        "avg": (sum(pnls) / len(pnls)) if pnls else None,
        "pf": _calc_profit_factor(pnls),
        "r": _r_stats(rows),
    }


def _append_slice_table(lines: list[str], groups: dict[str, list]) -> None:
    """Render one slice table sorted by sample size (largest first)."""
    lines.append("| Значение | Сделок | Винрейт | Средний PnL | Средний R | PF |")
    lines.append("|----------|--------|---------|-------------|-----------|----|")
    for key in sorted(groups, key=lambda k: (-len(groups[k]), str(k))):
        st = _slice_stats(groups[key])
        wr = f"{st['wr']:.1f}%" if st["wr"] is not None else "—"
        avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        pf = f"{st['pf']:.2f}" if st["pf"] is not None else "—"
        lines.append(
            f"| {key} | {st['n']} | {wr} | {avg} | {_fmt_r(st['r']['avg'])} | {pf} |"
        )
    lines.append("")


def format_daily_report(data: dict) -> str:
    """Format daily data as Markdown report."""
    date = data["date"]
    closed = data["closed_today"]
    open_trades = data["open_trades"]
    all_closed = data["all_closed"]

    lines = [
        f"# Daily Report — {date.strftime('%Y-%m-%d')}",
        "",
    ]

    # === Closed Today ===
    lines.append("## Закрытые сделки за день")
    lines.append("")

    if not closed:
        lines.append("*Нет закрытых сделок за день*")
        lines.append("")
    else:
        # Summary
        import math
        pnls = [float(o.pnl_pct) for o, s in closed if o.pnl_pct is not None and math.isfinite(float(o.pnl_pct))]
        wins = sum(p > 0 for p in pnls)
        losses_count = sum(p < 0 for p in pnls)
        zero = sum(p == 0 for p in pnls)
        unknown = len(closed) - len(pnls)
        expired = sum(o.status == "EXPIRED" for o, s in closed)
        tp_events = sum(o.status == "HIT_TP" for o, s in closed)
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        pf = _calc_profit_factor(pnls)
        pf_text = f"{pf:.2f}" if pf is not None else "—"
        wr_text = f"{wins / len(pnls) * 100:.1f}%" if pnls else "—"
        rstats = _r_stats(closed)

        lines.append("| Метрика | Значение |")
        lines.append("|---------|----------|")
        lines.append(f"| Всего закрыто | {len(closed)} |")
        lines.append(f"| Известный PnL / неизвестный | {len(pnls)} / {unknown} |")
        lines.append(f"| WIN (PnL > 0) | {wins} |")
        lines.append(f"| LOSS (PnL < 0) | {losses_count} |")
        lines.append(f"| Нулевой сохраненный PnL | {zero} |")
        lines.append(f"| События HIT_TP | {tp_events} |")
        lines.append(f"| EXPIRED (закрытие требует сверки) | {expired} |")
        lines.append(f"| Винрейт по известному PnL | {wr_text} |")
        lines.append(f"| Средний ценовой PnL | {avg_pnl:+.2f}% |")
        lines.append(f"| Ценовой Profit Factor | {pf_text} |")
        if rstats["count"]:
            lines.append(f"| Сделок с известным R | {rstats['count']} |")
            lines.append(f"| Средний R | {_fmt_r(rstats['avg'])} |")
            lines.append(f"| Суммарный R за день | {_fmt_r(rstats['total'])} |")
            pf_r_text = f"{rstats['pf']:.2f}" if rstats["pf"] is not None else "—"
            lines.append(f"| Profit Factor (в R) | {pf_r_text} |")
        lines.append("")

        # Details table
        lines.append("| # | Символ | TF | Направление | Entry | Exit | PnL | R | Статус |")
        lines.append("|---|--------|-----|-------------|-------|------|-----|---|--------|")
        for i, (outcome, signal) in enumerate(closed, 1):
            status_emoji = {"HIT_TP": "TP", "HIT_SL": "SL", "EXPIRED": "EXPIRED"}.get(outcome.status, "?")
            pnl_str = f"{outcome.pnl_pct:+.2f}%" if outcome.pnl_pct is not None and math.isfinite(float(outcome.pnl_pct)) else "—"
            r_str = _fmt_r(_r_multiple(outcome, signal))
            entry_str = f"{signal.close_price:.4f}" if signal.close_price else "—"
            exit_str = f"{outcome.close_price:.4f}" if outcome.close_price else "—"
            lines.append(
                f"| {i} | {signal.symbol} | {signal.timeframe} | {signal.signal_type} "
                f"| {entry_str} | {exit_str} | {pnl_str} | {r_str} | {status_emoji} |"
            )
        lines.append("")

    # === Open Trades ===
    lines.append("## Открытые сделки")
    lines.append("")

    if not open_trades:
        lines.append("*Нет открытых сделок*")
        lines.append("")
    else:
        lines.append("| Символ | TF | Направление | Entry | SL | TP | Открыт |")
        lines.append("|--------|-----|-------------|-------|------|------|--------|")
        for outcome, signal in open_trades:
            entry_str = f"{signal.close_price:.4f}" if signal.close_price else "—"
            sl_str = f"{signal.sl:.4f}" if signal.sl else "—"
            tp_str = f"{signal.tp:.4f}" if signal.tp else "—"
            opened_at = signal.sent_at or signal.created_at
            opened_str = opened_at.strftime("%m-%d %H:%M") if opened_at else "—"
            lines.append(
                f"| {signal.symbol} | {signal.timeframe} | {signal.signal_type} "
                f"| {entry_str} | {sl_str} | {tp_str} | {opened_str} |"
            )
        lines.append("")

    # === All-Time Stats ===
    lines.append("## Общая статистика (всё время)")
    lines.append("")

    if all_closed:
        import math
        all_pnls = [float(o.pnl_pct) for o, s in all_closed if o.pnl_pct is not None and math.isfinite(float(o.pnl_pct))]
        all_wins = sum(p > 0 for p in all_pnls)
        all_losses = sum(p < 0 for p in all_pnls)
        all_zero = sum(p == 0 for p in all_pnls)
        all_unknown = len(all_closed) - len(all_pnls)
        all_expired = sum(o.status == "EXPIRED" for o, s in all_closed)
        all_tp_events = sum(o.status == "HIT_TP" for o, s in all_closed)
        all_avg = sum(all_pnls) / len(all_pnls) if all_pnls else 0.0
        all_pf = _calc_profit_factor(all_pnls)
        all_pf_text = f"{all_pf:.2f}" if all_pf is not None else "—"
        all_wr_text = f"{all_wins / len(all_pnls) * 100:.1f}%" if all_pnls else "—"
        all_best = max(all_pnls) if all_pnls else 0.0
        all_worst = min(all_pnls) if all_pnls else 0.0
        all_rstats = _r_stats(all_closed)

        lines.append(f"| Метрика | Значение |")
        lines.append(f"|---------|----------|")
        lines.append(f"| Всего закрыто | {len(all_closed)} |")
        lines.append(f"| Известный PnL / неизвестный | {len(all_pnls)} / {all_unknown} |")
        lines.append(f"| WIN (PnL > 0) | {all_wins} |")
        lines.append(f"| LOSS (PnL < 0) | {all_losses} |")
        lines.append(f"| Нулевой сохраненный PnL | {all_zero} |")
        lines.append(f"| События HIT_TP | {all_tp_events} |")
        lines.append(f"| EXPIRED (закрытие требует сверки) | {all_expired} |")
        lines.append(f"| Винрейт по известному PnL | {all_wr_text} |")
        lines.append(f"| Средний ценовой PnL | {all_avg:+.2f}% |")
        lines.append(f"| Ценовой Profit Factor | {all_pf_text} |")
        if all_rstats["count"]:
            all_pf_r_text = f"{all_rstats['pf']:.2f}" if all_rstats["pf"] is not None else "—"
            lines.append(f"| Сделок с известным R | {all_rstats['count']} |")
            lines.append(f"| Средний R | {_fmt_r(all_rstats['avg'])} |")
            lines.append(f"| Суммарный R | {_fmt_r(all_rstats['total'])} |")
            lines.append(f"| Profit Factor (в R) | {all_pf_r_text} |")
        lines.append(f"| Лучшая | {all_best:+.2f}% |")
        lines.append(f"| Худшая | {all_worst:+.2f}% |")
        lines.append("")

        # Breakdown by TF
        tf_stats: dict[str, list] = {}
        tf_rows: dict[str, list] = {}
        for outcome, signal in all_closed:
            tf = signal.timeframe
            if tf not in tf_stats:
                tf_stats[tf] = []
            if outcome.pnl_pct is not None:
                tf_stats[tf].append(outcome.pnl_pct)
            tf_rows.setdefault(tf, []).append((outcome, signal))

        if tf_stats:
            lines.append("### По таймфреймам")
            lines.append("")
            lines.append("| TF | Сделок | Винрейт | Средний PnL | Средний R | PF (цена) | PF (R) |")
            lines.append("|-----|--------|---------|-------------|-----------|-----------|--------|")
            for tf in sorted(tf_stats.keys()):
                pnls_tf = tf_stats[tf]
                wins_tf = sum(1 for p in pnls_tf if p > 0)
                total_tf = len(pnls_tf)
                avg_tf = sum(pnls_tf) / total_tf if total_tf else 0.0
                pf_tf = _calc_profit_factor(pnls_tf)
                wr_tf = wins_tf / total_tf * 100 if total_tf else 0.0
                r_tf_stats = _r_stats(tf_rows.get(tf, []))
                pf_tf_text = f"{pf_tf:.2f}" if pf_tf is not None else "—"
                pf_r_tf_text = (
                    f"{r_tf_stats['pf']:.2f}" if r_tf_stats["pf"] is not None else "—"
                )
                lines.append(
                    f"| {tf} | {total_tf} | {wr_tf:.1f}% | {avg_tf:+.2f}% | "
                    f"{_fmt_r(r_tf_stats['avg'])} | {pf_tf_text} | {pf_r_tf_text} |"
                )
            lines.append("")

        # === WR по фильтрам (слайсы живых исходов) ===
        # Data slice for manual threshold tuning: each row is "if this filter
        # were the only one, how did that bucket actually perform".
        trace_by_signal = data.get("trace_by_signal") or {}

        def _trace_of(signal) -> dict:
            sid = getattr(signal, "id", None)
            return trace_by_signal.get(sid) or {}

        slices: dict[str, dict[str, list]] = {
            "Символ": {},
            "Направление": {},
            "Setup type": {},
            "Режим рынка": {},
            "ADX-бакет": {},
        }
        for outcome, signal in all_closed:
            tr = _trace_of(signal)
            setup_type = tr.get("setup_type") or "—"
            regime = tr.get("regime") or "—"
            adx = _adx_bucket(tr.get("adx"))
            slices["Символ"].setdefault(signal.symbol, []).append((outcome, signal))
            slices["Направление"].setdefault(signal.signal_type, []).append((outcome, signal))
            slices["Setup type"].setdefault(setup_type, []).append((outcome, signal))
            slices["Режим рынка"].setdefault(regime, []).append((outcome, signal))
            slices["ADX-бакет"].setdefault(adx, []).append((outcome, signal))

        lines.append("### WR по фильтрам")
        lines.append("")
        lines.append(
            "_Нарезка живых исходов по слайсам. N = размер выборки: "
            "при N < 10 разница между строками статистически шумит._"
        )
        lines.append("")
        for title, groups in slices.items():
            if not groups:
                continue
            lines.append(f"**{title}**")
            lines.append("")
            _append_slice_table(lines, groups)
    else:
        lines.append("*Пока нет закрытых сделок*")
        lines.append("")

    # === Заблокировано гейтами (live-воронка) ===
    gate_blocks = data.get("gate_blocks") or {}
    if gate_blocks:
        total_blocked = sum(gate_blocks.values())
        candidates_passed = data.get("candidates_passed") or 0
        lines.append("## Заблокировано гейтами (все кандидаты)")
        lines.append("")
        lines.append(
            f"_Прошло кандидатов: {candidates_passed}, "
            f"заблокировано: {total_blocked}. Финальный гейт из decision_traces.final_stage. "
            f"У заблокированных кандидатов нет исходов — цифра помогает искать, "
            f"где воронка теряет объём, а не оценивать качество._"
        )
        lines.append("")
        lines.append("| Гейт | Заблокировано | Доля |")
        lines.append("|------|---------------|------|")
        for stage, cnt in sorted(gate_blocks.items(), key=lambda kv: -kv[1]):
            share = cnt / total_blocked * 100 if total_blocked else 0.0
            lines.append(f"| {stage} | {cnt} | {share:.1f}% |")
        lines.append("")

    return "\n".join(lines)


async def generate_and_save(date: Optional[datetime] = None) -> str:
    """Generate daily report and save to file. Returns file path."""
    data = await get_daily_data(date)
    report = format_daily_report(data)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    filename = data["date"].strftime("%Y-%m-%d") + ".md"
    filepath = REPORT_DIR / filename
    filepath.write_text(report, encoding="utf-8")

    return str(filepath)


async def generate_summary() -> str:
    """Generate a short summary for Telegram (fits in one message)."""
    from analytics.performance import overall_stats, format_stats_telegram

    stats = await overall_stats()
    return format_stats_telegram(stats, "all time")


if __name__ == "__main__":
    path = asyncio.run(generate_and_save())
    print(f"Report saved: {path}")
