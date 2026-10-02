"""
scheduler/outcome_tracker.py — Фоновый трекинг закрытия сигналов (SL/TP/TTL).

PnL is calculated AFTER costs:
- Exchange commission (taker fee, both sides)
- Slippage (both sides)
- Estimated funding cost for perpetuals (0.01% per 8h, scaled by hold time)

Reliability rules:
- TTL is evaluated even when the OHLCV window cannot be loaded (ticker fallback
  after WINDOW_FAILURE_LIMIT bad loads) — an unreadable outcome must never stay
  OPEN forever, it blocks the portfolio risk budget.
"""
import asyncio
import html
import os
from datetime import datetime, timezone, timedelta
from loguru import logger
from data.exchange_client import exchange_client
from storage.database import db
from config.settings import config
from scheduler.outcome_window import first_touch, load_outcome_window


OUTCOME_CHECK_INTERVAL_SECONDS = int(
    os.getenv("OUTCOME_CHECK_INTERVAL_SECONDS", "300")
)
# Single source of truth: config.settings (also used by backtest/engine.py to
# give backtest trades the same lifetime as a live signal).
OUTCOME_TTL_DAYS = config.outcome_ttl_days

# Funding rate estimate for perpetuals (default 0.01% per 8h)
FUNDING_RATE_8H = float(os.getenv("FUNDING_RATE_8H", "0.0001"))

# Per-symbol failure tracking: after SYMBOL_FETCH_FAIL_THRESHOLD consecutive
# failures, skip the symbol for SYMBOL_FETCH_COOLDOWN_SECONDS.
SYMBOL_FETCH_FAIL_THRESHOLD = 3
SYMBOL_FETCH_COOLDOWN_SECONDS = 600  # 10 min cooldown
_symbol_fail_count: dict[str, int] = {}
_symbol_cooldown_until: dict[str, datetime] = {}
# Per-outcome failure counter for load_outcome_window: an unreadable window
# must not keep an outcome OPEN forever (an OPEN outcome eats portfolio risk
# budget). After WINDOW_FAILURE_LIMIT bad loads we evaluate on the ticker only
# and advance checked_at so the window stops growing.
_window_failures: dict[int, int] = {}
WINDOW_FAILURE_LIMIT = int(os.getenv("OUTCOME_WINDOW_FAILURE_LIMIT", "3"))
# Lock to protect _symbol_fail_count and _symbol_cooldown_until from
# concurrent async access (race condition between check_open_outcomes
# and parallel scanner tasks).
_symbol_tracker_lock = asyncio.Lock()


async def _send_close_notification(
    signal, status: str, current_price: float, net_pnl: float
) -> None:
    """Отправить уведомление в Telegram о закрытии сделки (TP/SL/TTL)."""
    if not config.telegram.channel_id:
        return
    try:
        from bot.notifier import get_bot
        from telegram.constants import ParseMode

        bot = get_bot()
        if status == "HIT_TP":
            emoji, action = "✅", "Тейк Профит"
        elif status == "HIT_SL":
            emoji, action = "🛑", "Стоп Лосс"
        else:  # EXPIRED
            emoji, action = "⌛", "Сделка закрыта по TTL"
        pnl_sign = "+" if net_pnl >= 0 else ""
        pnl_cls = "green" if net_pnl >= 0 else "red"

        text = (
            f"{emoji} <b>Сделка закрыта — {action}</b>\n\n"
            f"📊 {html.escape(signal.signal_type)} {html.escape(signal.symbol)} {html.escape(signal.timeframe)}\n"
            f"💰 Entry: <code>{signal.close_price}</code>\n"
            f"📍 Закрытие: <code>{current_price}</code>\n"
            f"📈 PnL: <b>{pnl_sign}{net_pnl:.2f}%</b>"
        )

        await bot.send_message(
            chat_id=config.telegram.channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        logger.info(f"Close notification sent: {signal.symbol} {status}")
    except Exception as e:
        logger.warning(f"Failed to send close notification: {e}")


def _calculate_net_pnl(
    signal_type: str,
    entry_price: float,
    exit_price: float,
    created_at: datetime,
    resolved_at: datetime,
) -> tuple[float, float]:
    """Calculate net PnL after costs.

    Costs deducted:
    - Exchange commission (both sides)
    - Slippage (both sides)
    - Estimated funding for perpetuals

    Returns:
        (gross_pnl_pct, net_pnl_pct)
    """
    fee_pct = config.trading.exchange_fee_pct
    slippage_pct = config.trading.slippage_pct

    # Gross PnL (before costs)
    if signal_type == "BUY":
        gross_pnl = (exit_price - entry_price) / entry_price * 100
    else:
        gross_pnl = (entry_price - exit_price) / entry_price * 100

    # Total round-trip cost: commission(2x) + slippage(2x)
    round_trip_cost_pct = (fee_pct + slippage_pct) * 2

    # Funding cost for perpetuals (estimated)
    funding_cost_pct = 0.0
    if config.exchange.market_type in ("swap", "future"):
        hold_hours = (resolved_at - created_at).total_seconds() / 3600
        n_funding_periods = hold_hours / 8.0  # funding every 8h
        funding_cost_pct = n_funding_periods * FUNDING_RATE_8H * 100

    net_pnl = gross_pnl - round_trip_cost_pct - funding_cost_pct
    return gross_pnl, net_pnl


def _calculate_excursion(
    signal_type: str,
    entry_price: float,
    high: float,
    low: float,
) -> tuple[float, float]:
    """Calculate Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE).

    Returns:
        (mfe_pct, mae_pct) — both as percentages
    """
    if signal_type == "BUY":
        # For BUY: favorable = price goes up, adverse = price goes down
        mfe_pct = ((high - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        mae_pct = ((entry_price - low) / entry_price) * 100 if entry_price > 0 else 0
    else:  # SELL
        # For SELL: favorable = price goes down, adverse = price goes up
        mfe_pct = ((entry_price - low) / entry_price) * 100 if entry_price > 0 else 0
        mae_pct = ((high - entry_price) / entry_price) * 100 if entry_price > 0 else 0
    return mfe_pct, mae_pct


async def check_open_outcomes() -> None:
    outcomes = await db.get_open_outcomes()
    if not outcomes:
        return
    logger.info(f"Checking {len(outcomes)} open outcomes")
    now = datetime.now(timezone.utc)
    for outcome in outcomes:
        signal = await db.get_signal(outcome.signal_id)
        if signal is None:
            continue

        # Skip symbols on cooldown after repeated fetch failures
        async with _symbol_tracker_lock:
            cooldown_until = _symbol_cooldown_until.get(signal.symbol)
        if cooldown_until and now < cooldown_until:
            continue
        # Expiry is decided BEFORE the window load: an unreadable window must
        # never keep an expired outcome OPEN — OPEN outcomes eat portfolio
        # risk budget (62% of all scan blocks come from that budget).
        created_naive = signal.created_at.replace(tzinfo=timezone.utc)
        is_expired = now - created_naive > timedelta(days=OUTCOME_TTL_DAYS)

        # Текущая цена через ticker (real-time, не свеча)
        current_price = await exchange_client.fetch_ticker_price(signal.symbol)
        if current_price is None:
            async with _symbol_tracker_lock:
                _symbol_fail_count[signal.symbol] = _symbol_fail_count.get(signal.symbol, 0) + 1
                if _symbol_fail_count[signal.symbol] >= SYMBOL_FETCH_FAIL_THRESHOLD:
                    _symbol_cooldown_until[signal.symbol] = now + timedelta(seconds=SYMBOL_FETCH_COOLDOWN_SECONDS)
                    logger.warning(
                        f"Symbol {signal.symbol} hit {SYMBOL_FETCH_FAIL_THRESHOLD} consecutive "
                        f"fetch failures — skipping for {SYMBOL_FETCH_COOLDOWN_SECONDS}s"
                    )
            continue
        # Success — reset failure tracking
        async with _symbol_tracker_lock:
            _symbol_fail_count.pop(signal.symbol, None)

        hit_tp = hit_sl = False
        candle_high = candle_low = float(current_price)
        try:
            bars = await load_outcome_window(exchange_client, signal, outcome, now)
            hit_tp, hit_sl, candle_high, candle_low = first_touch(
                signal, bars, signal.created_at, now, float(current_price),
            )
            _window_failures.pop(outcome.id, None)
        except (ValueError, TypeError, OverflowError) as exc:
            logger.warning(f"Outcome history incomplete: signal={signal.id}: {exc}")
            failures = _window_failures.get(outcome.id, 0) + 1
            _window_failures[outcome.id] = failures
            if not is_expired and failures < WINDOW_FAILURE_LIMIT:
                continue
            # Ticker-only fallback: the current price is still authoritative
            # for SL/TP, so we only lose the intrabar path (MFE/MAE precision).
            logger.warning(
                f"Outcome {outcome.id}: ticker-only evaluation "
                f"(window failed {failures}x, expired={is_expired})"
            )
            await db.touch_outcome_checked(outcome.id, checked_at=now)
            price = float(current_price)
            if signal.signal_type == "BUY":
                hit_tp, hit_sl = price >= signal.tp, price <= signal.sl
            else:
                hit_tp, hit_sl = price <= signal.tp, price >= signal.sl
            candle_high = candle_low = price

        if hit_tp:
            # Use TP price as close if candle hit it (better fill estimate)
            close_price = signal.tp if (
                (signal.signal_type == "BUY" and candle_high >= signal.tp) or
                (signal.signal_type == "SELL" and candle_low <= signal.tp)
            ) else current_price
            gross_pnl, net_pnl = _calculate_net_pnl(
                signal.signal_type, signal.close_price, close_price,
                signal.created_at.replace(tzinfo=timezone.utc), now,
            )
            # Calculate MFE/MAE using entry price and candle extremes
            mfe_pct, mae_pct = _calculate_excursion(
                signal.signal_type, signal.close_price, candle_high, candle_low,
            )
            await db.close_outcome(outcome.id, "HIT_TP", close_price, net_pnl)
            await db.update_signal_excursion(signal.id, mfe_pct, mae_pct)
            try:
                await db.update_candidate_outcome_by_signal(signal.id, "HIT_TP", net_pnl)
            except Exception:
                pass
            try:
                from storage.database import DecisionTrace
                from sqlalchemy import select
                async with db._session_factory() as session:
                    result = await session.execute(
                        select(DecisionTrace).where(DecisionTrace.signal_id == signal.id)
                    )
                    trace_row = result.scalar_one_or_none()
                    if trace_row:
                        trace_row.outcome = "HIT_TP"
                        trace_row.pnl_pct = net_pnl
                        await session.commit()
            except Exception:
                pass
            logger.info(
                f"Outcome RESOLVED: {signal.symbol} {signal.signal_type} -> HIT_TP "
                f"at {current_price} gross={gross_pnl:+.2f}% net={net_pnl:+.2f}% "
                f"(entry={signal.close_price}, SL={signal.sl}, TP={signal.tp})"
            )
            await _send_close_notification(signal, "HIT_TP", close_price, net_pnl)
        elif hit_sl:
            # Use SL price as close if candle hit it (better fill estimate)
            close_price = signal.sl if (
                (signal.signal_type == "BUY" and candle_low <= signal.sl) or
                (signal.signal_type == "SELL" and candle_high >= signal.sl)
            ) else current_price
            gross_pnl, net_pnl = _calculate_net_pnl(
                signal.signal_type, signal.close_price, close_price,
                signal.created_at.replace(tzinfo=timezone.utc), now,
            )
            # Calculate MFE/MAE using entry price and candle extremes
            mfe_pct, mae_pct = _calculate_excursion(
                signal.signal_type, signal.close_price, candle_high, candle_low,
            )
            await db.close_outcome(outcome.id, "HIT_SL", close_price, net_pnl)
            await db.update_signal_excursion(signal.id, mfe_pct, mae_pct)
            try:
                await db.update_candidate_outcome_by_signal(signal.id, "HIT_SL", net_pnl)
            except Exception:
                pass
            try:
                from storage.database import DecisionTrace
                from sqlalchemy import select
                async with db._session_factory() as session:
                    result = await session.execute(
                        select(DecisionTrace).where(DecisionTrace.signal_id == signal.id)
                    )
                    trace_row = result.scalar_one_or_none()
                    if trace_row:
                        trace_row.outcome = "HIT_SL"
                        trace_row.pnl_pct = net_pnl
                        await session.commit()
            except Exception:
                pass
            logger.info(
                f"Outcome RESOLVED: {signal.symbol} {signal.signal_type} -> HIT_SL "
                f"at {current_price} gross={gross_pnl:+.2f}% net={net_pnl:+.2f}% "
                f"(entry={signal.close_price}, SL={signal.sl}, TP={signal.tp})"
            )
            await _send_close_notification(signal, "HIT_SL", close_price, net_pnl)
        elif is_expired:
            close_price = float(current_price)
            gross_pnl, net_pnl = _calculate_net_pnl(
                signal.signal_type, signal.close_price, close_price,
                created_naive, now,
            )
            mfe_pct, mae_pct = _calculate_excursion(
                signal.signal_type, signal.close_price, candle_high, candle_low,
            )
            await db.close_outcome(outcome.id, "EXPIRED", close_price, net_pnl)
            await db.update_signal_excursion(signal.id, mfe_pct, mae_pct)
            try:
                await db.update_candidate_outcome_by_signal(signal.id, "EXPIRED", net_pnl)
            except Exception:
                pass
            try:
                from storage.database import DecisionTrace
                from sqlalchemy import select
                async with db._session_factory() as session:
                    rows = (await session.execute(
                        select(DecisionTrace).where(DecisionTrace.signal_id == signal.id)
                    )).scalars().all()
                    for row in rows:
                        row.outcome = "EXPIRED"
                        row.pnl_pct = net_pnl
                    await session.commit()
            except Exception:
                pass
            _window_failures.pop(outcome.id, None)
            logger.info(
                f"Outcome EXPIRED: signal={signal.id} price={close_price} "
                f"net={net_pnl:+.2f}% mfe={mfe_pct:.2f}% mae={mae_pct:.2f}% "
                f"(entry={signal.close_price}, SL={signal.sl}, TP={signal.tp})"
            )
            await _send_close_notification(signal, "EXPIRED", close_price, net_pnl)
        else:
            await db.touch_outcome_checked(outcome.id, checked_at=now)


async def outcome_tracker_loop() -> None:
    while True:
        try:
            await check_open_outcomes()
        except Exception as e:
            logger.warning(f"Outcome tracker error: {e}")
        await asyncio.sleep(OUTCOME_CHECK_INTERVAL_SECONDS)
