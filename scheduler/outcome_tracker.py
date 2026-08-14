"""
scheduler/outcome_tracker.py — Фоновый трекинг закрытия сигналов (SL/TP).

PnL is calculated AFTER costs:
- Exchange commission (taker fee, both sides)
- Slippage (both sides)
- Estimated funding cost for perpetuals (0.01% per 8h, scaled by hold time)
"""
import asyncio
import html
import json
import os
from datetime import datetime, timezone, timedelta
from loguru import logger
from data.exchange_client import exchange_client
from storage.database import db
from config.settings import config
# from strategy.scenario_memory import scenario_memory, ScenarioOutcomeRecord  # DELETED module


OUTCOME_CHECK_INTERVAL_SECONDS = int(
    os.getenv("OUTCOME_CHECK_INTERVAL_SECONDS", "300")
)
OUTCOME_TTL_DAYS = int(os.getenv("OUTCOME_TTL_DAYS", "7"))

# Funding rate estimate for perpetuals (default 0.01% per 8h)
FUNDING_RATE_8H = float(os.getenv("FUNDING_RATE_8H", "0.0001"))

# Per-symbol failure tracking: after SYMBOL_FETCH_FAIL_THRESHOLD consecutive
# failures, skip the symbol for SYMBOL_FETCH_COOLDOWN_SECONDS.
SYMBOL_FETCH_FAIL_THRESHOLD = 3
SYMBOL_FETCH_COOLDOWN_SECONDS = 600  # 10 min cooldown
_symbol_fail_count: dict[str, int] = {}
_symbol_cooldown_until: dict[str, datetime] = {}


async def _send_close_notification(
    signal, status: str, current_price: float, net_pnl: float
) -> None:
    """Отправить уведомление в Telegram о закрытии сделки (TP/SL)."""
    if not config.telegram.channel_id:
        return
    try:
        from bot.notifier import get_bot
        from telegram.constants import ParseMode

        bot = get_bot()
        emoji = "✅" if status == "HIT_TP" else "🛑"
        action = "Тейк Профит" if status == "HIT_TP" else "Стоп Лосс"
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
        # Просроченный сигнал → EXPIRED
        age = now - signal.created_at.replace(tzinfo=timezone.utc)
        if age > timedelta(days=OUTCOME_TTL_DAYS):
            await db.close_outcome(
                outcome.id, "EXPIRED",
                close_price=signal.close_price, pnl_pct=0.0,
            )
            continue

        # Skip if current candle is the same as the entry candle.
        # This prevents same-bar SL resolution when the candle's wick
        # already breached the SL level at signal creation time.
        # Uses entry_candle_open stored at signal creation (preferred)
        # or falls back to calculated value for backward compatibility.
        entry_candle_open = None
        if hasattr(signal, 'entry_candle_open') and signal.entry_candle_open is not None:
            entry_candle_open = signal.entry_candle_open.replace(tzinfo=timezone.utc)
        else:
            # Fallback: calculate from created_at and timeframe
            _TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                           "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}
            tf_minutes = _TF_MINUTES.get(signal.timeframe, 60)
            signal_ts = signal.created_at.replace(tzinfo=timezone.utc)
            tf_seconds = tf_minutes * 60
            signal_epoch = int(signal_ts.timestamp())
            entry_candle_open = datetime.fromtimestamp(
                signal_epoch - (signal_epoch % tf_seconds), tz=timezone.utc
            )

        # Calculate current candle open time
        now_epoch = int(now.timestamp())
        tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                      "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}
        tf_minutes_val = tf_minutes.get(signal.timeframe, 60)
        tf_seconds = tf_minutes_val * 60
        current_candle_open = datetime.fromtimestamp(
            now_epoch - (now_epoch % tf_seconds), tz=timezone.utc
        )

        if current_candle_open <= entry_candle_open:
            logger.debug(
                f"Skipping {signal.symbol} {signal.timeframe}: "
                f"still on or before entry candle "
                f"(entry_open={entry_candle_open.isoformat()}, "
                f"current_open={current_candle_open.isoformat()})"
            )
            continue

        # Skip symbols on cooldown after repeated fetch failures
        cooldown_until = _symbol_cooldown_until.get(signal.symbol)
        if cooldown_until and now < cooldown_until:
            continue
        # Текущая цена через ticker (real-time, не свеча)
        current_price = await exchange_client.fetch_ticker_price(signal.symbol)
        if current_price is None:
            _symbol_fail_count[signal.symbol] = _symbol_fail_count.get(signal.symbol, 0) + 1
            if _symbol_fail_count[signal.symbol] >= SYMBOL_FETCH_FAIL_THRESHOLD:
                _symbol_cooldown_until[signal.symbol] = now + timedelta(seconds=SYMBOL_FETCH_COOLDOWN_SECONDS)
                logger.warning(
                    f"Symbol {signal.symbol} hit {SYMBOL_FETCH_FAIL_THRESHOLD} consecutive "
                    f"fetch failures — skipping for {SYMBOL_FETCH_COOLDOWN_SECONDS}s"
                )
            continue
        # Success — reset failure tracking
        _symbol_fail_count.pop(signal.symbol, None)

        # Also check recent candle high/low to catch TP/SL spikes
        # that happened between tracker intervals
        candle_high = current_price
        candle_low = current_price
        try:
            candle_df = await exchange_client.fetch_ohlcv(signal.symbol, signal.timeframe, limit=2)
            if candle_df is not None and len(candle_df) >= 1:
                last_candle = candle_df.iloc[-1]
                candle_high = float(last_candle["high"])
                candle_low = float(last_candle["low"])
        except Exception:
            pass

        hit_tp = (
            signal.signal_type == "BUY" and signal.tp and candle_high >= signal.tp
        ) or (
            signal.signal_type == "SELL" and signal.tp and candle_low <= signal.tp
        )
        hit_sl = (
            signal.signal_type == "BUY" and signal.sl and candle_low <= signal.sl
        ) or (
            signal.signal_type == "SELL" and signal.sl and candle_high >= signal.sl
        )
        # Calculate hold bars for ScenarioMemory
        hold_bars = int((now - signal.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60 / max(1, {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}.get(signal.timeframe, 60)))
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
                        # Record outcome in ScenarioMemory
                        _record_hypothesis_outcome(
                            trace_row, signal, "HIT_TP", net_pnl,
                            mfe_pct, mae_pct, hold_bars,
                        )
            except Exception:
                pass
            logger.info(
                f"Outcome RESOLVED: {signal.symbol} {signal.signal_type} -> HIT_TP "
                f"at {current_price} gross={gross_pnl:+.2f}% net={net_pnl:+.2f}% "
                f"(entry={signal.close_price}, SL={signal.sl}, TP={signal.tp})"
            )
            await _send_close_notification(signal, "HIT_TP", current_price, net_pnl)
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
                        # Record outcome in ScenarioMemory
                        _record_hypothesis_outcome(
                            trace_row, signal, "HIT_SL", net_pnl,
                            mfe_pct, mae_pct, hold_bars,
                        )
            except Exception:
                pass
            logger.info(
                f"Outcome RESOLVED: {signal.symbol} {signal.signal_type} -> HIT_SL "
                f"at {current_price} gross={gross_pnl:+.2f}% net={net_pnl:+.2f}% "
                f"(entry={signal.close_price}, SL={signal.sl}, TP={signal.tp})"
            )
            await _send_close_notification(signal, "HIT_SL", current_price, net_pnl)
        else:
            await db.touch_outcome_checked(outcome.id)
            logger.debug(
                f"Outcome still open: signal_id={signal.id} {signal.symbol} "
                f"{signal.signal_type} price={current_price:.4f} SL={signal.sl:.4f} TP={signal.tp:.4f}"
            )


async def outcome_tracker_loop() -> None:
    while True:
        try:
            await check_open_outcomes()
        except Exception as e:
            logger.warning(f"Outcome tracker error: {e}")
        await asyncio.sleep(OUTCOME_CHECK_INTERVAL_SECONDS)


def _record_hypothesis_outcome(
    trace_row,
    signal,
    outcome: str,
    pnl_pct: float,
    mfe_pct: float,
    mae_pct: float,
    hold_bars: int,
) -> None:
    """Record trade outcome in ScenarioMemory for future learning.

    Extracts hypothesis data from DecisionTrace.hypothesis_snapshot JSON
    and creates a ScenarioOutcomeRecord.
    """
    try:
        if not trace_row.hypothesis_snapshot:
            return

        h_data = json.loads(trace_row.hypothesis_snapshot)
        narrative_type = h_data.get("narrative_type", "")
        if not narrative_type:
            return

        # Calculate actual RR from entry/exit
        entry_price = signal.close_price
        exit_price = signal.tp if outcome == "HIT_TP" else signal.sl
        if entry_price and exit_price and entry_price > 0:
            if signal.signal_type == "BUY":
                actual_rr = (exit_price - entry_price) / (entry_price - signal.sl) if signal.sl and entry_price > signal.sl else 0.0
            else:
                actual_rr = (entry_price - exit_price) / (signal.sl - entry_price) if signal.sl and signal.sl > entry_price else 0.0
        else:
            actual_rr = 0.0

        record = ScenarioOutcomeRecord(
            symbol=signal.symbol,
            hypothesis_name=h_data.get("hypothesis_id", ""),
            narrative_type=narrative_type,
            direction=h_data.get("direction", signal.signal_type.lower()),
            expected_rr=h_data.get("rr_ratio", 0.0),
            expected_p_tp=h_data.get("confidence", 0.0),
            expected_quality=h_data.get("quality", 0.0),
            expected_confidence=h_data.get("confidence", 0.0),
            actual_rr=actual_rr,
            actual_pnl_pct=pnl_pct,
            outcome=outcome,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            hold_bars=hold_bars,
        )
        scenario_memory.record_outcome(record)
        logger.debug(
            f"ScenarioMemory: recorded {outcome} for {signal.symbol} "
            f"{narrative_type} (rr={actual_rr:.2f}, pnl={pnl_pct:.2f}%)"
        )
    except Exception as e:
        logger.debug(f"Failed to record hypothesis outcome: {e}")
