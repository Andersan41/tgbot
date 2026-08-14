"""
bot/admin.py — Admin-only команды управления ботом
"""
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from loguru import logger
from config.settings import get_active_symbols, refresh_runtime_symbols, config
from storage.database import db
from data.exchange_client import exchange_client


async def _ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка админа — возвращает True если не админ (и отправляет сообщение)."""
    if not update.effective_user:
        return False
    from config.settings import config
    from loguru import logger
    uid = update.effective_user.id
    logger.warning(f"_ensure_admin: user_id={uid}, admin_ids={config.telegram.admin_ids}, chat_type={update.effective_chat.type if update.effective_chat else 'N/A'}")
    if uid not in config.telegram.admin_ids:
        await update.message.reply_text("\u26d4\ufe0f \u0414\u043e\u0441\u0442\u0443\u043f \u0437\u0430\u043f\u0440\u0435\u0449\u0451\u043d.")
        return False
    return True


async def addsymbol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /addsymbol BTC/USDT")
        return
    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    current = get_active_symbols()
    if symbol in current:
        await update.message.reply_text(f"\u0423\u0436\u0435 \u0432 \u0441\u043f\u0438\u0441\u043a\u0435: {symbol}")
        return
    dynamic = await db.get_dynamic_symbols() or []
    if symbol not in dynamic:
        dynamic.append(symbol)
        await db.set_dynamic_symbols(dynamic)
    await refresh_runtime_symbols()
    active = get_active_symbols()
    await update.message.reply_text(f"\u2705 \u0414\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u043e: {symbol}\n\u0412\u0441\u0435\u0433\u043e: {len(active)}")


async def removesymbol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removesymbol BTC/USDT")
        return
    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    current = get_active_symbols()
    if symbol not in current:
        await update.message.reply_text(f"\u041d\u0435\u0442 \u0432 \u0441\u043f\u0438\u0441\u043a\u0435: {symbol}")
        return
    dynamic = await db.get_dynamic_symbols() or []
    if symbol in dynamic:
        dynamic = [s for s in dynamic if s != symbol]
        await db.set_dynamic_symbols(dynamic)
    await refresh_runtime_symbols()
    active = get_active_symbols()
    await update.message.reply_text(f"\u2705 \u0423\u0434\u0430\u043b\u0435\u043d\u043e: {symbol}\n\u041e\u0441\u0442\u0430\u043b\u043e\u0441\u044c: {len(active)}")


async def listsymbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    symbols = get_active_symbols()
    text = "\U0001f4ca <b>\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u0441\u0438\u043c\u0432\u043e\u043b\u044b:</b>\n" + "\n".join(
        f"  \u2022 {s}" for s in symbols
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── F4.1: /setparam ──────────────────────────────────────────────────

ALLOWED_RUNTIME_PARAMS: dict[str, type] = {
    "EMA_FAST": int, "EMA_SLOW": int, "EMA_TREND": int,
    "RSI_PERIOD": int, "RSI_OVERBOUGHT": float, "RSI_OVERSOLD": float,
    "RSI_BULL_MIN": float, "RSI_BEAR_MAX": float,
    "MACD_FAST": int, "MACD_SLOW": int, "MACD_SIGNAL": int,
    "ADX_PERIOD": int, "ADX_MIN": float,
    "ATR_PERIOD": int, "ATR_MULTIPLIER_SL": float, "ATR_MULTIPLIER_TP": float,
    "SUPERTREND_PERIOD": int, "SUPERTREND_MULTIPLIER": float,
    "VOLUME_FACTOR": float, "CANDLES_LIMIT": int,
}


async def setparam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /setparam EMA_FAST 7")
        return
    name, raw = args[0].upper(), args[1]
    caster = ALLOWED_RUNTIME_PARAMS.get(name)
    if caster is None:
        await update.message.reply_text(
            f"\u274c \u041f\u0430\u0440\u0430\u043c\u0435\u0442\u0440 {name} \u043d\u0435 whitelisted. \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435: "
            f"{', '.join(ALLOWED_RUNTIME_PARAMS)}"
        )
        return
    try:
        value = caster(raw)
    except ValueError:
        await update.message.reply_text(f"\u274c {raw} \u043d\u0435 \u043f\u0440\u0438\u0432\u043e\u0434\u0438\u0442\u0441\u044f \u043a {caster.__name__}")
        return
    await db.set_setting(f"param:{name}", str(value))
    from config.settings import reload_filter_toggles
    await reload_filter_toggles()
    await update.message.reply_text(
        f"\u2705 {name}={value}. \u041f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u043e (live)."
    )


# ── F4.2: /disable / /enable ─────────────────────────────────────────


async def disable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    symbol = (context.args[0] if context.args else "").upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    disabled = await db.get_disabled_symbols() or []
    if symbol in disabled:
        await update.message.reply_text(f"\u0423\u0436\u0435 \u0432\u044b\u043a\u043b\u044e\u0447\u0451\u043d: {symbol}")
        return
    await db.set_disabled_symbols(disabled + [symbol])
    await update.message.reply_text(f"\u23f8\ufe0f {symbol} \u0432\u044b\u043a\u043b\u044e\u0447\u0451\u043d. \u0421\u043a\u0430\u043d\u0435\u0440 \u043f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u0442.")


async def enable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    symbol = (context.args[0] if context.args else "").upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    disabled = await db.get_disabled_symbols() or []
    new = [s for s in disabled if s != symbol]
    await db.set_disabled_symbols(new)
    await update.message.reply_text(f"\u25b6\ufe0f {symbol} \u0432\u043a\u043b\u044e\u0447\u0451\u043d.")


# ── F1: /stats ───────────────────────────────────────────────────────


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return

    from analytics.performance import (
        overall_stats, segmented_stats, mfe_mae_analysis,
        format_overall_stats, format_segmented_stats, format_mfe_mae,
        format_stats_telegram, format_segmented_telegram,
    )

    args = context.args
    period = args[0] if args else None  # e.g. '7d', '30d', 'full'
    is_full = (period == "full") or (len(args) > 1 and args[1] == "full")

    # 1. Overall stats
    stats = await overall_stats(period)
    period_label = period or "all time"
    text = format_overall_stats(stats, period_label) + "\n\n"

    if is_full and stats.total > 0:
        # 2. Segmented by symbol
        by_sym = await segmented_stats("symbol", period)
        text += format_segmented_stats(by_sym, "По символам") + "\n\n"

        # 3. Segmented by timeframe
        by_tf = await segmented_stats("timeframe", period)
        text += format_segmented_stats(by_tf, "По таймфреймам") + "\n\n"

        # 4. Segmented by direction
        by_dir = await segmented_stats("direction", period)
        text += format_segmented_stats(by_dir, "По направлению") + "\n\n"

        # 5. Segmented by session
        by_sess = await segmented_stats("session", period)
        text += format_segmented_stats(by_sess, "По сессиям") + "\n\n"

        # 6. MFE/MAE
        mfe_mae = await mfe_mae_analysis(period)
        text += format_mfe_mae(mfe_mae)

    # Split into chunks for Telegram (max 4096 chars)
    if len(text) > 4000:
        parts = text.split("\n\n")
        chunk = ""
        for part in parts:
            if len(chunk) + len(part) > 3900:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
                chunk = part
            else:
                chunk += "\n\n" + part if chunk else part
        if chunk:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── F4.3: /exportdb ──────────────────────────────────────────────────


async def exportdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return
    from config.settings import config
    db_path = config.database_url.replace("sqlite+aiosqlite:///", "")
    import os as _os
    import shutil
    import tempfile

    if not Path(db_path).exists():
        await update.message.reply_text("\u274c \u0424\u0430\u0439\u043b \u0431\u0430\u0437\u044b \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d: " + db_path)
        return

    size = _os.path.getsize(db_path)
    if size > 49 * 1024 * 1024:
        await update.message.reply_text(
            f"\u274c \u0424\u0430\u0439\u043b \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u0431\u043e\u043b\u044c\u0448\u043e\u0439: {size / 1024 / 1024:.1f} MB > 50 MB"
        )
        return

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        shutil.copy(db_path, tmp.name)
        filename = f"signals_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.db"
        with open(tmp.name, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption="\U0001f4e6 Backup SQLite",
            )


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return

    from sqlalchemy import select
    from storage.database import Signal, SignalOutcome

    outcomes = await db.get_open_outcomes()
    if not outcomes:
        await update.message.reply_text("📭 Нет открытых сделок.")
        return

    lines = [f"📊 <b>Открытые сделки ({len(outcomes)}):</b>\n"]
    async with db._session_factory() as session:
        for o in outcomes:
            result = await session.execute(select(Signal).where(Signal.id == o.signal_id))
            sig = result.scalar_one_or_none()
            if sig is None:
                continue

            emoji = "🟢" if sig.signal_type == "BUY" else "🔴"
            entry = sig.close_price
            sl = sig.sl or 0
            tp = sig.tp or 0

            # SL distance %
            sl_pct = abs(entry - sl) / entry * 100 if entry else 0
            # TP distance %
            tp_pct = abs(tp - entry) / entry * 100 if entry else 0

            lines.append(
                f"<b>#{sig.id}</b> {emoji} <b>{sig.signal_type}</b> {sig.symbol} {sig.timeframe}\n"
                f"   Entry: <code>{entry}</code> | SL: <code>{sl}</code> ({sl_pct:.1f}%) | TP: <code>{tp}</code> ({tp_pct:.1f}%)\n"
            )

    lines.append("\n💡 Закрыть: /closetrade <b>ID</b>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def closetrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update, context):
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /closetrade <b>ID</b>\nID — номер сделки из /trades", parse_mode=ParseMode.HTML)
        return

    try:
        signal_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Используй /trades чтобы увидеть номера.")
        return

    from sqlalchemy import select
    from storage.database import Signal, SignalOutcome

    # Find the open outcome for this signal
    async with db._session_factory() as session:
        result = await session.execute(
            select(SignalOutcome).where(
                SignalOutcome.signal_id == signal_id,
                SignalOutcome.status == "OPEN",
            )
        )
        outcome = result.scalar_one_or_none()

    if outcome is None:
        await update.message.reply_text(f"❌ Сделка #{signal_id} не найдена или уже закрыта.")
        return

    # Get signal info
    signal = await db.get_signal(signal_id)
    if signal is None:
        await update.message.reply_text(f"❌ Сигнал #{signal_id} не найден в БД.")
        return

    # Get current price
    current_price = await exchange_client.fetch_ticker_price(signal.symbol)
    if current_price is None:
        await update.message.reply_text(f"❌ Не удалось получить текущую цену {signal.symbol}")
        return

    # Calculate PnL
    entry = signal.close_price
    if signal.signal_type == "BUY":
        pnl_pct = (current_price - entry) / entry * 100
    else:
        pnl_pct = (entry - current_price) / entry * 100

    # Deduct fees
    fee_pct = config.trading.exchange_fee_pct
    slippage_pct = config.trading.slippage_pct
    net_pnl = pnl_pct - (fee_pct + slippage_pct) * 2

    # Close outcome
    await db.close_outcome(outcome.id, "MANUAL_CLOSE", current_price, net_pnl)

    emoji = "🟢" if net_pnl >= 0 else "🔴"
    await update.message.reply_text(
        f"✅ <b>Сделка #{signal_id} закрыта</b>\n\n"
        f"{signal.signal_type} {signal.symbol} {signal.timeframe}\n"
        f"Entry: <code>{entry}</code>\n"
        f"Exit: <code>{current_price}</code>\n"
        f"PnL: <b>{emoji} {net_pnl:+.2f}%</b>",
        parse_mode=ParseMode.HTML,
    )


async def hypotheses_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать текущие гипотезы для символа (debug)."""
    if not await _ensure_admin(update, context):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /hypotheses BTC/USDT [timeframe]"
        )
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    timeframe = args[1] if len(args) > 1 else "1h"

    # Get cached graph and thesis from scanner
    # NOTE: _dynamic_graphs and _dynamic_theses were removed (shadow mode deleted)
    cache_key = f"{symbol}_{timeframe}"
    thesis = None  # Shadow mode removed

    if thesis is None:
        await update.message.reply_text(
            f"📭 Нет данных для {symbol} {timeframe}\n"
            f"Запустите /scan и повторите попытку."
        )
        return

    lines = [f"🧠 <b>Hypotheses: {symbol} {timeframe}</b>\n"]

    # BUY scenario
    buy = thesis.buy_scenario
    if buy.is_active and buy.thesis:
        lines.append(f"🟢 <b>BUY:</b> {buy.thesis.scenario}")
        lines.append(f"  ├ Probability: {buy.probability:.2f}")
        lines.append(f"  ├ Score: {buy.thesis.scenario_score:.1f}")
        lines.append(f"  ├ Confidence: {buy.thesis.confidence:.0f}")
        lines.append(f"  ├ Stability: {buy.stability:.2f}")
        lines.append(f"  ├ Confirmed: {len(buy.confirmed_nodes)}")
        lines.append(f"  └ Invalidated: {len(buy.invalidated_nodes)}")
    else:
        lines.append("🟢 <b>BUY:</b> no active scenario")

    lines.append("")

    # SELL scenario
    sell = thesis.sell_scenario
    if sell.is_active and sell.thesis:
        lines.append(f"🔴 <b>SELL:</b> {sell.thesis.scenario}")
        lines.append(f"  ├ Probability: {sell.probability:.2f}")
        lines.append(f"  ├ Score: {sell.thesis.scenario_score:.1f}")
        lines.append(f"  ├ Confidence: {sell.thesis.confidence:.0f}")
        lines.append(f"  ├ Stability: {sell.stability:.2f}")
        lines.append(f"  ├ Confirmed: {len(sell.confirmed_nodes)}")
        lines.append(f"  └ Invalidated: {len(sell.invalidated_nodes)}")
    else:
        lines.append("🔴 <b>SELL:</b> no active scenario")

    lines.append("")

    # Summary
    lines.append(f"📊 <b>Summary:</b>")
    lines.append(f"  ├ Ambiguous: {thesis.is_ambiguous}")
    lines.append(f"  ├ Best direction: {thesis.direction or 'NONE'}")
    lines.append(f"  ├ Overall stability: {thesis.scenario_stability:.2f}")
    lines.append(f"  ├ Updates: {thesis.update_count}")
    lines.append(f"  └ History: {len(thesis.history)} snapshots")

    # Show graph node stats
    graph = _dynamic_graphs.get(cache_key)
    if graph:
        lines.append(f"\n🌐 <b>Graph:</b>")
        lines.append(f"  ├ Nodes: {len(graph.nodes)}")
        alive = sum(1 for n in graph.nodes if n.is_alive)
        lines.append(f"  ├ Alive: {alive}")
        lines.append(f"  └ Version: {graph.graph_version}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )
