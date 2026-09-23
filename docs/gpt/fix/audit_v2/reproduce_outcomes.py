"""Isolated reproductions against the audit commit; no database/network writes."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pandas as pd
from scheduler import outcome_tracker as tracker


async def case(name, *, ticker, highs, lows, age_hours=2, bar_offsets=None):
    now = datetime.now(timezone.utc)
    signal = SimpleNamespace(
        id=1, symbol="TEST/USDT", timeframe="1h", signal_type="BUY",
        close_price=100.0, sl=95.0, tp=110.0,
        created_at=now - timedelta(hours=age_hours),
        entry_candle_open=now - timedelta(hours=age_hours + 1),
    )
    outcome = SimpleNamespace(id=1, signal_id=1, checked_at=now-timedelta(minutes=5))
    offsets = bar_offsets or list(range(len(highs)-1, -1, -1))
    frame = pd.DataFrame({"high": highs, "low": lows}, index=[
        now.replace(minute=0, second=0, microsecond=0)-timedelta(hours=x)
        for x in offsets
    ])
    fake_db = SimpleNamespace(
        get_open_outcomes=AsyncMock(return_value=[outcome]),
        get_signal=AsyncMock(return_value=signal), close_outcome=AsyncMock(),
        update_signal_excursion=AsyncMock(), update_candidate_outcome_by_signal=AsyncMock(),
        touch_outcome_checked=AsyncMock(),
    )
    with patch.object(tracker,"db",fake_db), patch.object(
        tracker.exchange_client,"fetch_ticker_price",AsyncMock(return_value=ticker)
    ), patch.object(tracker.exchange_client,"fetch_ohlcv",AsyncMock(return_value=frame)), patch.object(
        tracker,"_send_close_notification",AsyncMock()
    ):
        await tracker.check_open_outcomes()
    call = fake_db.close_outcome.call_args
    return {"case":name,"resolution":list(call.args) if call else None,
            "resolution_kwargs":call.kwargs if call else None}


async def main():
    results = [
        await case("both_barriers_hit_tp_wins",ticker=100,highs=[111],lows=[94]),
        await case("ticker_sl_erased_by_candle",ticker=94,highs=[104],lows=[98]),
        await case("previous_bar_sl_ignored",ticker=100,highs=[104,103],lows=[94,99]),
        await case("pre_entry_wick_false_tp",ticker=100,highs=[111],lows=[99],age_hours=0.01),
        await case("expiry_discards_loss",ticker=96,highs=[102],lows=[96],age_hours=192),
    ]
    (Path(__file__).parent/"outcome-results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    print(json.dumps(results,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
