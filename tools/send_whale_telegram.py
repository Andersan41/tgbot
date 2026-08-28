"""
Отправить тестовый whale-сигнал в Telegram канал.

Использование:
  python tools/send_whale_telegram.py                    # T3 BUY INIT
  python tools/send_whale_telegram.py 3 sell ABS         # T3 SELL ABS
  python tools/send_whale_telegram.py 2 buy INIT 2       # 2x T2 BUY INIT
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import config
from bot.notifier import get_bot
from telegram.constants import ParseMode


def _format_whale_message(tier: int, direction: str, classification: str, count: int = 1) -> str:
    """Форматировать whale-сигнал как Telegram сообщение."""
    emoji_dir = "🟢" if direction == "buy" else "🔴"
    dir_word = "ПОКУПКА" if direction == "buy" else "ПРОДАЖА"
    tier_emoji = {1: "🐋", 2: "🐳", 3: "🦈"}.get(tier, "🐟")
    class_emoji = "⚡" if classification == "INIT" else "🛡️"
    class_word = "Инициатива" if classification == "INIT" else "Поглощение"

    z_base = {1: 2.5, 2: 3.5, 3: 5.0}.get(tier, 5.0)
    vol_base = {1: 80000, 2: 150000, 3: 300000}.get(tier, 50000)

    now_ms = int(time.time() * 1000)

    lines = [
        f"{tier_emoji} <b>КИТ ОБНАРУЖЕН</b> — T{tier} {class_emoji}",
        f"",
        f"{emoji_dir} <b>{dir_word}</b> — BTC/USDT",
        f"Классификация: {class_word} ({classification})",
        f"",
        f"📊 <b>Метрики:</b>",
        f"├ Z-Score: <code>{z_base:.1f}</code> (порог T{tier})",
        f"├ Объём: <code>${vol_base:,.0f}</code>",
        f"├ SMA объёма: <code>${vol_base / 3:,.0f}</code>",
        f"└ Множитель: <code>{vol_base / (vol_base / 3):.1f}x</code>",
        f"",
    ]

    if count > 1:
        lines.append(f"📡 <b>Сигналов:</b> {count} шт")
        for i in range(min(count, 5)):
            d = direction if i % 2 == 0 else ("sell" if direction == "buy" else "buy")
            c = classification if i % 2 == 0 else ("ABS" if classification == "INIT" else "INIT")
            d_emoji = "🟢" if d == "buy" else "🔴"
            c_emoji = "⚡" if c == "INIT" else "🛡️"
            lines.append(f"  {i+1}. {d_emoji} {d.upper()} {c_emoji} Z={z_base + i * 0.3:.1f}")
        lines.append("")

    lines.extend([
        f"⚠️ <b>Рекомендация:</b>",
        f"{'Ждать подтверждения' if classification == 'INIT' else 'Возможен разворот'}",
        f"",
        f"🕐 <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC</code>",
    ])

    return "\n".join(lines)


async def send(tier: int = 3, direction: str = "buy", classification: str = "INIT", count: int = 1):
    """Отправить whale-сигнал в Telegram канал."""
    if not config.telegram.token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    if not config.telegram.channel_id:
        print("ERROR: TELEGRAM_CHANNEL_ID not set")
        return

    bot = get_bot()
    text = _format_whale_message(tier, direction, classification, count)

    try:
        await bot.send_message(
            chat_id=config.telegram.channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        print(f"OK: Whale T{tier} {direction.upper()} {classification} sent to {config.telegram.channel_id}")
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    tier = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    direction = sys.argv[2] if len(sys.argv) > 2 else "buy"
    classification = sys.argv[3] if len(sys.argv) > 3 else "INIT"
    count = int(sys.argv[4]) if len(sys.argv) > 4 else 1

    print(f"Sending {count}x T{tier} {direction.upper()} {classification}...")
    asyncio.run(send(tier, direction, classification, count))
