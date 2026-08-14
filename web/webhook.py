"""
web/webhook.py — TradingView Webhook Handler

Accepts incoming webhook alerts from TradingView and processes them
through the ICT pipeline (Pattern Engine → Risk Engine → Telegram).

Supported formats:
- JSON: {"action": "buy", "symbol": "BTCUSDT", "price": 50000, "interval": "60"}
- TEXT: "BUY BTCUSDT @ 50000" (plain text alerts)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web
from loguru import logger


@dataclass
class WebhookPayload:
    """Parsed TradingView webhook payload."""
    action: str  # "buy" / "sell" / "long" / "short"
    symbol: str  # "BTCUSDT" or "BTC/USDT"
    price: float = 0.0
    interval: str = ""
    message: str = ""
    raw: str = ""

    @property
    def normalized_action(self) -> str:
        """Normalize action to 'buy' or 'sell'."""
        action = self.action.lower().strip()
        if action in ("buy", "long", "entry_long"):
            return "buy"
        elif action in ("sell", "short", "entry_short"):
            return "sell"
        return action

    @property
    def normalized_symbol(self) -> str:
        """Normalize symbol to CCXT format (BTC/USDT)."""
        s = self.symbol.upper().strip()
        # Already has slash
        if "/" in s:
            return s
        # Add slash before USDT, BUSD, etc.
        for quote in ("USDT", "BUSD", "USD", "BTC", "ETH"):
            if s.endswith(quote) and len(s) > len(quote):
                return f"{s[:-len(quote)]}/{quote}"
        return s


@dataclass
class WebhookRateLimiter:
    """Simple in-memory rate limiter for webhook endpoint."""
    max_requests: int = 30
    window_seconds: int = 60
    _requests: list[float] = field(default_factory=list)

    def is_allowed(self) -> bool:
        """Check if a new request is allowed within rate limit."""
        now = time.time()
        # Remove old requests outside the window
        self._requests = [t for t in self._requests if now - t < self.window_seconds]
        if len(self._requests) >= self.max_requests:
            return False
        self._requests.append(now)
        return True


def parse_webhook_body(body: str, content_type: str = "application/json") -> Optional[WebhookPayload]:
    """Parse TradingView webhook body into WebhookPayload.

    Supports:
    - JSON format: {"action": "buy", "symbol": "BTCUSDT", ...}
    - Text format: "BUY BTCUSDT @ 50000"
    """
    import json

    body = body.strip()
    if not body:
        return None

    # Try JSON first
    if "json" in content_type or body.startswith("{"):
        try:
            data = json.loads(body)
            return WebhookPayload(
                action=str(data.get("action", data.get("signal", data.get("side", "")))),
                symbol=str(data.get("symbol", data.get("ticker", data.get("pair", "")))),
                price=float(data.get("price", data.get("close", 0))),
                interval=str(data.get("interval", data.get("tf", data.get("timeframe", "")))),
                message=str(data.get("message", data.get("msg", ""))),
                raw=body,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"JSON parse failed, trying text format: {e}")

    # Try text format: "BUY BTCUSDT @ 50000" or "SELL ETH/USDT 4500"
    text_pattern = r'^(BUY|SELL|LONG|SHORT)\s+(\S+)\s*(?:@\s*|:\s*|price\s+)?(\d+\.?\d*)?$'
    match = re.match(text_pattern, body, re.IGNORECASE)
    if match:
        return WebhookPayload(
            action=match.group(1),
            symbol=match.group(2),
            price=float(match.group(3)) if match.group(3) else 0.0,
            raw=body,
        )

    # Try simpler format: "BUY BTCUSDT" (no price)
    simple_pattern = r'^(BUY|SELL|LONG|SHORT)\s+(\S+)$'
    match = re.match(simple_pattern, body, re.IGNORECASE)
    if match:
        return WebhookPayload(
            action=match.group(1),
            symbol=match.group(2),
            raw=body,
        )

    logger.warning(f"Failed to parse webhook body: {body[:100]}")
    return None


async def process_webhook_signal(payload: WebhookPayload) -> dict:
    """Process a parsed webhook signal through the ICT pipeline.

    Returns dict with status and details.
    """
    from config.settings import config

    action = payload.normalized_action
    symbol = payload.normalized_symbol

    # Validate action
    if action not in ("buy", "sell"):
        return {"status": "rejected", "reason": f"unknown action: {payload.action}"}

    # Validate symbol is in our scan list
    from config.settings import get_active_symbols
    active_symbols = get_active_symbols()
    if symbol not in active_symbols:
        return {
            "status": "rejected",
            "reason": f"symbol {symbol} not in active list: {active_symbols}",
        }

    logger.info(f"Webhook received: {action.upper()} {symbol} @ {payload.price}")

    # Run through ICT pipeline
    try:
        from strategy.pattern_engine import pattern_engine
        from data.exchange_client import exchange_client
        from indicators.engine import indicator_engine
        from liquidity.sweep import detect_sweeps
        from liquidity.order_blocks import detect_order_blocks
        from liquidity.fvg import detect_fvg
        from liquidity.candle_quality import analyze_last_candle
        from market_structure.structure import analyze_structure
        from risk.engine import risk_engine, PortfolioState
        from strategy.signal_engine import SignalType, SignalResult, _calculate_sl_tp
        from storage.database import db

        # Fetch OHLCV data
        timeframe = payload.interval or "1h"
        tf_map = {"60": "1h", "240": "4h", "1D": "1d", "1W": "1w"}
        timeframe = tf_map.get(timeframe, timeframe)

        df = await exchange_client.fetch_ohlcv(symbol, timeframe, limit=200)
        if df is None or len(df) < 20:
            return {"status": "error", "reason": "insufficient OHLCV data"}

        # Calculate indicators
        ind = indicator_engine.calculate(df, symbol, timeframe)
        if ind is None:
            return {"status": "error", "reason": "indicator calculation failed"}

        # Pattern detection
        _df_clean = df.dropna(subset=["open", "high", "low", "close", "volume"])
        sweeps = detect_sweeps(_df_clean, lookback=50)
        order_blocks = detect_order_blocks(_df_clean, lookback=100)
        fvgs = detect_fvg(_df_clean, lookback=100)
        candle_quality = analyze_last_candle(_df_clean, atr_value=ind.atr)

        # Compute displacement for MSS
        _disp_atr = 0.0
        _reclaim = 0
        if candle_quality and ind.atr and ind.atr > 0:
            _disp_atr = candle_quality.body_atr_ratio if hasattr(candle_quality, 'body_atr_ratio') else 0.0
        if sweeps:
            _valid_sw = [s for s in sweeps if s.is_valid]
            if _valid_sw:
                _reclaim = _valid_sw[0].reclaim_candles

        structure = analyze_structure(
            _df_clean, lookback=50,
            sweeps=sweeps,
            displacement_atr=_disp_atr,
            reclaim_bars=_reclaim,
            atr_value=ind.atr if ind.atr else 0.0,
        )

        setup = pattern_engine.detect(
            sweeps=sweeps,
            order_blocks=order_blocks,
            structure=structure,
            fvgs=fvgs,
            candle_quality=candle_quality,
            current_price=ind.close,
            atr=ind.atr if ind.atr else 0.0,
        )

        if not setup.detected:
            return {
                "status": "rejected",
                "reason": f"no ICT setup: {setup.rejection_reason}",
            }

        # Validate direction matches webhook action
        if setup.direction != action:
            return {
                "status": "rejected",
                "reason": f"direction mismatch: webhook={action}, setup={setup.direction}",
            }

        # Calculate SL/TP
        entry_price = payload.price if payload.price > 0 else ind.close
        sl, tp, sl_source = _calculate_sl_tp(
            ind=ind,
            signal=SignalType.BUY if action == "buy" else SignalType.SELL,
            structure=structure,
            entry=entry_price,
            timeframe=timeframe,
            order_blocks=order_blocks,
            fvgs=fvgs,
            df=_df_clean,
        )

        # Risk evaluation
        from risk.engine import PortfolioState
        active_count = await db.get_active_signals_count()
        portfolio_risk = await db.get_portfolio_risk_sum()

        portfolio = PortfolioState(
            active_count=active_count,
            total_risk_pct=portfolio_risk,
            max_active_signals=config.max_active_signals,
            max_portfolio_risk_pct=config.max_portfolio_risk_pct,
        )

        # Simple inline probability
        _components_score = setup.components_count
        p_tp = 0.45
        if _components_score >= 4:
            p_tp += 0.15
        elif _components_score >= 3:
            p_tp += 0.08
        if setup.mss_score > 70:
            p_tp += 0.05
        p_tp = max(0.15, min(0.85, p_tp))

        atr_pct = (ind.atr / ind.close * 100) if ind.atr and ind.close > 0 else 0.0

        risk_decision = risk_engine.evaluate(
            portfolio=portfolio,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            atr_pct=atr_pct,
            p_tp=p_tp,
            confidence=min(0.85, p_tp),
            mss_quality=setup.mss_score,
            atr=ind.atr if ind.atr else 0.0,
            sl_source=sl_source,
        )

        if not risk_decision.should_trade:
            return {
                "status": "rejected",
                "reason": f"risk engine: {risk_decision.rejection_reason}",
            }

        # Build signal result
        signal_type = SignalType.BUY if action == "buy" else SignalType.SELL
        reasons = [
            f"webhook: {payload.action}",
            f"components={setup.components_count}",
            f"P(TP)={p_tp:.1%}",
        ]

        result = SignalResult(
            signal=signal_type,
            symbol=symbol,
            timeframe=timeframe,
            close=ind.close,
            entry_price=entry_price,
            sl=risk_decision.sl_price,
            tp=risk_decision.tp_price,
            reasons=reasons,
            score=setup.components_count,
            _sl_source=sl_source,
        )

        # Save to DB
        saved_signal = await db.save_signal(
            symbol=result.symbol,
            timeframe=result.timeframe,
            signal_type=result.signal.value,
            close_price=result.close,
            sl=result.sl,
            tp=result.tp,
            score=result.score,
            reasons=result.reasons,
            confirmed=False,
            factor_fingerprint=f"webhook|components={setup.components_count}",
            confidence_v2_pct=p_tp * 100,
            confidence_v2_factors=[],
            entry_price_source="WEBHOOK",
            signal_detected_at=time.time(),
        )

        logger.info(
            f"Webhook signal processed: {result.signal.value} {symbol} {timeframe} | "
            f"SL={result.sl} TP={result.tp} | Risk={risk_decision.risk_pct:.2f}%"
        )

        return {
            "status": "accepted",
            "signal_id": saved_signal.id,
            "signal": result.signal.value,
            "symbol": symbol,
            "timeframe": timeframe,
            "entry": entry_price,
            "sl": result.sl,
            "tp": result.tp,
            "risk_pct": risk_decision.risk_pct,
        }

    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}


# Rate limiter singleton
_rate_limiter = WebhookRateLimiter()


async def webhook_handler(request: web.Request) -> web.Response:
    """POST /webhook/tradingview — accept TradingView webhook alerts."""
    from config.settings import config

    # Check if webhook is enabled
    if not config.webhook.enabled:
        return web.json_response(
            {"status": "disabled", "reason": "webhook endpoint is disabled"},
            status=503,
        )

    # Rate limiting
    if not _rate_limiter.is_allowed():
        logger.warning("Webhook rate limit exceeded")
        return web.json_response(
            {"status": "rate_limited", "reason": "too many requests"},
            status=429,
        )

    # Read body
    try:
        body = await request.text()
    except Exception as e:
        return web.json_response(
            {"status": "error", "reason": f"failed to read body: {e}"},
            status=400,
        )

    # Parse content type
    content_type = request.content_type or "application/json"

    # Parse payload
    payload = parse_webhook_body(body, content_type)
    if payload is None:
        return web.json_response(
            {"status": "error", "reason": "failed to parse webhook body"},
            status=400,
        )

    # Process signal
    result = await process_webhook_signal(payload)

    # Return appropriate status code
    status_code = 200
    if result.get("status") == "rejected":
        status_code = 422
    elif result.get("status") == "error":
        status_code = 500
    elif result.get("status") == "rate_limited":
        status_code = 429
    elif result.get("status") == "disabled":
        status_code = 503

    return web.json_response(result, status=status_code)
