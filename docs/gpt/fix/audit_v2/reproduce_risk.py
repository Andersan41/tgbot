"""Offline regression probes against reviewed HEAD; never opens the production DB."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
os.chdir(HERE)
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./probe.db"

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from config.settings import config
from risk.engine import RiskEngine, PortfolioState
from storage.database import Base, Database, DecisionTrace
from storage.trace import DecisionTraceBuilder


async def main():
    import pandas as pd
    import scheduler.scanner as scanner
    from strategy.pattern_engine import pattern_engine

    results = {}
    frame = pd.DataFrame(dict(open=[100.0]*9, high=[101.0]*9,
                              low=[99.0]*9, close=[100.0]*9, volume=[10.0]*9))
    fake_db = SimpleNamespace(
        get_cooldown=AsyncMock(return_value=None),
        get_active_signals_count_by_symbol=AsyncMock(return_value=0),
        get_active_signals_count=AsyncMock(return_value=0),
        get_portfolio_risk_sum=AsyncMock(return_value=0.0),
        save_decision_trace=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    with patch.object(scanner, "db", fake_db), \
         patch.object(scanner, "_get_indicators", AsyncMock(return_value=(SimpleNamespace(adx=20.0, atr=1., close=100.),frame))), \
         patch.object(scanner, "_detect_regime", return_value=SimpleNamespace(regime="trend")), \
         patch.object(config.trading, "block_compression_regime", True), \
         patch.object(scanner.exchange_client, "fetch_ohlcv", AsyncMock(return_value=None)), \
         patch.object(pattern_engine, "detect",return_value=SimpleNamespace(detected=False,rejection_reason="test",components_count=0)) as detect:
        await scanner.scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert detect.call_count == 1
        results["scanner_non_compression"] = "historical NameError fixed in 9d0da045; pattern reached"

    engine = RiskEngine(min_rr_ratio=1.5)
    normal = dict(entry_price=100.0, sl=99.0, tp=102.0, p_tp=0.65, confidence=0.65)
    budget = engine.evaluate(PortfolioState(active_count=2, total_risk_pct=2.9), **normal)
    assert budget.should_trade and 2.9 + budget.risk_pct > 3.0
    results["budget_overflow"] = dict(existing=2.9, added=budget.risk_pct, total=2.9 + budget.risk_pct)
    negative_edge = engine.evaluate(PortfolioState(), **(normal | dict(p_tp=0.2, confidence=0.2)))
    assert negative_edge.should_trade and negative_edge.kelly_fraction == 0.0 and negative_edge.risk_pct == 0.1
    results["nonpositive_kelly"] = vars(negative_edge)
    bad_price = engine.evaluate(PortfolioState(), **(normal | dict(entry_price=float("nan"))))
    assert bad_price.should_trade
    results["nan_accepted"] = vars(bad_price)

    database = Database()
    await database._engine.dispose()
    database._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    database._session_factory = sessionmaker(database._engine, class_=AsyncSession, expire_on_commit=False)
    async with database._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    trace = DecisionTraceBuilder("BTC/USDT", "1h")
    trace.passed("cooldown")
    trace.passed("portfolio_risk")
    trace.passed("indicators")
    trace.passed("pattern_engine")
    trace.blocked("htf_bias", "continuation buy vs HTF bearish")
    trace_id = await trace.save(database)
    assert trace_id > 0
    results["fresh_sqlite_trace_save"] = trace_id
    results["missing_htf_in_path"] = json.loads(trace.build_gate_path())
    assert all(not value.startswith("htf_bias:") for value in results["missing_htf_in_path"])

    trace = DecisionTraceBuilder("BTC/USDT", "1h")
    for gate in ("cooldown", "portfolio_risk", "indicators", "pattern_engine", "sl_tp", "risk_engine", "dedup"):
        trace.passed(gate)
    trace.set_features(dict(components=4, p_tp=0.65, risk_pct=1.0, atr_pct=1.5, context_score=0.2))
    results["retained_live_features"] = trace._features.copy()
    trace.set_signal("BUY", 4, 100.0, 99.0, 102.0)
    trace_id = await trace.save(database)
    await database.update_trace_outcome(trace_id, "HIT_TP", 2.0)
    stats = await database.get_trace_stats()
    cooldown = next(row for row in stats if row["gate"] == "cooldown")
    assert cooldown["wr_downstream"] is None
    results["lost_downstream_wr"] = cooldown
    await database._engine.dispose()
    (HERE / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
