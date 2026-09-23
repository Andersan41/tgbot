"""Reproduce aggregate audit evidence without importing bot code or writing its DB.

Run from any directory: python audit_data.py --repo PATH_TO_TGBOT --output OUTPUT_DIR
SQLite is opened with mode=ro; source logs are read from the audited Git commit,
so test-generated log lines cannot contaminate the historical sample.
"""
from __future__ import annotations

import collections
import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import sqlite3
import statistics
import subprocess

ROOT = pathlib.Path.cwd()
OUT = pathlib.Path(__file__).resolve().parent
REF = "9d0da04"


def git_bytes(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{REF}:{path}"], cwd=ROOT)


def date(value):
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def distribution(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "median": statistics.median(values),
            "mean": statistics.mean(values), "max": max(values)}


def stats(rows):
    closed = [r for r in rows if r["status"] != "OPEN"]
    ps = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
    gp = sum(p for p in ps if p > 0)
    gl = -sum(p for p in ps if p < 0)
    wins, losses, zero = (sum(p > 0 for p in ps), sum(p < 0 for p in ps), sum(p == 0 for p in ps))
    weighted = [r["pnl_risk_weighted_pp"] for r in closed if r["pnl_risk_weighted_pp"] is not None]
    wgp, wgl = sum(p for p in weighted if p > 0), -sum(p for p in weighted if p < 0)
    return {"signals": len(rows), "closed": len(closed), "open": len(rows)-len(closed),
            "positive": wins, "negative": losses, "zero": zero,
            "win_rate_all_closed_pct": 100*wins/len(closed) if closed else None,
            "win_rate_nonzero_pct": 100*wins/(wins+losses) if wins+losses else None,
            "tp_event_rate_closed_pct": 100*sum(r["status"]=="HIT_TP" for r in closed)/len(closed) if closed else None,
            "raw_pnl_sum_percentage_points": sum(ps), "raw_pnl_mean_pct": statistics.mean(ps) if ps else None,
            "price_pnl_profit_factor": gp/gl if gl else None,
            "gross_positive_price_pnl_pp": gp, "gross_negative_price_pnl_pp": gl,
            "risk_weighted_proxy_n": len(weighted), "risk_weighted_proxy_sum_pp": sum(weighted),
            "risk_weighted_proxy_profit_factor": wgp/wgl if wgl else None,
            "planned_rr_absolute": distribution(r["planned_rr_absolute"] for r in rows),
            "planned_rr_signed": distribution(r["planned_rr_signed"] for r in rows),
            "sl_distance_pct": distribution(r["sl_distance_pct"] for r in rows),
            "tp_signed_distance_pct": distribution(r["tp_signed_distance_pct"] for r in rows),
            "planned_rr_absolute_below_1_5": sum(r["planned_rr_absolute"] is not None and r["planned_rr_absolute"] < 1.5 for r in rows),
            "invalid_price_geometry": sum(not r["valid_geometry"] for r in rows),
            "sl_over_5_pct": sum(r["sl_distance_pct"] is not None and r["sl_distance_pct"] > 5 for r in rows),
            "duration_hours": distribution(r["duration_hours"] for r in closed),
            "realized_r": distribution(r["realized_r"] for r in closed),
            "first_created_at_utc": min((r["created_at"] for r in rows), default=None),
            "last_created_at_utc": max((r["created_at"] for r in rows), default=None)}


def grouped(rows, key):
    groups = collections.defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    return {name: stats(group) for name, group in sorted(groups.items())}


def audit_log(path):
    raw = git_bytes(path)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    timestamps, summaries, gatecounts, subreasons, days = [], [], collections.Counter(), collections.Counter(), {}
    riskvalues, examples = collections.Counter(), {}
    attempts = collections.Counter()
    for number, line in enumerate(lines, 1):
        if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line):
            timestamps.append(line[:19])
        if "[FUNNEL SUMMARY]" in line:
            values = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line.split("[FUNNEL SUMMARY]", 1)[1])}
            summaries.append({"timestamp_local": line[:19], "line": number, **values})
            day = days.setdefault(line[:10], collections.Counter())
            day.update(values)
            day["cycles"] += 1
            day["zero_signal_cycles"] += values.get("sent", 0) == 0
        match = re.search(r"\[FUNNEL\] (\S+) (\S+) → (\w+): BLOCKED(?: \((.*)\))?", line)
        if match:
            symbol, tf, gate, reason = match.groups()
            gatecounts[gate] += 1
            attempts[tf] += 1
            if gate == "portfolio_risk":
                reason = reason or ""
                category = ("per_symbol_limit" if reason.startswith("max active signals for ") else
                            "portfolio_budget" if reason.startswith("portfolio risk ") else
                            "global_count_limit" if reason.startswith("max active") else "unknown")
                subreasons[category] += 1
                examples.setdefault(category, {"line": number, "timestamp_local": line[:19], "reason": reason})
                rv = re.search(r"portfolio risk ([\d.]+)% >= ([\d.]+)%", reason)
                if rv:
                    riskvalues[f"{rv.group(1)}>={rv.group(2)}"] += 1
    totals = collections.Counter()
    for summary in summaries:
        totals.update({k:v for k,v in summary.items() if k not in ("timestamp_local", "line")})
    entered = totals.get("entered", 0)
    return {"path": path, "git_ref": REF, "sha256": hashlib.sha256(raw).hexdigest(),
            "lines": len(lines), "first_timestamp_local": min(timestamps, default=None),
            "last_timestamp_local": max(timestamps, default=None), "cycles": len(summaries),
            "zero_signal_cycles": sum(s.get("sent", 0)==0 for s in summaries), "summary_totals": dict(totals),
            "portfolio_gate_fraction_all_scan_entries_pct": 100*totals.get("portfolio_risk",0)/entered if entered else None,
            "blocked_gate_log_counts": dict(gatecounts), "portfolio_subreasons": dict(subreasons),
            "portfolio_risk_logged_values": dict(riskvalues), "portfolio_subreason_examples": examples,
            "blocked_attempts_by_timeframe": dict(attempts), "daily": {d:dict(v) for d,v in days.items()},
            "summaries": summaries}


def main():
    global ROOT, OUT, REF
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=ROOT)
    parser.add_argument("--output", type=pathlib.Path, default=OUT)
    parser.add_argument("--ref", default=REF)
    args = parser.parse_args()
    ROOT, OUT, REF = args.repo.resolve(), args.output.resolve(), args.ref
    dbpath = ROOT / "data/signals.db"
    before = hashlib.sha256(dbpath.read_bytes()).hexdigest()
    con = sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    counts = {t:con.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0] for t in tables}
    rows = [dict(r) for r in con.execute("SELECT s.*,o.status,o.closed_at,o.close_price AS exit_price,o.pnl_pct,o.risk_pct,o.checked_at FROM signals s JOIN signal_outcomes o ON o.signal_id=s.id ORDER BY s.id")]
    for r in rows:
        entry, sl, tp = r["close_price"], r["sl"], r["tp"]
        sign = 1 if r["signal_type"]=="BUY" else -1
        r["sl_distance_pct"] = abs(entry-sl)/entry*100 if entry and sl is not None else None
        r["tp_signed_distance_pct"] = sign*(tp-entry)/entry*100 if entry and tp is not None else None
        r["valid_geometry"] = (0 < sl < entry < tp) if sign==1 else (0 < tp < entry < sl)
        distance = r["sl_distance_pct"]
        r["planned_rr_absolute"] = abs(tp-entry)/abs(entry-sl) if distance else None
        r["planned_rr_signed"] = sign*(tp-entry)/abs(entry-sl) if distance else None
        r["realized_r"] = r["pnl_pct"]/distance if distance and r["pnl_pct"] is not None else None
        r["pnl_risk_weighted_pp"] = r["risk_pct"]*r["realized_r"] if r["risk_pct"] is not None and r["realized_r"] is not None else None
        r["duration_hours"] = (date(r["closed_at"])-date(r["created_at"])).total_seconds()/3600 if r["closed_at"] else None
        r["month"] = r["created_at"][:7]
        fp = r["factor_fingerprint"] or ""
        r["logging_cohort_NOT_strategy_version"] = ("compact_components" if fp.startswith("components=") else "feature_names_with_quality" if "bos_quality" in fp else "feature_names_with_smt" if "smt_divergence_score" in fp else "feature_names_without_smt")
        component = re.search(r"components=(\d+)", r["reasons"] or "")
        r["logged_components"] = component.group(1) if component else "unavailable"
    asof = max(date(r[field]) for r in rows for field in ("created_at","closed_at","checked_at") if r[field])
    open_rows = []
    for r in rows:
        if r["status"] == "OPEN":
            opened = {k:r[k] for k in ("id","symbol","timeframe","created_at","checked_at","risk_pct")}
            opened["age_hours_at_snapshot"] = (asof-date(r["created_at"])).total_seconds()/3600
            opened["age_bars_at_snapshot"] = opened["age_hours_at_snapshot"]/({"1h":1,"4h":4}[r["timeframe"]])
            open_rows.append(opened)
    anomalies = [{k:r[k] for k in ("id","symbol","timeframe","signal_type","created_at","close_price","sl","tp","exit_price","status","pnl_pct","risk_pct","planned_rr_absolute","planned_rr_signed")} for r in rows if not r["valid_geometry"]]
    presence = {k:sum(r[k] is not None for r in rows) for k in ("execution_snapshot","confidence_v2_factors","entry_price_source","entry_candle_open","mfe_pct","mae_pct","signal_detected_at","telegram_sent_at")}
    null_risks = sum(r["risk_pct"] is None for r in rows)
    foreign = list(con.execute("PRAGMA foreign_key_check"))
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    duplicate_outcomes = [dict(r) for r in con.execute("SELECT signal_id,COUNT(*) AS n FROM signal_outcomes GROUP BY signal_id HAVING COUNT(*)>1")]
    con.close()
    logpaths = subprocess.check_output(["git","ls-tree","-r","--name-only",REF,"logs"],cwd=ROOT,text=True).splitlines()
    logs = [audit_log(p) for p in logpaths if p.endswith((".log",".txt"))]
    after = hashlib.sha256(dbpath.read_bytes()).hexdigest()
    assert before==after, "Audit must never mutate input DB"
    db_at_ref_sha256 = hashlib.sha256(git_bytes("data/signals.db")).hexdigest()
    report = {"source":{"ref":REF,"resolved_commit":subprocess.check_output(["git","rev-parse",REF],cwd=ROOT,text=True).strip(),"db_sha256_at_ref":db_at_ref_sha256,"working_db_matches_ref":before==db_at_ref_sha256,"db_sha256_before":before,"db_sha256_after":after,"read_only_uri":True,"snapshot_asof_utc":asof.isoformat(),"table_counts":counts,"integrity_check":integrity,"foreign_key_violations":len(foreign),"duplicate_outcomes":duplicate_outcomes},
              "metrics":stats(rows),"by_outcome":grouped(rows,"status"),"by_direction":grouped(rows,"signal_type"),"by_timeframe":grouped(rows,"timeframe"),"by_symbol":grouped(rows,"symbol"),"by_month":grouped(rows,"month"),"by_logging_cohort_NOT_version":grouped(rows,"logging_cohort_NOT_strategy_version"),"by_logged_components":grouped(rows,"logged_components"),
              "invalid_geometry_rows":anomalies,"open_rows":open_rows,"open_risk_sum_pct":sum(r["risk_pct"] or 0 for r in open_rows),"null_risk_rows":null_risks,"evidence_field_nonnull_counts":presence,
              "limitations":["Database is a September 14 snapshot, not the live September 21 account.","pnl_pct is a price-return estimate; arithmetic sum is percentage points, not account return.","Risk-weighted proxy = risk_pct * pnl_pct / SL_distance_pct; no actual size/equity/fill/funding ledger exists.","No setup_type or strategy_version is persisted on signals; fingerprint cohorts describe logging format only.","Log timestamps appear UTC+3 versus signal UTC timestamps (match signal 143 to notifier); log timezone is not explicitly persisted.","Do not concatenate rotated logs: overlapping records require deduplication; bot.log is the single 250-cycle baseline.","Counterfactual throughput at 5/8% cannot be inferred because blocked candidates have neither downstream traces nor outcomes."],"logs":logs}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"aggregate.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"metrics":report["metrics"],"by_direction":report["by_direction"],"open_risk_sum_pct":report["open_risk_sum_pct"],"logs":[{k:l[k] for k in ("path","cycles","zero_signal_cycles","summary_totals","portfolio_subreasons","portfolio_risk_logged_values","first_timestamp_local","last_timestamp_local")} for l in logs]},ensure_ascii=True,indent=2))


if __name__=="__main__":
    main()
