"""
tests/test_webhook.py — TradingView Webhook Tests
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestWebhookParsing:
    """Test webhook payload parsing."""

    def test_parse_json_payload(self):
        from web.webhook import parse_webhook_body
        body = '{"action": "buy", "symbol": "BTCUSDT", "price": 50000}'
        result = parse_webhook_body(body, "application/json")
        assert result is not None
        assert result.normalized_action == "buy"
        assert result.normalized_symbol == "BTC/USDT"
        assert result.price == 50000.0

    def test_parse_json_with_slash_symbol(self):
        from web.webhook import parse_webhook_body
        body = '{"action": "sell", "symbol": "ETH/USDT", "price": 4500}'
        result = parse_webhook_body(body, "application/json")
        assert result is not None
        assert result.normalized_action == "sell"
        assert result.normalized_symbol == "ETH/USDT"

    def test_parse_json_with_alternative_fields(self):
        from web.webhook import parse_webhook_body
        body = '{"signal": "long", "ticker": "SOLUSDT", "close": 150.5}'
        result = parse_webhook_body(body, "application/json")
        assert result is not None
        assert result.normalized_action == "buy"
        assert result.normalized_symbol == "SOL/USDT"
        assert result.price == 150.5

    def test_parse_text_payload(self):
        from web.webhook import parse_webhook_body
        body = "BUY BTCUSDT @ 50000"
        result = parse_webhook_body(body, "text/plain")
        assert result is not None
        assert result.normalized_action == "buy"
        assert result.normalized_symbol == "BTC/USDT"
        assert result.price == 50000.0

    def test_parse_text_no_price(self):
        from web.webhook import parse_webhook_body
        body = "SELL ETHUSDT"
        result = parse_webhook_body(body, "text/plain")
        assert result is not None
        assert result.normalized_action == "sell"
        assert result.normalized_symbol == "ETH/USDT"

    def test_parse_empty_body(self):
        from web.webhook import parse_webhook_body
        result = parse_webhook_body("", "application/json")
        assert result is None

    def test_parse_invalid_json(self):
        from web.webhook import parse_webhook_body
        result = parse_webhook_body("not json", "application/json")
        assert result is None

    def test_parse_unknown_action(self):
        from web.webhook import parse_webhook_body
        body = '{"action": "hold", "symbol": "BTCUSDT"}'
        result = parse_webhook_body(body, "application/json")
        assert result is not None
        assert result.normalized_action == "hold"


class TestWebhookNormalization:
    """Test symbol and action normalization."""

    def test_buy_aliases(self):
        from web.webhook import WebhookPayload
        for action in ("buy", "long", "entry_long", "BUY", "Long"):
            p = WebhookPayload(action=action, symbol="BTCUSDT")
            assert p.normalized_action == "buy", f"Failed for {action}"

    def test_sell_aliases(self):
        from web.webhook import WebhookPayload
        for action in ("sell", "short", "entry_short", "SELL", "Short"):
            p = WebhookPayload(action=action, symbol="BTCUSDT")
            assert p.normalized_action == "sell", f"Failed for {action}"

    def test_symbol_normalization(self):
        from web.webhook import WebhookPayload
        cases = [
            ("BTCUSDT", "BTC/USDT"),
            ("ETHUSDT", "ETH/USDT"),
            ("SOLUSDT", "SOL/USDT"),
            ("BTC/USDT", "BTC/USDT"),
            ("ETH/BUSD", "ETH/BUSD"),
        ]
        for input_sym, expected in cases:
            p = WebhookPayload(action="buy", symbol=input_sym)
            assert p.normalized_symbol == expected, f"Failed for {input_sym}"


class TestWebhookRateLimiter:
    """Test rate limiter."""

    def test_allows_within_limit(self):
        from web.webhook import WebhookRateLimiter
        limiter = WebhookRateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert limiter.is_allowed() is True

    def test_blocks_over_limit(self):
        from web.webhook import WebhookRateLimiter
        limiter = WebhookRateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.is_allowed()
        assert limiter.is_allowed() is False


class TestWebhookConfig:
    """Test webhook config loads correctly."""

    def test_config_defaults(self):
        from config.settings import WebhookConfig
        cfg = WebhookConfig()
        assert cfg.enabled is False
        assert cfg.rate_limit == 30

    def test_config_in_app_config(self):
        from config.settings import config
        assert hasattr(config, 'webhook')
        assert config.webhook.enabled is False
