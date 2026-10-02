import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _MockDB:
    """Заглушка, заменяющая Database с in-memory хранилищем."""
    def __init__(self, store):
        self._store = store

    async def get_setting(self, key: str, default: str = "") -> str:
        return self._store.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        self._store[key] = value

    async def get_dynamic_symbols(self):
        val = self._store.get("dynamic_symbols", "")
        if not val:
            return None
        return [s.strip() for s in val.split(",") if s.strip()]

    async def set_dynamic_symbols(self, symbols: list[str]) -> None:
        await self.set_setting("dynamic_symbols", ",".join(symbols))

    async def get_disabled_symbols(self):
        val = self._store.get("disabled_symbols", "")
        if not val:
            return None
        return [s.strip() for s in val.split(",") if s.strip()]

    async def set_disabled_symbols(self, symbols: list[str]) -> None:
        await self.set_setting("disabled_symbols", ",".join(symbols))


@pytest.fixture
def mock_db(monkeypatch):
    """Возвращает dict-хранилище и патит bot.admin.db на _MockDB."""
    store: dict[str, str] = {}
    mock_obj = _MockDB(store)
    monkeypatch.setattr("bot.admin.db", mock_obj)
    # Также патсим storage.database.db для тестов, которые читают напрямую
    monkeypatch.setattr("storage.database.db", mock_obj)
    return store


@pytest.fixture
def mock_db_obj(monkeypatch):
    """Возвращает сам _MockDB объект для вызова методов."""
    store: dict[str, str] = {}
    mock_obj = _MockDB(store)
    monkeypatch.setattr("bot.admin.db", mock_obj)
    monkeypatch.setattr("storage.database.db", mock_obj)
    monkeypatch.setattr("scheduler.scanner.db", mock_obj)
    return mock_obj


@pytest.fixture
def mock_admin_user():
    user = MagicMock()
    user.id = 111111
    return user


@pytest.fixture
def mock_regular_user():
    user = MagicMock()
    user.id = 999999
    return user


@pytest.fixture
def mock_update(mock_admin_user):
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.effective_user = mock_admin_user
    update = MagicMock()
    update.message = msg
    update.effective_user = mock_admin_user
    update.callback_query = None
    return update


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.args = []
    return ctx


# ── F4.1: /setparam ──────────────────────────────────────────────────


class TestSetparam:
    @pytest.mark.asyncio
    async def test_setparam_success(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["EMA_FAST", "7"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        assert mock_db.get("param:EMA_FAST") == "7"
        reply = mock_update.message.reply_text.call_args[0][0]
        assert "EMA_FAST=7" in reply

    @pytest.mark.asyncio
    async def test_setparam_float_value(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["RSI_OVERBOUGHT", "75.5"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        assert mock_db.get("param:RSI_OVERBOUGHT") == "75.5"

    @pytest.mark.asyncio
    async def test_setparam_non_whitelisted(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["INVALID_PARAM", "10"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        reply = mock_update.message.reply_text.call_args[0][0]
        assert "\u274c" in reply or "whitelisted" in reply.lower() or "не whitelisted" in reply

    @pytest.mark.asyncio
    async def test_setparam_invalid_type(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["EMA_FAST", "not_a_number"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        reply = mock_update.message.reply_text.call_args[0][0]
        assert "\u274c" in reply or "не приводится" in reply

    @pytest.mark.asyncio
    async def test_setparam_wrong_arg_count(self, mock_update, mock_context, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["EMA_FAST"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "Usage" in call_args

    @pytest.mark.asyncio
    async def test_setparam_denies_non_admin(self, mock_update, mock_context, mock_regular_user, monkeypatch):
        mock_update.effective_user = mock_regular_user
        mock_context.args = ["EMA_FAST", "7"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u26d4" in call_args or "Доступ" in call_args

    @pytest.mark.asyncio
    async def test_setparam_uppercases_name(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["ema_slow", "30"]
        from bot.admin import setparam_command
        await setparam_command(mock_update, mock_context)

        assert mock_db.get("param:EMA_SLOW") == "30"


# ── F4.2: /disable / /enable ─────────────────────────────────────────


class TestDisableEnable:
    @pytest.mark.asyncio
    async def test_disable_success(self, mock_update, mock_context, mock_db_obj, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["DOGE/USDT"]
        from bot.admin import disable_command
        await disable_command(mock_update, mock_context)

        disabled = await mock_db_obj.get_disabled_symbols() or []
        assert "DOGE/USDT" in disabled

    @pytest.mark.asyncio
    async def test_disable_no_slash(self, mock_update, mock_context, mock_db_obj, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["SOL"]
        from bot.admin import disable_command
        await disable_command(mock_update, mock_context)

        disabled = await mock_db_obj.get_disabled_symbols() or []
        assert "SOL/USDT" in disabled

    @pytest.mark.asyncio
    async def test_disable_already_disabled(self, mock_update, mock_context, mock_db, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])
        mock_db["disabled_symbols"] = "BTC/USDT"

        mock_context.args = ["BTC/USDT"]
        from bot.admin import disable_command
        await disable_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u0423\u0436\u0435" in call_args or "\u0432\u044b\u043a\u043b\u044e\u0447\u0451\u043d" in call_args

    @pytest.mark.asyncio
    async def test_enable_success(self, mock_update, mock_context, mock_db_obj, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])
        await mock_db_obj.set_disabled_symbols(["BTC/USDT", "ETH/USDT"])

        mock_context.args = ["ETH/USDT"]
        from bot.admin import enable_command
        await enable_command(mock_update, mock_context)

        disabled = await mock_db_obj.get_disabled_symbols() or []
        assert "ETH/USDT" not in disabled
        assert "BTC/USDT" in disabled

    @pytest.mark.asyncio
    async def test_enable_not_disabled(self, mock_update, mock_context, mock_db_obj, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])

        mock_context.args = ["BTC/USDT"]
        from bot.admin import enable_command
        await enable_command(mock_update, mock_context)

        # Should still succeed (just no-op on the list)
        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u0432\u043a\u043b\u044e\u0447\u0451\u043d" in call_args

    @pytest.mark.asyncio
    async def test_disable_denies_non_admin(self, mock_update, mock_context, mock_regular_user, monkeypatch):
        mock_update.effective_user = mock_regular_user
        mock_context.args = ["BTC/USDT"]
        from bot.admin import disable_command
        await disable_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u26d4" in call_args or "Доступ" in call_args

    @pytest.mark.asyncio
    async def test_enable_denies_non_admin(self, mock_update, mock_context, mock_regular_user, monkeypatch):
        mock_update.effective_user = mock_regular_user
        mock_context.args = ["BTC/USDT"]
        from bot.admin import enable_command
        await enable_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u26d4" in call_args or "Доступ" in call_args


# ── F4.3: /exportdb ──────────────────────────────────────────────────


class TestExportdb:
    @pytest.mark.asyncio
    async def test_exportdb_denies_non_admin(self, mock_update, mock_context, mock_regular_user, monkeypatch):
        mock_update.effective_user = mock_regular_user
        from bot.admin import exportdb_command
        await exportdb_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u26d4" in call_args or "Доступ" in call_args

    @pytest.mark.asyncio
    async def test_exportdb_file_too_large(self, mock_update, mock_context, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])
        monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///./data/signals.db")

        with patch("pathlib.Path.exists", return_value=True), \
             patch("os.path.getsize", return_value=50 * 1024 * 1024):
            from bot.admin import exportdb_command
            await exportdb_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u274c" in call_args or "большой" in call_args or "50" in call_args

    @pytest.mark.asyncio
    async def test_exportdb_file_not_found(self, mock_update, mock_context, monkeypatch):
        from config.settings import config
        monkeypatch.setattr(config.telegram, "admin_ids", [111111])
        monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///./nonexistent.db")

        with patch("pathlib.Path.exists", return_value=False):
            from bot.admin import exportdb_command
            await exportdb_command(mock_update, mock_context)

        call_args = mock_update.message.reply_text.call_args[0][0]
        assert "\u274c" in call_args or "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d" in call_args


# ── DB: get_disabled_symbols / set_disabled_symbols ───────────────────


class TestDatabaseDisabledSymbols:
    @pytest.mark.asyncio
    async def test_get_disabled_symbols_empty(self, mock_db_obj):
        result = await mock_db_obj.get_disabled_symbols()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_disabled_symbols(self, mock_db_obj):
        await mock_db_obj.set_disabled_symbols(["BTC/USDT", "ETH/USDT"])
        result = await mock_db_obj.get_disabled_symbols()
        assert result == ["BTC/USDT", "ETH/USDT"]

    @pytest.mark.asyncio
    async def test_set_disabled_symbols_single(self, mock_db_obj):
        await mock_db_obj.set_disabled_symbols(["SOL/USDT"])
        result = await mock_db_obj.get_disabled_symbols()
        assert result == ["SOL/USDT"]


# ── Scanner: disabled symbols filter ─────────────────────────────────


class TestScannerDisabledFilter:
    @pytest.mark.asyncio
    async def test_scan_skips_disabled(self, mock_db_obj, monkeypatch, sample_ohlcv):
        """Disabled symbols should be filtered out before scan tasks are created."""
        await mock_db_obj.set_disabled_symbols(["ETH/USDT"])

        import config.settings as settings
        monkeypatch.setenv("SYMBOLS", "BTC/USDT,ETH/USDT")
        import importlib
        importlib.reload(settings)

        from scheduler.scanner import run_scan_cycle

        # Mock scan_symbol to track which symbols were scanned
        scanned = []
        async def mock_scan(symbol, tf, cb, blocked_cb=None, funnel=None):
            scanned.append((symbol, tf))
            return None

        with patch("scheduler.scanner.scan_symbol_v2", mock_scan):
            await run_scan_cycle(lambda *a, **k: None)

        # ETH/USDT should NOT appear in scanned symbols
        for symbol, _ in scanned:
            assert symbol != "ETH/USDT", "ETH/USDT should have been filtered out"
        assert any(s == "BTC/USDT" for s, _ in scanned), "BTC/USDT should be scanned"

    @pytest.mark.asyncio
    async def test_scan_all_when_no_disabled(self, mock_db_obj, monkeypatch, sample_ohlcv):
        """Когда disabled_symbols пустой — сканируются все символы."""
        import config.settings as settings
        monkeypatch.setenv("SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT")
        import importlib
        importlib.reload(settings)

        # disabled_symbols не установлен
        disabled = await mock_db_obj.get_disabled_symbols()
        assert disabled is None

        from scheduler.scanner import run_scan_cycle

        scanned = []
        async def mock_scan(symbol, tf, cb, blocked_cb=None, funnel=None):
            scanned.append((symbol, tf))
            return None

        with patch("scheduler.scanner.scan_symbol_v2", mock_scan):
            await run_scan_cycle(lambda *a, **k: None)

        symbols_scanned = set(s for s, _ in scanned)
        assert "BTC/USDT" in symbols_scanned
        assert "ETH/USDT" in symbols_scanned
        assert "SOL/USDT" in symbols_scanned
