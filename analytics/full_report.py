"""
analytics/full_report.py — Полный отчёт по торговле бота.

Генерирует Markdown отчётreports/full_report.md с графиками в reports/charts/.

Использование:
    python -m analytics.full_report
    python -m analytics.full_report --days 30
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import db, Signal, SignalOutcome, DecisionTrace, SignalCandidate
from analytics.performance import overall_stats, segmented_stats, mfe_mae_analysis
from sqlalchemy import select, func, and_, or_, text

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
CHART_DIR = REPORT_DIR / "charts"


# ═══════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════

async def load_trades(period: Optional[str] = None) -> pd.DataFrame:
    """Load all closed trades with signal data."""
    async with db._session_factory() as session:
        query = (
            select(
                Signal.id,
                Signal.symbol,
                Signal.timeframe,
                Signal.signal_type,
                Signal.close_price.label("entry_price"),
                Signal.sl,
                Signal.tp,
                Signal.score,
                Signal.confidence_v2_pct,
                Signal.created_at,
                Signal.sent_at,
                Signal.telegram_sent_at,
                Signal.entry_atr,
                Signal.mfe_pct,
                Signal.mae_pct,
                SignalOutcome.status,
                SignalOutcome.close_price,
                SignalOutcome.pnl_pct,
                SignalOutcome.risk_pct,
                SignalOutcome.closed_at,
            )
            .join(SignalOutcome, Signal.id == SignalOutcome.signal_id)
            .where(SignalOutcome.status != "OPEN")
        )

        if period:
            days = int(period.replace("d", ""))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            query = query.where(Signal.created_at >= cutoff)

        query = query.order_by(Signal.created_at)
        result = await session.execute(query)
        rows = result.mappings().all()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["closed_at"] = pd.to_datetime(df["closed_at"])
    df["sent_at"] = pd.to_datetime(df["sent_at"])

    # Compute hold duration
    df["hold_hours"] = (df["closed_at"] - df["created_at"]).dt.total_seconds() / 3600

    # Compute R-multiple
    df["r_multiple"] = df.apply(
        lambda r: (r["pnl_pct"] / (abs(r["entry_price"] - r["sl"]) / r["entry_price"] * 100))
        if r["sl"] and r["entry_price"] and abs(r["entry_price"] - r["sl"]) > 0
        else 0.0,
        axis=1,
    )

    # Day of week
    df["dow"] = df["created_at"].dt.day_name()
    df["hour"] = df["created_at"].dt.hour
    df["date"] = df["created_at"].dt.date
    df["month"] = df["created_at"].dt.to_period("M")

    return df


async def load_signals_summary() -> dict:
    """Load signal generation stats."""
    async with db._session_factory() as session:
        total_q = await session.execute(select(func.count(Signal.id)))
        total_signals = total_q.scalar() or 0

        outcomes_q = await session.execute(
            select(SignalOutcome.status, func.count(SignalOutcome.id)).group_by(SignalOutcome.status)
        )
        outcomes = dict(outcomes_q.all())

        return {
            "total_signals": total_signals,
            "closed": outcomes.get("HIT_TP", 0) + outcomes.get("HIT_SL", 0) + outcomes.get("EXPIRED", 0),
            "open": outcomes.get("OPEN", 0),
            "hit_tp": outcomes.get("HIT_TP", 0),
            "hit_sl": outcomes.get("HIT_SL", 0),
            "expired": outcomes.get("EXPIRED", 0),
        }


async def load_gate_funnel() -> list[dict]:
    """Load gate funnel from decision traces."""
    async with db._session_factory() as session:
        query = (
            select(
                DecisionTrace.symbol,
                DecisionTrace.timeframe,
                DecisionTrace.timestamp,
                DecisionTrace.gate_cooldown,
                DecisionTrace.gate_portfolio_risk,
                DecisionTrace.gate_btc_global_trend,
                DecisionTrace.gate_indicators,
                DecisionTrace.gate_signal_engine,
                DecisionTrace.gate_distance_filter,
                DecisionTrace.gate_tp_path,
                DecisionTrace.gate_mtf_alignment,
                DecisionTrace.gate_volatility,
                DecisionTrace.gate_sl_distance,
                DecisionTrace.gate_rr_guard,
                DecisionTrace.gate_dynamic_risk,
                DecisionTrace.gate_confidence_v2,
                DecisionTrace.gate_dedup,
                DecisionTrace.gate_regime_block,
                DecisionTrace.gate_structure_alignment,
                DecisionTrace.gate_sweep_required,
                DecisionTrace.final_stage,
                DecisionTrace.signal_generated,
                DecisionTrace.blocked_reason,
            )
        )
        result = await session.execute(query)
        return [dict(r._mapping) for r in result.all()]


async def load_log_errors() -> dict:
    """Parse bot.log for errors and performance metrics."""
    log_path = Path(__file__).resolve().parent.parent / "logs" / "bot.log"
    if not log_path.exists():
        return {"errors": [], "api_errors": 0, "total_scans": 0}

    errors = []
    api_errors = 0
    total_scans = 0
    funnel_summaries = []

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "ERROR" in line:
                api_errors += 1
                if len(errors) < 50:
                    errors.append(line.strip()[:200])
            if "Scan complete" in line:
                total_scans += 1
            if "FUNNEL SUMMARY" in line:
                funnel_summaries.append(line.strip())

    return {
        "errors": errors,
        "api_errors": api_errors,
        "total_scans": total_scans,
        "funnel_summaries": funnel_summaries[-10:],
    }


# ═══════════════════════════════════════════════════════════════════════
# CHART GENERATION
# ═══════════════════════════════════════════════════════════════════════

def plot_equity_curve(df: pd.DataFrame, output_path: Path):
    """Plot cumulative PnL equity curve."""
    if df.empty:
        return

    df_sorted = df.sort_values("closed_at")
    cumulative_pnl = df_sorted["pnl_pct"].cumsum()

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(df_sorted["closed_at"], cumulative_pnl, linewidth=1.5, color="#2196F3")
    ax.fill_between(df_sorted["closed_at"], 0, cumulative_pnl,
                     where=cumulative_pnl >= 0, alpha=0.15, color="green")
    ax.fill_between(df_sorted["closed_at"], 0, cumulative_pnl,
                     where=cumulative_pnl < 0, alpha=0.15, color="red")
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title("Price Return Sum (Cumulative PnL %)", fontsize=14, fontweight="bold")
    ax.set_ylabel("Cumulative PnL %")
    ax.set_xlabel("")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_drawdown(df: pd.DataFrame, output_path: Path):
    """Plot drawdown chart."""
    if df.empty:
        return

    df_sorted = df.sort_values("closed_at")
    cumulative = df_sorted["pnl_pct"].cumsum()
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(df_sorted["closed_at"], 0, drawdown, color="red", alpha=0.4)
    ax.plot(df_sorted["closed_at"], drawdown, color="darkred", linewidth=1)
    ax.set_title("Drawdown (%)", fontsize=14, fontweight="bold")
    ax.set_ylabel("Drawdown %")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_pnl_distribution(df: pd.DataFrame, output_path: Path):
    """Plot histogram of PnL distribution."""
    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    pnls = df["pnl_pct"].dropna()
    colors = ["green" if x > 0 else "red" for x in pnls]
    ax.hist(pnls, bins=30, color="#2196F3", edgecolor="white", alpha=0.8)
    ax.axvline(x=0, color="gray", linestyle="--", linewidth=1)
    ax.axvline(x=pnls.mean(), color="orange", linestyle="-", linewidth=1.5, label=f"Mean: {pnls.mean():.2f}%")
    ax.set_title("PnL Distribution", fontsize=14, fontweight="bold")
    ax.set_xlabel("PnL %")
    ax.set_ylabel("Count")
    ax.legend()
    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_monthly_pnl(df: pd.DataFrame, output_path: Path):
    """Plot monthly PnL bars."""
    if df.empty:
        return

    monthly = df.groupby("month")["pnl_pct"].sum()
    colors = ["green" if x > 0 else "red" for x in monthly.values]

    fig, ax = plt.subplots(figsize=(12, 5))
    bars = ax.bar(range(len(monthly)), monthly.values, color=colors, edgecolor="white")
    ax.set_xticks(range(len(monthly)))
    ax.set_xticklabels([str(m) for m in monthly.index], rotation=45, ha="right")
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title("Monthly PnL (%)", fontsize=14, fontweight="bold")
    ax.set_ylabel("PnL %")

    for bar, val in zip(bars, monthly.values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=9)

    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_winrate_by_hour(df: pd.DataFrame, output_path: Path):
    """Plot winrate by hour of day."""
    if df.empty:
        return

    hourly = df.groupby("hour").agg(
        total=("pnl_pct", "count"),
        wins=("pnl_pct", lambda x: (x > 0).sum()),
    ).reset_index()
    hourly["wr"] = hourly["wins"] / hourly["total"] * 100

    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.bar(hourly["hour"], hourly["total"], color="#2196F3", alpha=0.4, label="Trades")
    ax1.set_ylabel("Trade Count", color="#2196F3")
    ax1.set_xlabel("Hour (UTC)")

    ax2 = ax1.twinx()
    ax2.plot(hourly["hour"], hourly["wr"], color="orange", marker="o", linewidth=2, label="Winrate %")
    ax2.set_ylabel("Winrate %", color="orange")
    ax2.set_ylim(0, 100)

    ax1.set_title("Trades & Winrate by Hour (UTC)", fontsize=14, fontweight="bold")
    ax1.set_xticks(range(24))
    fig.legend(loc="upper right", bbox_to_anchor=(0.95, 0.95))
    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_pnl_by_symbol(df: pd.DataFrame, output_path: Path):
    """Plot PnL breakdown by symbol."""
    if df.empty:
        return

    by_sym = df.groupby("symbol")["pnl_pct"].sum().sort_values()
    colors = ["green" if x > 0 else "red" for x in by_sym.values]

    fig, ax = plt.subplots(figsize=(10, max(4, len(by_sym) * 0.4)))
    bars = ax.barh(range(len(by_sym)), by_sym.values, color=colors, edgecolor="white")
    ax.set_yticks(range(len(by_sym)))
    ax.set_yticklabels(by_sym.index)
    ax.axvline(x=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title("PnL by Symbol (%)", fontsize=14, fontweight="bold")
    ax.set_xlabel("PnL %")

    for bar, val in zip(bars, by_sym.values):
        ax.text(bar.get_width() + 0.1 if val >= 0 else bar.get_width() - 0.1,
                bar.get_y() + bar.get_height() / 2,
                f"{val:.1f}%", ha="left" if val >= 0 else "right",
                va="center", fontsize=9)

    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════════

def compute_consecutive(df: pd.DataFrame) -> tuple[int, int]:
    """Compute max consecutive wins and losses."""
    if df.empty:
        return 0, 0

    results = (df.sort_values("closed_at")["pnl_pct"] > 0).astype(int).values
    max_wins, max_losses = 0, 0
    current_wins, current_losses = 0, 0

    for r in results:
        if r == 1:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    return max_wins, max_losses


def compute_r_distribution(df: pd.DataFrame) -> dict[int, int]:
    """Count trades by R-multiple buckets."""
    if df.empty:
        return {}
    bins = range(-3, 6)
    result = {}
    for b in bins:
        count = len(df[(df["r_multiple"] >= b) & (df["r_multiple"] < b + 1)])
        if count > 0:
            result[f"+{b}R" if b >= 0 else f"{b}R"] = count
    return result


async def generate_report(period: Optional[str] = None) -> str:
    """Generate full trading report."""
    logger.info("Loading trade data...")
    df = await load_trades(period)
    signals = await load_signals_summary()
    gate_data = await load_gate_funnel()
    log_data = await load_log_errors()

    if df.empty:
        return "# No closed trades found for this period.\n"

    logger.info(f"Loaded {len(df)} closed trades")

    # ── Core metrics ──
    total = len(df)
    wins = len(df[df["pnl_pct"] > 0])
    losses = len(df[df["pnl_pct"] <= 0])
    wr = wins / total * 100 if total else 0
    gross_profit = df[df["pnl_pct"] > 0]["pnl_pct"].sum()
    gross_loss = abs(df[df["pnl_pct"] <= 0]["pnl_pct"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win = df[df["pnl_pct"] > 0]["pnl_pct"].mean() if wins else 0
    avg_loss = df[df["pnl_pct"] <= 0]["pnl_pct"].mean() if losses else 0
    avg_r_win = df[df["pnl_pct"] > 0]["r_multiple"].mean() if wins else 0
    avg_r_loss = df[df["pnl_pct"] <= 0]["r_multiple"].mean() if losses else 0
    expectancy = (wr / 100 * avg_r_win) - ((1 - wr / 100) * abs(avg_r_loss))
    net_pnl = df["pnl_pct"].sum()

    # Max drawdown
    cumulative = df.sort_values("closed_at")["pnl_pct"].cumsum()
    running_max = cumulative.cummax()
    drawdowns = cumulative - running_max
    max_dd = drawdowns.min()

    # Max drawdown duration
    dd_start = None
    max_dd_duration = timedelta()
    for i, (idx, dd) in enumerate(drawdowns.items()):
        if dd < 0 and dd_start is None:
            dd_start = df.iloc[i]["closed_at"]
        elif dd >= 0 and dd_start is not None:
            duration = df.iloc[i]["closed_at"] - dd_start
            max_dd_duration = max(max_dd_duration, duration)
            dd_start = None

    # Consecutive
    max_consec_wins, max_consec_losses = compute_consecutive(df)

    # Best/worst
    best_trade = df["pnl_pct"].max()
    worst_trade = df["pnl_pct"].min()

    # R distribution
    r_dist = compute_r_distribution(df)

    # Recovery factor
    recovery = net_pnl / abs(max_dd) if max_dd != 0 else float("inf")

    # Sharpe (annualized, assuming ~365 trades/year)
    if len(df) > 1:
        daily_returns = df.set_index("closed_at").resample("D")["pnl_pct"].sum()
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(365)) if daily_returns.std() > 0 else 0
    else:
        sharpe = 0

    # ── Build report ──
    lines = []
    lines.append("# 📊 Полный отчёт по торговле бота")
    lines.append(f"\n**Дата отчёта:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Период:** {df['created_at'].min().strftime('%Y-%m-%d')} → {df['created_at'].max().strftime('%Y-%m-%d')}")
    lines.append(f"**Всего сделок:** {total}")

    # ── Section 1: Trade History ──
    lines.append("\n---\n## 1. История сделок\n")
    lines.append("| # | Дата входа | Символ | TF | Направление | Entry | SL | TP | Exit | Результат | R | Статус |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        entry_dt = row["created_at"].strftime("%m-%d %H:%M") if pd.notna(row["created_at"]) else "?"
        status_emoji = {"HIT_TP": "✅ TP", "HIT_SL": "❌ SL", "EXPIRED": "⏰ EX"}.get(row["status"], row["status"])
        pnl_sign = "+" if row["pnl_pct"] > 0 else ""
        lines.append(
            f"| {i} | {entry_dt} | {row['symbol']} | {row['timeframe']} | {row['signal_type']} "
            f"| {row['entry_price']:.5f} | {row['sl']:.5f} | {row['tp']:.5f} "
            f"| {row['close_price']:.5f} | {pnl_sign}{row['pnl_pct']:.2f}% "
            f"| {row['r_multiple']:+.1f}R | {status_emoji} |"
        )

    # ── Section 2: Core Metrics ──
    lines.append("\n---\n## 2. Метрики по сделкам\n")
    lines.append("| Метрика | Значение |")
    lines.append("|---|---|")
    lines.append(f"| Всего сделок | {total} |")
    lines.append(f"| Wins / Losses | {wins} / {losses} |")
    lines.append(f"| Winrate | {wr:.1f}% |")
    lines.append(f"| Profit Factor | {pf:.2f} |")
    lines.append(f"| Expectancy (R) | {expectancy:+.2f}R |")
    lines.append(f"| Net PnL | {net_pnl:+.2f}% |")
    lines.append(f"| Avg Win | +{avg_win:.2f}% ({avg_r_win:+.1f}R) |")
    lines.append(f"| Avg Loss | {avg_loss:.2f}% ({avg_r_loss:+.1f}R) |")
    lines.append(f"| Best Trade | +{best_trade:.2f}% |")
    lines.append(f"| Worst Trade | {worst_trade:.2f}% |")
    lines.append(f"| Max Drawdown | {max_dd:.2f}% |")
    lines.append(f"| Max DD Duration | {max_dd_duration.total_seconds()/3600:.1f}h |")
    lines.append(f"| Max Consecutive Wins | {max_consec_wins} |")
    lines.append(f"| Max Consecutive Losses | {max_consec_losses} |")
    lines.append(f"| Recovery Factor | {recovery:.2f} |")
    lines.append(f"| Sharpe Ratio | {sharpe:.2f} |")
    lines.append(f"| Avg Hold Time | {df['hold_hours'].mean():.1f}h |")

    # R distribution
    if r_dist:
        lines.append("\n### Распределение по R-множителю\n")
        lines.append("| R-диапазон | Кол-во | % |")
        lines.append("|---|---|---|")
        for bucket, count in sorted(r_dist.items()):
            lines.append(f"| {bucket} | {count} | {count/total*100:.1f}% |")

    # ── Section 3: Time Analysis ──
    lines.append("\n---\n## 3. Анализ по времени\n")

    # By day of week
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_stats = df.groupby("dow").agg(
        trades=("pnl_pct", "count"),
        pnl=("pnl_pct", "sum"),
        wr=("pnl_pct", lambda x: (x > 0).sum() / len(x) * 100),
    ).reindex([d for d in dow_order if d in df["dow"].unique()])

    if not dow_stats.empty:
        lines.append("### По дням недели\n")
        lines.append("| День | Сделки | PnL | Winrate |")
        lines.append("|---|---|---|---|")
        for dow, row in dow_stats.iterrows():
            lines.append(f"| {dow} | {row['trades']:.0f} | {row['pnl']:+.1f}% | {row['wr']:.0f}% |")

    # By hour
    hour_stats = df.groupby("hour").agg(
        trades=("pnl_pct", "count"),
        pnl=("pnl_pct", "sum"),
        wr=("pnl_pct", lambda x: (x > 0).sum() / len(x) * 100),
    )

    if not hour_stats.empty:
        lines.append("\n### По часам (UTC)\n")
        lines.append("| Час | Сделки | PnL | Winrate |")
        lines.append("|---|---|---|---|")
        for h, row in hour_stats.iterrows():
            lines.append(f"| {h:02d}:00 | {row['trades']:.0f} | {row['pnl']:+.1f}% | {row['wr']:.0f}% |")

    # ── Section 4: By Instrument ──
    lines.append("\n---\n## 4. Анализ по инструментам\n")

    by_sym = df.groupby("symbol").agg(
        trades=("pnl_pct", "count"),
        pnl=("pnl_pct", "sum"),
        avg_pnl=("pnl_pct", "mean"),
        wr=("pnl_pct", lambda x: (x > 0).sum() / len(x) * 100),
        best=("pnl_pct", "max"),
        worst=("pnl_pct", "min"),
    ).sort_values("pnl", ascending=False)

    if not by_sym.empty:
        lines.append("| Символ | Сделки | PnL | Avg PnL | WR | Лучшая | Худшая |")
        lines.append("|---|---|---|---|---|---|---|")
        for sym, row in by_sym.iterrows():
            lines.append(
                f"| {sym} | {row['trades']:.0f} | {row['pnl']:+.1f}% | {row['avg_pnl']:+.2f}% "
                f"| {row['wr']:.0f}% | +{row['best']:.2f}% | {row['worst']:.2f}% |"
            )

    best_sym = by_sym.index[0] if not by_sym.empty else "N/A"
    worst_sym = by_sym.index[-1] if not by_sym.empty else "N/A"
    lines.append(f"\n**Лучший инструмент:** {best_sym} | **Худший:** {worst_sym}")

    # ── Section 5: By Direction ──
    lines.append("\n---\n## 5. Анализ по направлению\n")

    by_dir = df.groupby("signal_type").agg(
        trades=("pnl_pct", "count"),
        pnl=("pnl_pct", "sum"),
        avg_pnl=("pnl_pct", "mean"),
        wr=("pnl_pct", lambda x: (x > 0).sum() / len(x) * 100),
    )

    if not by_dir.empty:
        lines.append("| Направление | Сделки | PnL | Avg PnL | WR |")
        lines.append("|---|---|---|---|---|")
        for d, row in by_dir.iterrows():
            label = "Long (BUY)" if d == "BUY" else "Short (SELL)"
            lines.append(f"| {label} | {row['trades']:.0f} | {row['pnl']:+.1f}% | {row['avg_pnl']:+.2f}% | {row['wr']:.0f}% |")

    # ── Section 6: Risk Analysis ──
    lines.append("\n---\n## 6. Анализ риск-менеджмента\n")

    if "risk_pct" in df.columns:
        avg_risk = df["risk_pct"].mean()
        lines.append(f"- **Средний размер позиции:** {avg_risk:.2f}% от депозита")
        lines.append(f"- **Мин. размер позиции:** {df['risk_pct'].min():.2f}%")
        lines.append(f"- **Макс. размер позиции:** {df['risk_pct'].max():.2f}%")

    lines.append(f"- **Средний R:R выигрышных:** {avg_r_win:+.1f}R")
    lines.append(f"- **Средний R:R проигрышных:** {avg_r_loss:+.1f}R")
    lines.append(f"- **Expectancy:** {expectancy:+.2f}R на сделку")

    # R distribution histogram text
    if r_dist:
        lines.append("\n### Распределение результатов (R-множитель)\n")
        for bucket in sorted(r_dist.keys()):
            bar = "█" * min(40, r_dist[bucket])
            lines.append(f"  {bucket:>4s}: {bar} ({r_dist[bucket]})")

    # ── Section 7: Log Analysis ──
    lines.append("\n---\n## 7. Анализ логов\n")
    lines.append(f"- **Всего сканов:** {log_data['total_scans']}")
    lines.append(f"- **API ошибок:** {log_data['api_errors']}")

    if log_data["errors"]:
        lines.append("\n### Последние ошибки\n")
        lines.append("```")
        for err in log_data["errors"][:10]:
            lines.append(err)
        lines.append("```")

    if log_data["funnel_summaries"]:
        lines.append("\n### Последние сводки воронки\n")
        lines.append("```")
        for s in log_data["funnel_summaries"]:
            lines.append(s)
        lines.append("```")

    # ── Section 8: Strategy Analysis ──
    lines.append("\n---\n## 8. Анализ стратегии\n")
    lines.append(f"- **Всего сигналов сгенерировано:** {signals['total_signals']}")
    lines.append(f"- **Исполнено (закрыто):** {signals['closed']}")
    lines.append(f"- **Открыто сейчас:** {signals['open']}")
    lines.append(f"- **TP:** {signals['hit_tp']} ({signals['hit_tp']/signals['closed']*100:.0f}%)" if signals['closed'] else "- **TP:** 0")
    lines.append(f"- **SL:** {signals['hit_sl']} ({signals['hit_sl']/signals['closed']*100:.0f}%)" if signals['closed'] else "- **SL:** 0")
    lines.append(f"- **EX (Expired):** {signals['expired']}")

    # ── Section 9: Charts ──
    lines.append("\n---\n## 9. Визуализация\n")

    CHART_DIR.mkdir(parents=True, exist_ok=True)

    charts = [
        ("equity_curve.png", "Equity Curve", plot_equity_curve),
        ("drawdown.png", "Drawdown", plot_drawdown),
        ("pnl_distribution.png", "PnL Distribution", plot_pnl_distribution),
        ("monthly_pnl.png", "Monthly PnL", plot_monthly_pnl),
        ("pnl_by_symbol.png", "PnL by Symbol", plot_pnl_by_symbol),
        ("wr_by_hour.png", "Winrate by Hour", plot_winrate_by_hour),
    ]

    for filename, title, plot_func in charts:
        try:
            path = CHART_DIR / filename
            plot_func(df, path)
            if path.exists():
                rel = os.path.relpath(path, REPORT_DIR)
                lines.append(f"### {title}\n![{title}]({rel})\n")
        except Exception as e:
            lines.append(f"### {title}\n⚠️ Error generating chart: {e}\n")

    # ── Section 10: Conclusions ──
    lines.append("\n---\n## 10. Выводы и рекомендации\n")

    # Auto-generate insights
    insights = []
    if wr < 40:
        insights.append("⚠️ **Низкий винрейт** ({:.0f}%) — система фильтрует много ложных сигналов или SL слишком близко.".format(wr))
    if pf < 1.0:
        insights.append("❌ **PF < 1.0** — система убыточна. Требуется доработка стратегии.")
    elif pf < 1.3:
        insights.append("⚠️ **PF {} — еле прибыльна**. Небольшое ухудшение WR или R:R сделает систему убыточной.".format(pf))
    elif pf > 2.0:
        insights.append("✅ **PF {} — отличный результат**. Система стабильно прибыльна.".format(pf))

    if max_dd < -10:
        insights.append(f"⚠️ **Большая просадка {max_dd:.1f}%** — стоит рассмотреть снижение размера позиции.")

    if avg_r_loss < -2:
        insights.append("⚠️ **Средний убыток > 2R** — проигрышные сделки слишком большие. Проверь размер SL.")

    if expectancy < 0:
        insights.append("❌ **Отрицательный expectancy** — на каждой сделке бот в среднем теряет. Стратегия нуждается в корректировке.")
    elif expectancy < 0.1:
        insights.append("⚠️ **Expectancy很低** ({:+.2f}R) — минимальный запас прочности.".format(expectancy))

    # Check best/worst hours
    if not hour_stats.empty:
        best_h = hour_stats["pnl"].idxmax()
        worst_h = hour_stats["pnl"].idxmin()
        if hour_stats.loc[best_h, "pnl"] > 0 and hour_stats.loc[worst_h, "pnl"] < 0:
            insights.append(f"⏰ **Лучшее время:** {best_h:02d}:00 UTC | **Худшее:** {worst_h:02d}:00 UTC")

    # Check direction imbalance
    if not by_dir.empty and len(by_dir) == 2:
        dirs = by_dir.index.tolist()
        if by_dir.loc[dirs[0], "pnl"] > 0 and by_dir.loc[dirs[1], "pnl"] < 0:
            insights.append(f" directional imbalance: {dirs[0]} прибыльно, {dirs[1]} убыточно.")

    if not insights:
        insights.append("Данных недостаточно для автоматических выводов. Соберите больше сделок.")

    for insight in insights:
        lines.append(f"- {insight}")

    lines.append("\n### Что отслеживать\n")
    lines.append("- Rolling PF (последние 20 сделок) — стабильность системы")
    lines.append("- Winrate по дням недели — выявление паттернов")
    lines.append("- MFE/MAE — качество входов и стопов")
    lines.append("- Калибровка confidence — предсказываемость модели")

    lines.append("\n---\n*Отчёт сгенерирован автоматически*")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Full trading report")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--output", type=str, default=None, help="Output file path")
    args = parser.parse_args()

    period = f"{args.days}d" if args.days else None
    report = await generate_report(period)

    output = Path(args.output) if args.output else REPORT_DIR / "full_report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    logger.info(f"Report saved: {output}")
    logger.info(f"Charts saved: {CHART_DIR}/")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
