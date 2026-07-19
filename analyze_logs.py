import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

LOG_DIR = Path(r"E:\Projects\tgbot\logs")

# Regex for structured logs: "2026-07-12 11:56:33 | ERROR    | ..."
RE_LEVEL = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\|\s*(ERROR|INFO|WARNING)\s+\|"
)

# Regex for plain logs: "2026-07-14 00:17:03 | [FUNNEL] ..."
RE_PLAIN = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\|")

# Patterns we care about
PATTERNS = {
    "quote_service_unavailable": re.compile(r"quote service unavailable", re.I),
    "failed_to_send_signal": re.compile(r"Failed to send signal", re.I),
    "unexpected_error_sending_signal": re.compile(r"Unexpected error sending signal", re.I),
    "web_payload_error": re.compile(r"Web payload error", re.I),
    "unhandled_app_error": re.compile(r"Unhandled app error", re.I),
}

# To detect sent signals
RE_SIGNAL_SENT = re.compile(r"Signal sent to channel: (.+)", re.I)


def collect_log_files():
    files = []
    for p in LOG_DIR.rglob("*"):
        if p.is_file() and p.suffix in (".log", ".txt") and not p.name.endswith(".zip"):
            files.append(p)
    return sorted(files)


def parse_timestamp(ts_str):
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def analyze():
    files = collect_log_files()
    print(f"Found {len(files)} log files:\n")
    for f in files:
        print(f"  {f.relative_to(LOG_DIR)}")
    print()

    error_counts = defaultdict(int)
    other_errors = defaultdict(int)
    date_hour_errors = defaultdict(lambda: defaultdict(int))
    total_lines = 0
    total_error_lines = 0
    total_info_lines = 0
    total_warning_lines = 0
    notifier_errors = []  # (timestamp, line_text, file)
    all_sent_signals = []  # (timestamp, signal_desc, file)

    for fpath in files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  Could not read {fpath.name}: {e}")
            continue

        for raw_line in content.splitlines():
            clean = strip_ansi(raw_line)
            total_lines += 1

            m_level = RE_LEVEL.search(clean)
            if m_level:
                ts_str, level = m_level.group(1), m_level.group(2)
                ts = parse_timestamp(ts_str)
                hour_key = ts.strftime("%Y-%m-%d %H:00") if ts else "unknown"

                if level == "ERROR":
                    total_error_lines += 1
                    date_hour_errors[ts_str[:10] if ts else "unknown"][hour_key] += 1

                    matched = False
                    for name, pat in PATTERNS.items():
                        if pat.search(clean):
                            error_counts[name] += 1
                            matched = True
                            if name in ("failed_to_send_signal", "unexpected_error_sending_signal"):
                                notifier_errors.append((ts, clean, fpath.name))
                            break
                    if not matched:
                        # bucket other errors
                        sig = clean[:120]
                        other_errors[sig] += 1

                elif level == "INFO":
                    total_info_lines += 1
                    m_sig = RE_SIGNAL_SENT.search(clean)
                    if m_sig:
                        all_sent_signals.append((ts, m_sig.group(1).strip(), fpath.name))

                elif level == "WARNING":
                    total_warning_lines += 1
            else:
                # Plain log lines (no level tag) — still count as info
                m_plain = RE_PLAIN.search(clean)
                if m_plain:
                    total_info_lines += 1

    # ── REPORT ──────────────────────────────────────────────────
    print("=" * 70)
    print("  LOG ERROR ANALYSIS REPORT")
    print("=" * 70)

    print("\n-- LINE COUNTS --")
    print(f"  Total lines:       {total_lines:>10}")
    print(f"  INFO lines:        {total_info_lines:>10}")
    print(f"  WARNING lines:     {total_warning_lines:>10}")
    print(f"  ERROR lines:       {total_error_lines:>10}")
    if total_lines:
        rate = total_error_lines / total_lines * 100
        print(f"  Error rate:        {rate:>9.2f}%")

    print("\n-- ERROR COUNTS BY TYPE --")
    for name, count in sorted(error_counts.items(), key=lambda x: -x[1]):
        label = name.replace("_", " ").title()
        print(f"  {label:<40} {count:>6}")
    if other_errors:
        print(f"\n-- OTHER ERRORS ({len(other_errors)} unique) --")
        for sig, count in sorted(other_errors.items(), key=lambda x: -x[1]):
            print(f"  [{count:>3}x] {sig}")

    print("\n-- ERROR DISTRIBUTION BY DATE --")
    for date_key in sorted(date_hour_errors):
        day_total = sum(date_hour_errors[date_key].values())
        print(f"\n  {date_key}  ({day_total} errors)")
        for hour_key in sorted(date_hour_errors[date_key]):
            hour_short = hour_key.split(" ")[1]
            print(f"    {hour_short}  {'#' * min(date_hour_errors[date_key][hour_key], 80):<40} {date_hour_errors[date_key][hour_key]}")

    # ── LOST SIGNAL ANALYSIS ──
    print("\n" + "=" * 70)
    print("  NOTIFIER ERRORS & POTENTIALLY LOST SIGNALS")
    print("=" * 70)

    if not notifier_errors:
        print("\n  No notifier errors found.")
    else:
        print(f"\n  Total notifier errors: {len(notifier_errors)}")
        print(f"  Total signals sent successfully: {len(all_sent_signals)}\n")

        for ne_ts, ne_line, ne_file in notifier_errors:
            print(f"  NOTIFIER ERROR at {ne_ts} ({ne_file})")
            print(f"    {ne_line.strip()[:100]}")

            # Find signals sent within a 3-minute window around the error
            nearby = []
            for sig_ts, sig_desc, sig_file in all_sent_signals:
                if sig_ts and ne_ts and abs((sig_ts - ne_ts).total_seconds()) <= 180:
                    nearby.append((sig_ts, sig_desc))

            # Also look for signal attempts that may have been the one that failed
            # The "Unexpected error" / "Failed to send" pattern means a signal WAS attempted
            # but the channel send failed. Look for the signal that likely triggered this error.

            if nearby:
                print(f"    Signals sent within +/-3 min:")
                for sig_ts, sig_desc in sorted(nearby, key=lambda x: x[0]):
                    delta = (sig_ts - ne_ts).total_seconds()
                    print(f"      {sig_ts} ({delta:+.0f}s) -> {sig_desc}")
            else:
                print(f"    No successful signal sends within ±3 min window.")
                print(f"    [!] This error likely means a signal was ATTEMPTED but FAILED to deliver.")
            print()

    # ── SUMMARY OF POTENTIALLY LOST SIGNALS ──
    # If a notifier error occurred and no "Signal sent" line appears within 3 seconds,
    # that signal was almost certainly lost.
    print("=" * 70)
    print("  POTENTIALLY LOST SIGNALS (notifier error with no nearby success)")
    print("=" * 70)

    lost_count = 0
    for ne_ts, ne_line, ne_file in notifier_errors:
        # Check if a successful send happened within 3 seconds of the error
        has_success = any(
            sig_ts and ne_ts and abs((sig_ts - ne_ts).total_seconds()) <= 3
            for sig_ts, _, _ in all_sent_signals
        )
        if not has_success:
            lost_count += 1
            print(f"  X {ne_ts} -- {ne_line.strip()[:90]}")

    if lost_count == 0:
        print("  All notifier errors had a successful delivery nearby — no confirmed losses.")
    else:
        print(f"\n  Total potentially lost signals: {lost_count}")


if __name__ == "__main__":
    analyze()
