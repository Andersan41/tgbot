"""
scripts/analyze_backtest.py — Parse backtest results and compare old vs new pipeline.

Usage:
    python scripts/analyze_backtest.py [path_to_backtest_md]
"""
import re
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class BacktestResult:
    total_trades: int = 0
    winrate: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    total_pnl: float = 0.0
    max_dd: float = 0.0
    expectancy: float = 0.0
    avg_pnl: float = 0.0
    symbols: dict = None
    setups: dict = None

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = {}
        if self.setups is None:
            self.setups = {}


def parse_backtest_md(filepath: str) -> BacktestResult:
    """Parse new_pipeline_backtest.md format."""
    text = Path(filepath).read_text(encoding="utf-8")
    result = BacktestResult()

    # Aggregate metrics
    m = re.search(r"Total trades\s*\|\s*(\d+)", text)
    if m:
        result.total_trades = int(m.group(1))

    m = re.search(r"Winrate\s*\|\s*([\d.]+)%", text)
    if m:
        result.winrate = float(m.group(1))

    m = re.search(r"Profit Factor\s*\|\s*([\d.]+)", text)
    if m:
        result.profit_factor = float(m.group(1))

    m = re.search(r"Sharpe Ratio\s*\|\s*([\d.]+)", text)
    if m:
        result.sharpe = float(m.group(1))

    m = re.search(r"Total PnL \(net\)\s*\|\s*([+-]?[\d.]+)%", text)
    if m:
        result.total_pnl = float(m.group(1))

    m = re.search(r"Max DD.*?\|\s*([\d.]+)%", text)
    if m:
        result.max_dd = float(m.group(1))

    # Per-symbol breakdown
    for line in text.split("\n"):
        sym_match = re.match(
            r"\|\s*(\w+/\w+)\s*\|\s*(\d+)\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)\s*\|\s*([+-]?[\d.]+)%\s*\|",
            line,
        )
        if sym_match:
            result.symbols[sym_match.group(1)] = {
                "trades": int(sym_match.group(2)),
                "wr": float(sym_match.group(3)),
                "pf": float(sym_match.group(4)),
                "pnl": float(sym_match.group(5)),
            }

    # Setup type breakdown
    for line in text.split("\n"):
        setup_match = re.match(
            r"\|\s*(MSS Reversal|BOS Continuation \w+|BOS Continuation ALL)\s*\|\s*(\d+)\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)\s*\|\s*([+-]?[\d.]+)\s*\|\s*([+-]?[\d.]+)%\s*\|",
            line,
        )
        if setup_match:
            result.setups[setup_match.group(1)] = {
                "trades": int(setup_match.group(2)),
                "wr": float(setup_match.group(3)),
                "pf": float(setup_match.group(4)),
                "avg_r": float(setup_match.group(5)),
                "pnl": float(setup_match.group(6)),
            }

    return result


def compare(old: BacktestResult, new: BacktestResult) -> str:
    """Generate comparison report."""
    lines = []
    lines.append("# Backtest Comparison: Old vs New Pipeline\n")

    # Normalize to monthly
    old_months = 24  # 2 years
    new_months = 3   # 90 days

    lines.append("## Key Metrics (normalized to monthly)\n")
    lines.append("| Metric | Old (24mo) | New (3mo) | Monthly Old | Monthly New | Delta |")
    lines.append("|--------|-----------|----------|-------------|-------------|-------|")

    for metric, old_val, new_val, fmt in [
        ("Total trades", old.total_trades, new.total_trades, "d"),
        ("Winrate", old.winrate, new.winrate, ".1f"),
        ("Profit Factor", old.profit_factor, new.profit_factor, ".2f"),
        ("Total PnL", old.total_pnl, new.total_pnl, "+.2f"),
    ]:
        old_monthly = old_val / old_months if old_months else 0
        new_monthly = new_val / new_months if new_months else 0
        delta = new_monthly - old_monthly
        if metric == "Total trades":
            delta_str = f"{delta:+.1f}"
        elif metric == "Winrate":
            delta_str = f"{delta:+.1f}%"
        else:
            delta_str = f"{delta:+.2f}"
        lines.append(
            f"| {metric} | {old_val:{fmt}} | {new_val:{fmt}} | "
            f"{old_monthly:{fmt}}/mo | {new_monthly:{fmt}}/mo | {delta_str} |"
        )

    # Per-symbol comparison
    if old.symbols or new.symbols:
        lines.append("\n## Per-Symbol Comparison\n")
        lines.append("| Symbol | Old WR | New WR | Old PF | New PF | Old PnL | New PnL |")
        lines.append("|--------|--------|--------|--------|--------|---------|---------|")
        all_syms = set(list(old.symbols.keys()) + list(new.symbols.keys()))
        for sym in sorted(all_syms):
            o = old.symbols.get(sym, {})
            n = new.symbols.get(sym, {})
            lines.append(
                f"| {sym} | "
                f"{o.get('wr', '-')}% | {n.get('wr', '-')}% | "
                f"{o.get('pf', '-')} | {n.get('pf', '-')} | "
                f"{o.get('pnl', '-')}% | {n.get('pnl', '-')}% |"
            )

    # Setup comparison
    if old.setups or new.setups:
        lines.append("\n## Setup Type Comparison\n")
        lines.append("| Setup | Old Trades | New Trades | Old WR | New WR | Old PF | New PF |")
        lines.append("|-------|-----------|-----------|--------|--------|--------|--------|")
        all_setups = set(list(old.setups.keys()) + list(new.setups.keys()))
        for setup in sorted(all_setups):
            o = old.setups.get(setup, {})
            n = new.setups.get(setup, {})
            lines.append(
                f"| {setup} | "
                f"{o.get('trades', '-')} | {n.get('trades', '-')} | "
                f"{o.get('wr', '-')}% | {n.get('wr', '-')}% | "
                f"{o.get('pf', '-')} | {n.get('pf', '-')} |"
            )

    # Verdict
    lines.append("\n## Verdict\n")
    if new.winrate > 35 and new.profit_factor > 1.3:
        lines.append("**PASS** — WR > 35%, PF > 1.3. Ready for walk-forward validation.")
    elif new.winrate > 25 and new.profit_factor > 1.0:
        lines.append("**MARGINAL** — WR 25-35%, PF 1.0-1.3. Needs ATR gate tuning.")
    else:
        lines.append("**FAIL** — WR < 25% or PF < 1.0. Needs investigation.")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/analyze_backtest.py <new_backtest.md> [old_backtest.md]")
        sys.exit(1)

    new_path = sys.argv[1]
    old_path = sys.argv[2] if len(sys.argv) > 2 else "reports/new_pipeline_backtest.md"

    new_result = parse_backtest_md(new_path)
    old_result = parse_backtest_md(old_path)

    report = compare(old_result, new_result)
    print(report)

    # Save
    out_path = Path("reports") / "backtest_comparison.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
