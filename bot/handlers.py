"""
bot/handlers.py — Обработчики команд Telegram бота
"""
from datetime import timezone
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, Application
)
from telegram.constants import ParseMode
from loguru import logger
from config.settings import config, get_active_symbols
from storage.database import db
from bot.menu import (
    send_main_menu, handle_menu_callback, handle_menu_message
)
from bot.admin import (
    addsymbol_command, removesymbol_command, listsymbols_command,
    setparam_command, disable_command, enable_command, exportdb_command,
    stats_command, trades_command, closetrade_command, hypotheses_command,
)


def _is_admin(user_id: int) -> bool:
    return user_id in config.telegram.admin_ids


def _admin_only(func):
    """Декоратор — только для администраторов"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not _is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещён.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Справка</b>\n\n"
        "<b>/start</b> — главное меню\n"
        "<b>/menu</b> — главное меню\n"
        "<b>/status</b> — состояние сканера\n"
        "<b>/lastsignal</b> — последние 5 сигналов\n"
        "<b>/symbols</b> — отслеживаемые символы\n"
        "<b>/trades</b> — открытые сделки\n"
        "<b>/closetrade ID</b> — закрыть сделку вручную\n"
        "<b>/scan</b> — ручной запуск сканирования (только для admin)\n"
        "<b>/settings</b> — текущие настройки индикаторов (только для admin)\n\n"
        "🔍 <b>Логика сигналов:</b>\n"
        "• ICT Core: Pattern Engine → Feature Builder → Probability Engine → Risk Engine\n"
        "• SL/TP на основе BOS или ATR\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols = get_active_symbols()
    timeframes = config.trading.primary_timeframes
    confirm_tf = config.trading.confirm_timeframe
    text = (
        "⚙️ <b>Статус бота</b>\n\n"
        f"✅ Бот активен\n"
        f"📊 Символов: <b>{len(symbols)}</b>\n"
        f"⏱ Таймфреймы: <b>{', '.join(timeframes)}</b>\n"
        f"🔁 Подтверждение: <b>{confirm_tf}</b>\n"
        f"📋 Список: <code>{', '.join(symbols)}</code>\n"
        f"⏰ Cooldown между сигналами: <b>{config.signal_cooldown_minutes} мин</b>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_lastsignal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signals = await db.get_recent_signals(limit=5)
    if not signals:
        await update.message.reply_text("📭 Сигналов пока нет.")
        return

    lines = ["📜 <b>Последние сигналы:</b>\n"]
    for sig in signals:
        emoji = "🟢" if sig.signal_type == "BUY" else "🔴"
        dt = sig.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_str = dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"
        lines.append(
            f"{emoji} <b>{sig.signal_type}</b> {sig.symbol} {sig.timeframe} "
            f"@ {sig.close_price:.4f} | {dt_str}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols = get_active_symbols()
    lines = ["📊 <b>Отслеживаемые символы:</b>\n"]
    for s in symbols:
        lines.append(f"• {s}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск сканирования. /scan — все токены, /scan ZRO — один токен."""
    from scheduler.scanner import run_scan_cycle
    from bot.notifier import send_signal_blocked
    from web.signal_events import dual_notify

    if context.args:
        raw = context.args[0].upper().strip()
        symbol = raw if "/" in raw else f"{raw}/USDT"
        await update.message.reply_text(f"🔍 Анализирую <b>{symbol}</b>…", parse_mode=ParseMode.HTML)
        try:
            from bot.menu import _do_full_analysis
            result = await _do_full_analysis(symbol)
            await update.message.reply_text(result, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Single scan error for {symbol}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {e}")
    else:
        await update.message.reply_text("🔍 Запускаю сканирование...")
        try:
            await run_scan_cycle(dual_notify, blocked_callback=send_signal_blocked)
            await update.message.reply_text("✅ Сканирование завершено.")
        except Exception as e:
            logger.error(f"Manual scan error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {e}")


@_admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = config.trading
    text = (
        "⚙️ <b>Настройки индикаторов</b>\n\n"
        f"EMA Fast: <b>{cfg.ema_fast}</b>\n"
        f"EMA Slow: <b>{cfg.ema_slow}</b>\n"
        f"EMA Trend: <b>{cfg.ema_trend}</b>\n"
        f"RSI Period: <b>{cfg.rsi_period}</b>\n"
        f"RSI Overbought: <b>{cfg.rsi_overbought}</b>\n"
        f"RSI Oversold: <b>{cfg.rsi_oversold}</b>\n"
        f"MACD: <b>{cfg.macd_fast}/{cfg.macd_slow}/{cfg.macd_signal}</b>\n"
        f"ADX Period: <b>{cfg.adx_period}</b>\n"
        f"ADX Min (anti-flat): <b>{cfg.adx_min}</b>\n"
        f"ATR Period: <b>{cfg.atr_period}</b>\n"
        f"ATR SL multiplier: <b>{cfg.atr_multiplier_sl}x</b>\n"
        f"ATR TP multiplier: <b>{cfg.atr_multiplier_tp}x</b>\n"
        f"Supertrend: <b>{cfg.supertrend_period}/{cfg.supertrend_multiplier}</b>\n"
        f"Volume factor: <b>{cfg.volume_factor}x SMA</b>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("lastsignal", cmd_lastsignal))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("settings", cmd_settings))
    # Admin symbol management
    app.add_handler(CommandHandler("addsymbol", addsymbol_command))
    app.add_handler(CommandHandler("removesymbol", removesymbol_command))
    app.add_handler(CommandHandler("listsymbols", listsymbols_command))
    # F4 admin commands
    app.add_handler(CommandHandler("setparam", setparam_command))
    app.add_handler(CommandHandler("disable", disable_command))
    app.add_handler(CommandHandler("enable", enable_command))
    app.add_handler(CommandHandler("exportdb", exportdb_command))
    # F1: /stats
    app.add_handler(CommandHandler("stats", stats_command))
    # F2: /trades, /closetrade
    app.add_handler(CommandHandler("trades", trades_command))
    app.add_handler(CommandHandler("closetrade", closetrade_command))
    # F3: /hypotheses (new pipeline debug)
    app.add_handler(CommandHandler("hypotheses", hypotheses_command))
    # Menu navigation (callbacks + text input for custom token)
    app.add_handler(CallbackQueryHandler(handle_menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_message))
    logger.info("Telegram handlers registered (menu: callbacks + text input)")
