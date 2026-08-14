"""
web/server.py — WebSocket-сервер для дашборда в браузере

aiohttp: раздача статики + WebSocket хэндлеры.
Периодически рассчитывает индикаторы и broadcast updates всем клиентам.
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Set, Dict, Any, Optional

from aiohttp import web
from loguru import logger

from config.settings import config, FILTER_TOGGLE_KEYS, _set_nested_config, _get_nested_config

STATIC_DIR = Path(__file__).parent / "public"

# Глобальное состояние
_clients: Set[web.WebSocketResponse] = set()
_client_symbols: Dict[web.WebSocketResponse, str] = {}
_broadcast_task: Optional[asyncio.Task] = None


async def _fetch_candles(symbol: str, timeframe: str, limit: int = 200):
    """Получить свечи через exchange_client (sync в executor)."""
    from data.exchange_client import exchange_client
    if exchange_client._exchange is None:
        return None
    return await exchange_client.fetch_ohlcv(symbol, timeframe, limit=limit)


def _compute_indicators(df, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
    """Рассчитать индикаторы и вернуть dict для JSON."""
    from indicators.engine import indicator_engine
    iv = indicator_engine.calculate(df, symbol, timeframe)
    if iv is None:
        return None
    return {
        "value": round(iv.close, 2),
        "rsi": round(iv.rsi, 1),
        "macd": round(iv.macd, 4),
        "macd_signal": round(iv.macd_signal, 4),
        "macd_hist": round(iv.macd_hist, 4),
        "ema_fast": round(iv.ema_fast, 2),
        "ema_slow": round(iv.ema_slow, 2),
        "ema_trend": round(iv.ema_trend, 2),
        "adx": round(iv.adx, 1),
        "dmi_plus": round(iv.dmi_plus, 1),
        "dmi_minus": round(iv.dmi_minus, 1),
        "atr": round(iv.atr, 4),
        "supertrend": round(iv.supertrend, 2),
        "supertrend_direction": iv.supertrend_direction,
        "volume": round(iv.volume, 2),
        "volume_sma": round(iv.volume_sma, 2),
        "volume_delta_pct": round(iv.volume_delta_pct, 1) if iv.volume_delta_pct is not None else None,
        "ema_bullish_cross": iv.ema_bullish_cross,
        "ema_bearish_cross": iv.ema_bearish_cross,
        "ema_bullish_alignment": iv.ema_bullish_alignment,
        "ema_bearish_alignment": iv.ema_bearish_alignment,
        "macd_bullish_cross": iv.macd_bullish_cross,
        "macd_bearish_cross": iv.macd_bearish_cross,
        "volume_above_avg": iv.volume_above_avg,
        "trend_is_strong": iv.trend_is_strong,
        "supertrend_bullish": iv.supertrend_bullish,
    }


def _compute_structure(df, symbol: str, timeframe: str) -> Dict[str, Any]:
    """Рассчитать рыночную структуру (BOS, swing points)."""
    from market_structure.structure import analyze_structure
    state = analyze_structure(df)
    return {
        "trend": state.trend if hasattr(state, "trend") else "unknown",
        "bos": {
            "type": state.last_bos.direction if state.last_bos and hasattr(state.last_bos, "direction") else "none",
            "level": round(state.last_bos.level, 2) if state.last_bos and hasattr(state.last_bos, "level") else None,
        } if state.last_bos else {"type": "none", "level": None},
        "swing_highs": [
            round(sp.price, 2)
            for sp in (state.swing_points[-5:] if state.swing_points else [])
            if hasattr(sp, "price") and hasattr(sp, "type") and sp.type == "high"
        ],
        "swing_lows": [
            round(sp.price, 2)
            for sp in (state.swing_points[-5:] if state.swing_points else [])
            if hasattr(sp, "price") and hasattr(sp, "type") and sp.type == "low"
        ],
    }


def _compute_liquidity(df, symbol: str, timeframe: str) -> Dict[str, Any]:
    """Рассчитать ликвидность (sweeps, OB, FVG)."""
    from liquidity.order_blocks import detect_order_blocks
    from liquidity.fvg import detect_fvg
    from liquidity.sweep import detect_sweeps

    obs = detect_order_blocks(df)
    fvgs = detect_fvg(df)
    sweeps = detect_sweeps(df)

    return {
        "order_blocks": [
            {"type": getattr(ob, "type", ""), "price": round(getattr(ob, "midpoint", 0), 2)}
            for ob in (obs[-3:] if obs else [])
        ],
        "fvg": [
            {"type": getattr(f, "type", ""), "top": round(getattr(f, "top", 0), 2), "bottom": round(getattr(f, "bottom", 0), 2)}
            for f in (fvgs[-3:] if fvgs else [])
        ],
        "sweep": {
            "detected": len(sweeps) > 0 if sweeps else False,
            "type": getattr(sweeps[-1], "type", "") if sweeps else "",
        } if sweeps else {"detected": False, "type": ""},
    }


def _compute_sr_levels(df, symbol: str, timeframe: str) -> Dict[str, Any]:
    """Рассчитать уровни поддержки/сопротивления."""
    from strategy.levels import get_support_resistance
    last_price = float(df["close"].iloc[-1]) if len(df) > 0 else 0
    levels = get_support_resistance(df, last_price)
    if not levels:
        return {"resistance": [], "support": []}

    resistance = [{"price": round(p, 4), "strength": "medium"} for p in levels.get("resistance", [])]
    support = [{"price": round(p, 4), "strength": "medium"} for p in levels.get("support", [])]

    return {
        "resistance": sorted(resistance, key=lambda x: x["price"])[:3],
        "support": sorted(support, key=lambda x: -x["price"])[:3],
    }


async def _build_payload(symbol: str) -> Dict[str, Any]:
    """Собрать полный payload для отправки клиенту."""
    try:
        from config.settings import config as cfg
        tf = cfg.trading.primary_timeframes[0] if cfg.trading.primary_timeframes else "1h"

        df = await _fetch_candles(symbol, tf, limit=200)
        if df is None or df.empty:
            return {
                "type": "update",
                "symbol": symbol,
                "timestamp": int(time.time() * 1000),
                "error": "No data",
            }

        indicators = _compute_indicators(df, symbol, tf) or {}
        structure = _compute_structure(df, symbol, tf)
        liquidity = _compute_liquidity(df, symbol, tf)
        sr_levels = _compute_sr_levels(df, symbol, tf)
        signal_info = _compute_signal_light(df, symbol, tf)

        # Price history for chart (последние 20 свечей)
        price_history = []
        for _, row in df.tail(20).iterrows():
            ts = row.name  # timestamp — это индекс DataFrame
            if hasattr(ts, 'timestamp'):
                ts_ms = int(ts.timestamp() * 1000)
            else:
                ts_ms = int(ts)
            price_history.append({"time": ts_ms, "close": round(float(row["close"]), 2)})

        return {
            "type": "update",
            "symbol": symbol,
            "timestamp": int(time.time() * 1000),
            "price": indicators.get("value", 0),
            "indicators": indicators,
            "structure": structure,
            "liquidity": liquidity,
            "levels": sr_levels,
            "signal": signal_info,
            "priceHistory": price_history,
        }
    except Exception as e:
        import traceback
        logger.error(f"Web payload error for {symbol}: {e}\n{traceback.format_exc()}")
        return {
            "type": "update",
            "symbol": symbol,
            "timestamp": int(time.time() * 1000),
            "error": str(e),
        }


def _compute_signal_light(df, symbol: str, timeframe: str) -> Dict[str, Any]:
    """Упрощённый сигнал для дашборда (indicator-only heuristic)."""
    from indicators.engine import indicator_engine
    iv = indicator_engine.calculate(df, symbol, timeframe)
    if iv is None:
        return {"signal": "NO_SIGNAL", "score": 0, "reasons": []}

    reasons = []
    score = 0

    if iv.ema_fast > iv.ema_slow:
        score += 1
        reasons.append("EMA fast > slow")
    elif iv.ema_fast < iv.ema_slow:
        score -= 1

    if iv.rsi > 55:
        score += 1
        reasons.append(f"RSI {iv.rsi:.0f} > 55")
    elif iv.rsi < 45:
        score -= 1

    if iv.macd_hist > 0:
        score += 1
        reasons.append("MACD hist > 0")
    elif iv.macd_hist < 0:
        score -= 1

    if iv.adx > 20:
        if iv.dmi_plus > iv.dmi_minus:
            score += 1
            reasons.append("ADX+ > ADX-")
        else:
            score -= 1

    if score >= 2:
        signal = "BUY"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "NO_SIGNAL"

    return {
        "signal": signal,
        "score": score,
        "verdict": "СИЛЬНЫЙ" if abs(score) >= 3 else "УМЕРЕННЫЙ" if abs(score) == 2 else "СЛАБЫЙ",
        "confidence": round(abs(score) / 4 * 100, 1),
        "reasons": reasons[:5],
        "entry": round(iv.close, 2),
        "sl": None,
        "tp": None,
        "regime": None,
    }


async def _broadcast_loop():
    """Фоновая задача: рассылает обновления каждые N секунд."""
    from storage.database import db
    while True:
        try:
            if _clients:
                # Собрать уникальные символы
                symbols = set(_client_symbols.values())
                if not symbols:
                    symbols = {config.trading.symbols[0]} if config.trading.symbols else {"BTC/USDT"}

                for symbol in symbols:
                    payload = await _build_payload(symbol)
                    message = json.dumps(payload, default=str)

                    # Отправить только клиентам, подписанным на этот символ
                    stale = set()
                    for ws in _clients:
                        if _client_symbols.get(ws) == symbol:
                            try:
                                await ws.send_str(message)
                            except Exception:
                                stale.add(ws)
                    _clients.difference_update(stale)

                # Рассылка открытых сделок всем клиентам
                try:
                    trades = await db.get_open_trades_with_signals()
                    trades_msg = json.dumps({"type": "open_trades", "trades": trades}, default=str)
                    stale = set()
                    for ws in _clients:
                        try:
                            await ws.send_str(trades_msg)
                        except Exception:
                            stale.add(ws)
                    _clients.difference_update(stale)
                except Exception as e:
                    logger.warning(f"Open trades broadcast error: {e}")

            await asyncio.sleep(config.web.update_interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Broadcast loop error: {e}")
            await asyncio.sleep(5)


# ─── HTTP handlers ──────────────────────────────────────────────────────

async def index_handler(request):
    """Отдаём index.html."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Dashboard not built yet", status=404)


# ─── WebSocket handler ──────────────────────────────────────────────────

async def ws_handler(request):
    """Обработка WebSocket подключений."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Подписываем на символ по умолчанию
    default_symbol = config.trading.symbols[0] if config.trading.symbols else "BTC/USDT"
    _clients.add(ws)
    _client_symbols[ws] = default_symbol

    logger.info(f"WS client connected ({len(_clients)} total), default: {default_symbol}")

    try:
        # Отправить текущее состояние сразу
        payload = await _build_payload(default_symbol)
        await ws.send_json(payload)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "subscribe" and data.get("symbol"):
                        new_symbol = data["symbol"].upper()
                        # Конвертируем BTC → BTC/USDT
                        if "/" not in new_symbol:
                            new_symbol = new_symbol + "/USDT"
                        _client_symbols[ws] = new_symbol
                        logger.info(f"WS client subscribed to {new_symbol}")
                        # Отправить данные нового символа
                        payload = await _build_payload(new_symbol)
                        await ws.send_json(payload)
                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.warning(f"WS error: {ws.exception()}")
    finally:
        _clients.discard(ws)
        _client_symbols.pop(ws, None)
        logger.info(f"WS client disconnected ({len(_clients)} remaining)")

    return ws


# ─── Filter API ────────────────────────────────────────────────────────

# Human-readable names for each filter toggle
FILTER_LABELS: dict[str, str] = {
    "adx_filter": "ADX",
    "ema_alignment": "EMA Align",
    "ema_spread": "EMA Spread",
    "trigger": "Trigger",
    "candle_close": "Candle Close",
    "min_score": "Min Score",
    "compression": "Compression",
    "confirm_tf": "Confirm TF",
    "ema_slope": "EMA Slope",
    "macd_slope": "MACD Slope",
    "mtf": "MTF",
    "distance_filter": "Distance",
    "sr_levels": "S/R Levels",
    "tp_path": "TP Path",
    "btc_corr": "BTC Corr",
    "eth_corr": "ETH Corr",
    "volatility": "Volatility",
    "no_trade_zones": "No-Trade",
    "dynamic_risk": "Dyn Risk",
    "context": "Context",
    "confidence_v2": "Confidence V2",
    "signal_block": "Block Notify",
}


_HIDDEN_FILTERS: set[str] = {"ema_slope", "macd_slope", "tp_path"}


async def api_filters_get(request):
    """GET /api/filters — вернуть текущее состояние всех фильтров.

    Скрытые (отключённые по умолчанию) фильтры не отдаются в UI.
    """
    filters = []
    for key, (attr_path, _) in FILTER_TOGGLE_KEYS.items():
        if key in _HIDDEN_FILTERS:
            continue
        try:
            current = _get_nested_config(config, attr_path)
        except AttributeError:
            current = True
        filters.append({
            "key": key,
            "label": FILTER_LABELS.get(key, key),
            "enabled": bool(current),
        })
    return web.json_response({"filters": filters})


async def api_filters_post(request):
    """POST /api/filters — переключить фильтр.

    Body: {"key": "adx_filter", "enabled": true}
    """
    from storage.database import db
    from config.settings import reload_filter_toggles

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = data.get("key")
    enabled = data.get("enabled")

    if key is None or enabled is None or key not in FILTER_TOGGLE_KEYS:
        return web.json_response({"error": f"Invalid filter key: {key}"}, status=400)

    attr_path, _ = FILTER_TOGGLE_KEYS[key]
    _set_nested_config(config, attr_path, bool(enabled))
    await db.set_setting(f"filter:toggle:{key}", str(bool(enabled)).lower())

    return web.json_response({"ok": True, "key": key, "enabled": bool(enabled)})


async def api_open_trades(request):
    """GET /api/open-trades — вернуть список открытых сделок."""
    from storage.database import db
    try:
        trades = await db.get_open_trades_with_signals()
        return web.json_response({"trades": trades})
    except Exception as e:
        logger.error(f"api_open_trades error: {e}")
        return web.json_response({"trades": [], "error": str(e)})


# ─── App factory ────────────────────────────────────────────────────────

def create_app() -> web.Application:
    """Создаём aiohttp приложение."""

    @web.middleware
    async def no_cache_middleware(request, handler):
        response = await handler(request)
        if not request.path.startswith("/api/") and not request.path.startswith("/webhook/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    app = web.Application(middlewares=[no_cache_middleware])

    # API
    app.router.add_get("/api/filters", api_filters_get)
    app.router.add_post("/api/filters", api_filters_post)
    app.router.add_get("/api/open-trades", api_open_trades)

    # Webhook (TradingView)
    from web.webhook import webhook_handler
    app.router.add_post("/webhook/tradingview", webhook_handler)

    # WebSocket
    app.router.add_get("/ws", ws_handler)

    # Статика
    if STATIC_DIR.exists():
        app.router.add_static("/css/", STATIC_DIR / "css", show_index=False)
        app.router.add_static("/js/", STATIC_DIR / "js", show_index=False)

    # Index
    app.router.add_get("/", index_handler)
    app.router.add_get("/{path:.*}", index_handler)

    return app


async def start_web_server():
    """Запуск веб-сервера (вызывается из main.py)."""
    global _broadcast_task

    if not config.web.enabled:
        logger.info("Web server disabled (WEB_ENABLED=false)")
        return

    app = create_app()

    # Запускаем broadcast loop
    _broadcast_task = asyncio.create_task(_broadcast_loop())

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.web.host, config.web.port)
    await site.start()

    logger.info(f"Web dashboard: http://{config.web.host}:{config.web.port}")

    # Не блокируем — возвращаем runner для shutdown
    return runner
