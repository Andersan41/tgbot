from datetime import datetime, timezone
from math import ceil, floor, isfinite


def as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def first_touch(signal, bars, created_at, now, current_price):
    """Virtual fills; SL first only when both barriers share one minute."""
    created_at, now = as_utc(created_at), as_utc(now)
    observations = []
    for opened, row in bars:
        opened = as_utc(opened)
        if opened < created_at or opened > now:
            continue
        observations.append((float(row["high"]), float(row["low"])))
    # A ticker is observed after the historical bars; never overwrite it.
    observations.append((current_price, current_price))
    for high, low in observations:
        if not all(isfinite(x) and x > 0 for x in (high, low)):
            raise ValueError("invalid outcome observation")
        if signal.signal_type == "BUY":
            hit_sl, hit_tp = low <= signal.sl, high >= signal.tp
        else:
            hit_sl, hit_tp = high >= signal.sl, low <= signal.tp
        if hit_sl or hit_tp:
            return bool(hit_tp and not hit_sl), bool(hit_sl), high, low
    return False, False, current_price, current_price


async def load_outcome_window(client, signal, outcome, now):
    """Read every available minute from the last check, with overlap."""
    created_at = as_utc(signal.created_at)
    checked_at = as_utc(outcome.checked_at) if outcome.checked_at else created_at
    # Include a full entry-minute only when entry coincides with its opening.
    start_ms = max(ceil(created_at.timestamp() / 60) * 60000,
                   floor(min(checked_at, now).timestamp() / 60) * 60000)
    last_ms = floor(now.timestamp() / 60) * 60000
    bars = []
    cursor = start_ms
    while cursor <= last_ms:
        frame = await client.fetch_ohlcv(
            signal.symbol, "1m", limit=1000, since=cursor, drop_last=False,
        )
        if frame is None or frame.empty:
            raise ValueError("outcome history unavailable")
        progressed = False
        for opened, row in frame.sort_index().iterrows():
            stamp = int(opened.timestamp() * 1000)
            if stamp < cursor or stamp > last_ms:
                continue
            if stamp != cursor:
                raise ValueError("gap in outcome minute history")
            bars.append((opened.to_pydatetime(), row))
            cursor += 60000
            progressed = True
        if not progressed:
            raise ValueError("outcome history did not advance")
    return bars
