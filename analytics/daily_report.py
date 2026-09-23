"""
analytics/daily_report.py — Ежедневный отчёт по сделкам.

Генерирует Markdown-файл reports/daily/YYYY-MM-DD.md со статистикой:
- Закрытые сделки за день
- Открытые сделки и текущий PnL
- Винрейт, средний PnL, profit factor
- Лучшая/худшая сделка
- Breakdown по TF

Usage:
    python -m analytics.daily_report [--days 1] [--output reports/daily/]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import db, Signal, SignalOutcome
from sqlalchemy import select, desc


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

    return {
        "date": day_start,
        "closed_today": closed,
        "open_trades": open_trades,
        "all_closed": all_closed,
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
        lines.append("")

        # Details table
        lines.append("| # | Символ | TF | Направление | Entry | Exit | PnL | Статус |")
        lines.append("|---|--------|-----|-------------|-------|------|-----|--------|")
        for i, (outcome, signal) in enumerate(closed, 1):
            status_emoji = {"HIT_TP": "TP", "HIT_SL": "SL", "EXPIRED": "EXPIRED"}.get(outcome.status, "?")
            pnl_str = f"{outcome.pnl_pct:+.2f}%" if outcome.pnl_pct is not None and math.isfinite(float(outcome.pnl_pct)) else "—"
            entry_str = f"{signal.close_price:.4f}" if signal.close_price else "—"
            exit_str = f"{outcome.close_price:.4f}" if outcome.close_price else "—"
            lines.append(
                f"| {i} | {signal.symbol} | {signal.timeframe} | {signal.signal_type} "
                f"| {entry_str} | {exit_str} | {pnl_str} | {status_emoji} |"
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
        lines.append(f"| Лучшая | {all_best:+.2f}% |")
        lines.append(f"| Худшая | {all_worst:+.2f}% |")
        lines.append("")

        # Breakdown by TF
        tf_stats: dict[str, list] = {}
        for outcome, signal in all_closed:
            tf = signal.timeframe
            if tf not in tf_stats:
                tf_stats[tf] = []
            if outcome.pnl_pct is not None:
                tf_stats[tf].append(outcome.pnl_pct)

        if tf_stats:
            lines.append("### По таймфреймам")
            lines.append("")
            lines.append("| TF | Сделок | Винрейт | Средний PnL | PF |")
            lines.append("|-----|--------|---------|-------------|-----|")
            for tf in sorted(tf_stats.keys()):
                pnls_tf = tf_stats[tf]
                wins_tf = sum(1 for p in pnls_tf if p > 0)
                total_tf = len(pnls_tf)
                avg_tf = sum(pnls_tf) / total_tf if total_tf else 0.0
                pf_tf = _calc_profit_factor(pnls_tf)
                wr_tf = wins_tf / total_tf * 100 if total_tf else 0.0
                lines.append(f"| {tf} | {total_tf} | {wr_tf:.1f}% | {avg_tf:+.2f}% | {pf_tf:.2f} |")
            lines.append("")
    else:
        lines.append("*Пока нет закрытых сделок*")
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
