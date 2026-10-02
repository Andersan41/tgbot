import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bot.menu import (
    main_menu_keyboard,
    token_list_keyboard,
    back_keyboard,
    _normalize_symbol,
    _fmt_price,
    _calc_trend_strength,
)


class TestKeyboardStructure:
    # test_main_menu_has_ict_button / test_ict_token_list_has_custom_button removed:
    # the ICT menu (m:ict_analyze / m:ict_custom_token) was never implemented —
    # no handler or button for it exists anywhere outside this test file.

    def test_main_menu_three_rows(self):
        kb = main_menu_keyboard()
        assert len(kb.inline_keyboard) == 3, "Main menu should have 3 rows"

    def test_ict_token_list_has_back_button(self):
        kb = token_list_keyboard("ict_analyze")
        flat = []
        for row in kb.inline_keyboard:
            for btn in row:
                flat.append(btn)
        back = [b for b in flat if b.callback_data == "m:back"]
        assert len(back) == 1

    def test_analyze_token_list_unchanged(self):
        kb = token_list_keyboard("analyze")
        flat = []
        for row in kb.inline_keyboard:
            for btn in row:
                flat.append(btn)
        custom = [b for b in flat if b.callback_data == "m:custom_token"]
        assert len(custom) == 1, "Analyze token list should still have m:custom_token"


class TestNormalizeSymbol:
    def test_normal_usdt(self):
        assert _normalize_symbol("BTC") == "BTC/USDT"

    def test_with_usdt(self):
        assert _normalize_symbol("BTCUSDT") == "BTC/USDT"

    def test_with_slash(self):
        assert _normalize_symbol("ETH/USDT") == "ETH/USDT"

    def test_lowercase(self):
        assert _normalize_symbol("btc") == "BTC/USDT"

    def test_lowercase_with_usdt(self):
        assert _normalize_symbol("btcusdt") == "BTC/USDT"


class TestFmtPrice:
    def test_large_number(self):
        assert _fmt_price(67432.50) == "67,432.50"

    def test_medium_number(self):
        assert _fmt_price(100.50) == "100.5000"

    def test_small_number(self):
        assert _fmt_price(0.0567) == "0.056700"

    def test_very_small(self):
        assert _fmt_price(0.00001234) == "0.00001234"

    def test_none(self):
        assert _fmt_price(None) == "—"


class TestCalcTrendStrength:
    def test_adx_zero(self):
        assert _calc_trend_strength(0) == 0.0

    def test_adx_below_20(self):
        strength = _calc_trend_strength(10)
        assert strength == 20.0

    def test_adx_at_20(self):
        strength = _calc_trend_strength(20)
        assert strength == 40.0

    def test_adx_30(self):
        strength = _calc_trend_strength(30)
        assert strength == 60.0

    def test_adx_50(self):
        strength = _calc_trend_strength(50)
        assert strength == 100.0

    def test_adx_above_50(self):
        strength = _calc_trend_strength(60)
        assert strength == 100.0

    def test_adx_25(self):
        strength = _calc_trend_strength(25)
        assert strength == 50.0
