"""
web/signal_events.py — Dual notification: Telegram + Web Dashboard.

Single source of truth for the combined notify_callback used by both
the scheduler and the /scan command handler.
"""
import asyncio
from loguru import logger


async def dual_notify(result, context_verdict=None):
    """Отправить сигнал и в Telegram, и в web dashboard параллельно."""
    from bot.notifier import send_signal
    from web.server import broadcast_signal_event

    await asyncio.gather(
        send_signal(result, context_verdict),
        broadcast_signal_event({
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "direction": result.signal.value,
            "entry": result.entry_price,
            "sl": result.sl,
            "tp": result.tp,
            "confidence": result.confidence,
            "message": result.format_message(),
        }),
        return_exceptions=True,
    )
