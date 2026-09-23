# bot_fix_v2.0.md — Senior model audit

## Date: 2026-09-21
## Auditor: Codex (GPT-6)
## Branch: tgbot-claude

Репозиторий: `Andersan41/tgbot`. Проверенный commit: `9d0da045c617917e210e2ff802a2602a5068cfa1`.
В исходном prompt осталась ссылка на `Andersan41/telebot` / `main`; здесь применяется явное указание пользователя — `tgbot/tgbot-claude`. Прочитана последняя версия prompt, включая добавленные БД/логи и пересмотр WR. Результат — аудит и точные предложения кода для mimo; рабочие модули бота в ходе аудита не изменены.

---

## I. Executive summary

Приоритет — восстановить корректность планов, виртуальных исходов и контроля риска. В исторической БД девять TP находятся с убыточной стороны входа; такие сделки закрыты как HIT_TP с отрицательным PnL. Текущий RiskEngine использует абсолютные расстояния и не проверяет направленную геометрию. FVG может подменить фактическую цену входа ещё не исполненной лимитной ценой. Проверки портфеля не образуют атомарную операцию с сохранением: новый риск не учитывается при допуске, параллельные запросы проходят по одному старому состоянию. Логи показывают уже превышенный cap: 3.4% и 3.8% при лимите 3%.

Исходные цифры нельзя использовать как достоверное измерение доходности стратегии. На 139 закрытых записях — 36 положительных, 91 отрицательная и 12 нулевых; WR=25.90% всех закрытых либо 28.35% ненулевых, PF ценового PnL=0.5846. Сумма −126.1245 п.п. ценовых доходностей не равна потере 126% капитала. Медиана планового абсолютного RR=2.49, а отношение условных средних HIT_TP/HIT_SL не доказывает RR=1:1. BUY положителен только в невзвешенной ценовой сумме; proxy с учётом риска отрицателен и для BUY. Данные смешивают периоды и реализации, не содержат setup/version snapshots и не позволяют доказать причину слабости SELL.

Рекомендуется сохранить текущий лимит риска 3%, устранить технические дефекты, наладить полный trace и временно честный replay, затем сравнить варианты стратегии на отложенном периоде. Исправления устраняют конкретные неверные допуски и ошибки наблюдения, но сами по себе не обещают PF>1.2, WR>50% или заданное число сигналов. Увеличение риска, принудительный RR=2+, отключение SELL и изменение exits — отдельные эксперименты после исправления измерений. `UNCERTAIN` ниже означает неопределённую причинную атрибуцию/эффект, а не установленный факт.

### Проверка вопросов VII исходного задания

1. **Portfolio gate:** 8526/8750=97.44% попыток блокируется на первом portfolio gate; 7591 — budget, 935 — лимит по символу, 0 — глобальный count. Это попытки сканирования, а не 8526 валидных setups. Сумма OPEN risk арифметически верна; на дату снимка нет доказанных просроченных OPEN. Исправлять admission и сопровождение, не расширять cap до 8–10%.
2. **Низкий WR / TP:SL:** неверны исходные знаменатель WR и интерпретация среднего RR. Есть реальные ошибки геометрии и outcome labels. FVG, reversal и HTF replay содержат воспроизведённые дефекты. Какую долю исторического убытка создал каждый дефект — **(UNCERTAIN)** без версии каждой сделки, fills и replay.
3. **SELL:** ценовой PF=0.163 против BUY=1.061; медиана планового RR SELL=2.90 против BUY=2.12. HTF opposition в текущем shared evaluator симметричен направлениям. Нельзя утверждать, что short проигрывает из-за меньшего RR или доказанного перекоса HTF. Нужны временные сегменты setup×direction×regime и OOS; blanket SELL block не назначается как доказанное исправление.
4. **Пропускная способность при 5/8%:** на замороженных logged states 7591 budget-only отказ исчез бы, 935 symbol-limit остались бы. Это верхняя оценка снятых отказов при фиксированном состоянии, не новых сигналов: последующие состояния портфеля изменятся. Только 96 попыток дошли после initial gate; данных остальных паттернов нет. Число сигналов в неделю **неидентифицируемо**. В sample есть перерывы 10.525 и 80.521 часа, и сканируется только 4h; 8/week нельзя экстраполировать на непрерывную работу 1h+4h.
5. **Приоритет:** E — корректность и измеримость; затем безопасный admission (A без ослабления лимитов); затем проверка качества entry/SL/TP и SELL на честном replay. Не начинать с подгонки порогов.

### Источники и границы проверки

Прочитаны `scheduler/scanner.py`, `strategy/trade_engine.py`, `strategy/pattern_engine.py`, `risk/engine.py`, `strategy/signal_evaluator.py`, `config/settings.py`, storage/trace, tracker, FVG/MSS/HTF, backtest и relevant tests. SQLite открыт только read-only. SHA-256 до/после: `950218335fcd31ef26ced8b7e7663e3b344c0a1b1b436e1f2e8774f3bf210687`; integrity check OK. Снимок датирован **2026-09-14 19:55:57 UTC**: это не состояние живого счёта 21 сентября. Исходные логи берутся из Git commit, чтобы синтетические строки тестов не влияли на статистику.

Аудит начался на `311c98e`; перед публикацией обнаружен новый upstream commit `9d0da04`, и затронутые модули перепроверены. Найденный ранее NameError `_regime_check` **уже исправлен upstream** и исключён из списка актуальных дефектов. В новой версии добавлен POI-entry path и sweep стал необязательным для continuation, расширены lookbacks, введён MAX_SL_ATR. Эти изменения учтены в B-001/B-012. DB и все шесть log blobs между commit побайтно совпадают; результаты по ним не изменились.

Текст prompt и AGENTS всё ещё частично устарел: отдельные FeatureBuilder/ProbabilityEngine удалены, P(TP) рассчитывается shared `estimate_p_tp`; context применяется один раз через if/elif. Установленный CCXT4.2.15 использует DECIMAL_PLACES для Binance/BingX, поэтому утверждение о доказанно неверной формуле tick size здесь не поддерживается.

Воспроизводимые агрегаты и скрипт: `fix/audit_v2/aggregate.json`, `fix/audit_v2/audit_data.py`. Команда из корня репозитория: `python fix/audit_v2/audit_data.py`. Установленный тестовый runtime: Python 3.12; точные версии и результаты приведены в разделе VII. Номера строк в findings относятся к указанному commit и изменятся после применения предыдущих правок: ориентироваться также по именам методов и точным фрагментам.

---

## II. Findings

Все исправления ниже — предложения для реализации и регрессионной проверки. Не запускать код из отчёта целиком как один скрипт: методы вставлять в указанный класс, фрагменты — в указанный async/method context. Не менять исторические исходы без отдельного воспроизводимого восстановления. Технический дефект подтверждён отдельно от неизвестного исторического финансового эффекта.

### B-001 — MAX_SL_ATR обходится широким swing и последующими буферами — Severity: MEDIUM

**File:** `strategy/invalidation.py`, `strategy/trade_engine.py`, `scheduler/scanner.py`, `config/settings.py`
**Lines:** `strategy/invalidation.py:100-108,200-208; strategy/trade_engine.py:160-207; scheduler/scanner.py:537,580; config/settings.py:163-164`

**Current behavior:** Новый MAX_SL_ATR=3 ограничивает sweep/OB/BOS, но если все swings шире лимита, код всё равно возвращает ближайший широкий swing. Затем ATR buffer и wick safety могут расширить даже первоначально допустимый SL за пределы лимита. Комментарий конфигурации обещает ATR fallback для слишком широкого structural SL, которого в этой ветке нет.

**Expected behavior:** Если подходящего swing нет, перейти к следующему приоритету BOS и затем существующему ATR fallback. Проверить окончательное расстояние entry-SL после всех буферов; если оно превышает текущий MAX_SL_ATR, отклонить план. Не переносить SL внутрь invalidation ради прохождения фильтра.

**Why it matters:** Детерминированно BUY entry100/ATR1/swing90 возвращает invalidation90 (10ATR) при cap3; SELL зеркально110. При swing97 BUY SL96.85 даёт3.15ATR уже после buffer. Это нарушение объявленного ограничения; доля реальных сигналов и влияние на доходность неизвестны, поэтому MEDIUM, а не обещание >5% эффекта.

**Evidence (verbatim):**
```python
        # All swing lows too wide — use closest one anyway (better than ATR fallback)
        level = max(valid_swings)
        return Invalidation(
            level=level,
            type="swing_point",
            reason=f"swing low — fractal point (wide SL, {((entry - level) / atr):.1f} ATR)",
            buffer_pct=0,
            distance_pct=(entry - level) / entry * 100,
        )
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('strategy/invalidation.py',
'''        # All swing lows too wide — use closest one anyway (better than ATR fallback)
        level = max(valid_swings)
        return Invalidation(
            level=level,
            type="swing_point",
            reason=f"swing low — fractal point (wide SL, {((entry - level) / atr):.1f} ATR)",
            buffer_pct=0,
            distance_pct=(entry - level) / entry * 100,
        )
''',
'''        # No swing fits the configured cap; try BOS, then ATR fallback.
'''),
    ('strategy/invalidation.py',
'''        # All swing highs too wide — use closest one anyway
        level = min(valid_swings)
        return Invalidation(
            level=level,
            type="swing_point",
            reason=f"swing high — fractal point (wide SL, {((level - entry) / atr):.1f} ATR)",
            buffer_pct=0,
            distance_pct=(level - entry) / entry * 100,
        )
''',
'''        # No swing fits the configured cap; try BOS, then ATR fallback.
'''),
    ('strategy/trade_engine.py',
'''        # ═══ Step 3: Find Targets (TP) ═══
''',
'''        # The configured maximum applies to the final SL, including buffers
        # and wick safety. Never move SL inside invalidation to satisfy the cap.
        max_sl_distance = atr * cfg.max_sl_atr
        final_sl_distance = abs(entry - sl)
        if not (0 < max_sl_distance < float("inf")) or final_sl_distance > max_sl_distance:
            return TradePlan(
                direction=direction,
                symbol=ind.symbol,
                timeframe=timeframe,
                entry_price=entry,
                invalidation=invalidation,
                sl=sl,
                sl_source=invalidation.type,
                sl_distance_pct=round(final_sl_distance / entry * 100, 2),
                is_valid=False,
                rejection_reason=(
                    f"final SL distance {final_sl_distance:.8g} exceeds "
                    f"MAX_SL_ATR={cfg.max_sl_atr} ({max_sl_distance:.8g})"
                ),
            )

        # ═══ Step 3: Find Targets (TP) ═══
'''),
    ('scheduler/scanner.py',
'''
        if sl is None or tp is None:
''',
'''
        if not trade_plan.is_valid or sl is None or tp is None:
'''),
    ('scheduler/scanner.py',
'''
                if sl is None or tp is None:
''',
'''
                if not trade_plan.is_valid or sl is None or tp is None:
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Новые 10 regression cases: обе стороны, широкий swing пропускается к допустимому BOS либо ATR fallback; финальный SL после ATR buffer или wick expansion не проходит cap; допустимый план остаётся валидным. Production examples сохранены в latest-reproduction-results.json. Полные собранные модули компилируются.

**Integration / tests / limitations:** Не менять число3 и не подбирать новый ATR multiplier. Только исполнить текущий конфигурационный контракт. В scanner оба этапа построения плана должны учитывать trade_plan.is_valid; иначе отказ теряет причину и доживает до последующего invalid-price gate. Backtest уже проверяет is_valid. Ужесточение фактически неработавшего ограничения может сократить объём; проверить shadow. Код max_sl_atr<=0 не является documented disable и отвергается финальной проверкой.

---


### B-002 — RiskEngine принимает неверную геометрию/NaN и оживляет нулевой Kelly — CRITICAL

**File:** `risk/engine.py`; **Lines:** 112–167, 197; callers `scheduler/scanner.py:924`, `backtest/engine.py:896`, `web/webhook.py:266`.

**Current behavior:** знак SL/TP теряется через abs; нет finite-проверок, проверки фактического направления, portfolio не используется. Kelly<=0 становится risk=0.1% через final floor. **Expected behavior:** BUY sl<entry<tp; SELL tp<entry<sl; конечные значения; ненулевая положительная Kelly allocation; лимит учитывает новый риск. **Why it matters:** защитный слой разрешает заведомо неправильные сигналы и ставки с неположительным ожидаемым результатом даже по собственной некалиброванной P(TP). Это не доказывает, что Kelly или P(TP) обеспечивают прибыль; устраняется арифметическая ошибка.

**Evidence:**

```python

        if entry_price <= 0 or sl <= 0 or tp <= 0:
            return RiskDecision(
                should_trade=False,
                rejection_reason="invalid price data",
            )

        risk_dist = abs(entry_price - sl)
        reward_dist = abs(tp - entry_price)

        if risk_dist <= 0:
            return RiskDecision(
                should_trade=False,
                rejection_reason="zero risk distance",
            )

        rr_ratio = reward_dist / risk_dist
        sl_distance_pct = risk_dist / entry_price * 100

        # === SOFT GATES (log violations, don't block — Kelly sizing handles them) ===

        _structural_sources = {
            "sweep_extreme", "ob_boundary", "swing_point",
            "structure_break", "structural",  # invalidation.py produces both
            "bos_level", "ob", "fractal", "bos",  # signal_engine.py / trade_engine.py
        }
        _is_structural = sl_source and sl_source in _structural_sources
```


```python

        # Kelly-inspired: f = (p * b - q) / b
        p = p_tp
        q = 1 - p
        b = rr_ratio
        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(0.0, min(kelly, 0.20))  # cap at 20% (half-Kelly)

        # Scale by model confidence
        kelly *= confidence

        # Final risk = min(kelly, base_risk)
        risk_pct = min(kelly * 100, self.base_risk_pct)
```


```python
        # Clamp
        risk_pct = max(self.min_risk_pct, min(risk_pct, self.max_risk_pct))
```


**Fix:** целиком заменить только метод `RiskEngine.evaluate` следующим методом (отступ 4 пробела внутри class). Существующие импорты/dataclass/init/singleton сохранить. `direction` необязателен ради совместимости со старыми callers/tests: без него валидируется геометрия со стороной, выведенной из SL; **все три production callers обязаны передать реальное направление**. В scanner и backtest к вызову добавить `direction=setup.direction`, в webhook — `direction=action`. Метод ниже также ранним образом отбрасывает превышение portfolio cap, однако окончательная атомарная проверка из B-003 всё равно обязательна.


```python
def evaluate(
    self,
    portfolio: PortfolioState,
    entry_price: float,
    sl: float,
    tp: float,
    atr_pct: float = 0.0,
    p_tp: float = 0.5,
    confidence: float = 0.5,
    mss_quality: float = 0.0,
    atr: float = 0.0,
    sl_source: Optional[str] = None,
    *,
    direction: Optional[str] = None,
) -> RiskDecision:
    """Evaluate risk and size the position.

    Args:
        portfolio: current portfolio state
        entry_price: entry price
        sl: stop loss price
        tp: take profit price
        atr_pct: ATR as percentage of price (for volatility scaling)
        p_tp: probability of take profit [0, 1]
        confidence: model confidence [0, 1]
        mss_quality: MSS score from Pattern Engine [0, 100]
        atr: raw ATR value
        sl_source: source of SL calculation (bos, ob, fractal, atr)

    Returns:
        RiskDecision with should_trade, risk_pct, and details.
    """
    import math

    # Validate before arithmetic: comparisons against NaN do not reject it.
    values = (entry_price, sl, tp, atr_pct, p_tp, confidence, mss_quality, atr)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        return RiskDecision(False, rejection_reason="non-finite risk input")
    if not 0 <= p_tp <= 1 or not 0 <= confidence <= 1:
        return RiskDecision(False, rejection_reason="probability/confidence outside [0, 1]")
    if direction is None:
        direction = "buy" if sl < entry_price else "sell"
    if direction not in ("buy", "sell"):
        return RiskDecision(False, rejection_reason="invalid direction")
    if atr_pct < 0 or atr < 0 or not 0 <= mss_quality <= 100:
        return RiskDecision(False, rejection_reason="invalid sizing input")
    if not math.isfinite(portfolio.total_risk_pct) or portfolio.total_risk_pct < 0:
        return RiskDecision(False, rejection_reason="invalid portfolio risk")
    if not math.isfinite(portfolio.max_portfolio_risk_pct) or portfolio.max_portfolio_risk_pct <= 0:
        return RiskDecision(False, rejection_reason="invalid portfolio cap")
    if portfolio.active_count >= portfolio.max_active_signals:
        return RiskDecision(False, rejection_reason="max active signals")

    # === DATA VALIDITY ===

    if entry_price <= 0 or sl <= 0 or tp <= 0:
        return RiskDecision(
            should_trade=False,
            rejection_reason="invalid price data",
        )

    geometry_valid = (
        sl < entry_price < tp if direction == "buy" else tp < entry_price < sl
    )
    if not geometry_valid:
        return RiskDecision(False, rejection_reason=f"invalid {direction} SL/entry/TP geometry")

    risk_dist = abs(entry_price - sl)
    reward_dist = abs(tp - entry_price)

    if risk_dist <= 0:
        return RiskDecision(
            should_trade=False,
            rejection_reason="zero risk distance",
        )

    rr_ratio = reward_dist / risk_dist
    sl_distance_pct = risk_dist / entry_price * 100

    # === SOFT GATES (log violations, don't block — Kelly sizing handles them) ===

    _structural_sources = {
        "sweep_extreme", "ob_boundary", "swing_point",
        "structure_break", "structural",  # invalidation.py produces both
        "bos_level", "ob", "fractal", "bos",  # signal_engine.py / trade_engine.py
    }
    _is_structural = sl_source and sl_source in _structural_sources

    if rr_ratio < self.min_rr_ratio:
        logger.info(f"Risk hard gate: RR={rr_ratio:.2f} < {self.min_rr_ratio} (BLOCKED)")
        return RiskDecision(
            should_trade=False,
            rr_ratio=round(rr_ratio, 2),
            rejection_reason=f"RR {rr_ratio:.2f} < min {self.min_rr_ratio}",
        )

    if sl_distance_pct < self.sl_absolute_min_pct:
        logger.info(f"Risk soft gate: SL tight {sl_distance_pct:.2f}% < {self.sl_absolute_min_pct}% (proceeding via Kelly)")

    if not _is_structural and sl_distance_pct > self.sl_absolute_max_pct:
        logger.info(f"Risk soft gate: SL wide {sl_distance_pct:.2f}% > {self.sl_absolute_max_pct}% (proceeding via Kelly)")

    if atr > 0 and entry_price > 0 and not _is_structural:
        atr_pct_calc = atr / entry_price * 100
        min_sl_from_atr = atr_pct_calc * self.sl_min_atr_multiplier
        if sl_distance_pct < min_sl_from_atr:
            logger.info(f"Risk soft gate: SL tight vs ATR {sl_distance_pct:.2f}% < {self.sl_min_atr_multiplier}x ATR (proceeding via Kelly)")

    # === POSITION SIZING ===

    # Kelly-inspired: f = (p * b - q) / b
    p = p_tp
    q = 1 - p
    b = rr_ratio
    kelly = (p * b - q) / b if b > 0 else 0
    if kelly <= 0 or confidence <= 0:
        return RiskDecision(False, rr_ratio=round(rr_ratio, 2),
                            rejection_reason="non-positive Kelly allocation")
    kelly = min(kelly, 0.20)  # preserve existing allocation cap

    # Scale by model confidence
    kelly *= confidence

    # Final risk = min(kelly, base_risk)
    risk_pct = min(kelly * 100, self.base_risk_pct)

    # Volatility adjustment
    vol_adj = 1.0
    if atr_pct > 4.0:
        vol_adj = 0.5
    elif atr_pct > 2.5:
        vol_adj = 0.75
    risk_pct *= vol_adj

    # MSS quality soft adjustment (0-100 → 0.8x-1.1x)
    mss_adj = 1.0
    if mss_quality > 0:
        mss_adj = 0.8 + (mss_quality / 100.0) * 0.3
        mss_adj = max(0.8, min(1.1, mss_adj))
    risk_pct *= mss_adj

    # SL distance quality (soft)
    if sl_distance_pct < 1.0:
        risk_pct *= 1.1  # bonus for tight SL
    elif sl_distance_pct > 3.0:
        risk_pct *= 0.8  # penalty for wide SL

    # Clamp
    risk_pct = round(max(self.min_risk_pct, min(risk_pct, self.max_risk_pct)), 4)
    if not math.isfinite(risk_pct) or risk_pct <= 0:
        return RiskDecision(False, rejection_reason="invalid calculated risk")
    if round(portfolio.total_risk_pct + risk_pct, 10) > portfolio.max_portfolio_risk_pct:
        return RiskDecision(False, rr_ratio=round(rr_ratio, 2),
                            rejection_reason="portfolio risk including new trade exceeds cap")

    logger.info(
        f"Risk decision: risk={risk_pct:.2f}% | "
        f"RR={rr_ratio:.2f} | SL={sl_distance_pct:.2f}% | "
        f"P(TP)={p_tp:.1%} | Kelly={kelly:.3f} | "
        f"vol_adj={vol_adj:.2f} | mss_adj={mss_adj:.2f}"
    )

    return RiskDecision(
        should_trade=True,
        risk_pct=round(risk_pct, 4),
        rr_ratio=round(rr_ratio, 2),
        sl_price=sl,
        tp_price=tp,
        kelly_fraction=round(kelly, 4),
        volatility_adjustment=vol_adj,
        probability_confidence=confidence,
    )
```


**Verification:** `verify_fixes.py` тестирует корректные BUY/SELL, TP/SL не с той стороны, NaN/Inf, p_tp outside [0,1], confidence=0, Kelly<=0, 2.9%+1% при cap3 и active_count=max. Исходник разрешает NaN-entry с RR=nan; p_tp=.2, RR=2 => Kelly=0, should_trade=True, risk=.1%. После patch оба rejected. Старый call без direction остаётся валиден. Дополнить production-проверки actual setup.direction и обновить устаревшие tests, где в evaluate передаются удалённые features/probability kwargs. Не подменять тестовую ошибку API возвратом этих неиспользуемых kwargs в production.

---


### B-003 — Портфельный lock ничего не резервирует; final save неатомарен — CRITICAL

**File:** `scheduler/scanner.py`; **Lines:** 223–248, 900–921, 1046–1073,1121; `storage/database.py`:392–452,1087–1094.

**Current behavior:** lock защищает только три чтения. Все параллельные задачи проходят до появления нового outcome. Recheck тоже без lock и без symbol cap. `existing_risk >= cap` не считает новый risk. Signal и Outcome коммитятся двумя сессиями, между ними trace и другие await. Падение процесса оставляет сигнал без OPEN outcome/учёта риска. **Expected behavior:** admission-check + Signal + OPEN outcome + cooldown единым write transaction. **Why it matters:** лимит3 может стать3.9 даже без concurrency; разные TF одного symbol могут нарушить max_per_symbol1. Это дефект защиты, а не доказательство, что cap3 слишком строг.

**Evidence:**

```python
        # 0.2 Portfolio risk — atomic check-and-reserve
        # The lock ensures no two concurrent scan tasks see the same active_count
        # and both pass the limit check.
        async with _portfolio_lock:
            max_per_sym = config.max_active_signals_per_symbol
            sym_active = await db.get_active_signals_count_by_symbol(symbol)
            if sym_active >= max_per_sym:
                reason = f"max active signals for {symbol} ({sym_active}/{max_per_sym})"
                _funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
                trace.blocked("portfolio_risk", reason)
                await trace.save(db)
                return None

            max_sigs = config.max_active_signals
            max_risk = config.max_portfolio_risk_pct
            active_count = await db.get_active_signals_count()
            if active_count >= max_sigs:
                reason = f"max active signals ({active_count}/{max_sigs})"
                _funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
                trace.blocked("portfolio_risk", reason)
                await trace.save(db)
                return None
            portfolio_risk = await db.get_portfolio_risk_sum()
            if portfolio_risk >= max_risk:
                reason = f"portfolio risk {portfolio_risk:.1f}% >= {max_risk}%"
                _funnel.log_gate(symbol, timeframe, "portfolio_risk", "BLOCKED", reason)
                trace.blocked("portfolio_risk", reason)
                await trace.save(db)
                return None
```


```python
        # Re-check portfolio state (may have changed during pipeline)
        _recheck_active = await db.get_active_signals_count()
        _recheck_risk = await db.get_portfolio_risk_sum()
        if _recheck_active >= config.max_active_signals:
            reason = f"max active signals ({_recheck_active}/{config.max_active_signals}) [re-check]"
            _funnel.log_gate(symbol, timeframe, "portfolio_risk_recheck", "BLOCKED", reason)
            trace.blocked("portfolio_risk_recheck", reason)
            trace.set_version(VERSION, _config_snapshot)
            await trace.save(db)
            return None
        if _recheck_risk >= config.max_portfolio_risk_pct:
            reason = f"portfolio risk {_recheck_risk:.1f}% >= {config.max_portfolio_risk_pct}% [re-check]"
            _funnel.log_gate(symbol, timeframe, "portfolio_risk_recheck", "BLOCKED", reason)
            trace.blocked("portfolio_risk_recheck", reason)
            trace.set_version(VERSION, _config_snapshot)
            await trace.save(db)
            return None

        portfolio_state = PortfolioState(
            active_count=_recheck_active,
            total_risk_pct=_recheck_risk,
            max_active_signals=config.max_active_signals,
            max_portfolio_risk_pct=config.max_portfolio_risk_pct,
```


```python
        trace.set_features(_trace_features)
        trace.set_version(VERSION, _config_snapshot)
        await trace.save(db, signal_id=saved_signal.id)

        await db.create_outcome(saved_signal.id, risk_pct=risk_decision.risk_pct)

        # ═══ Phase 8: Cooldown + Notify ═══

        await _set_cooldown(symbol, timeframe)
```


**Fix:**

1. В `Database` добавить полный метод ниже, отступ 4 пробела. Existing `save_signal` оставить для совместимости read/test/tooling. SQLite BEGIN IMMEDIATE сериализует check/write даже между двумя connection/process. PostgreSQL lock ветка приведена для совместимости, но в этом аудите не исполнялась. Для другой DB метод fail-closed. Старые OPEN rows с NULL/0 risk или orphan signal должны сначала пройти reconciliation с серверными данными: метод намеренно не считает неизвестный риск нулевым.


```python
async def save_signal_with_risk(
    self, *, risk_pct: float, **signal_values
) -> tuple[Optional[Signal], Optional[str]]:
    """Check current limits and persist signal, outcome, cooldown in one transaction."""
    import math
    from strategy.signal_evaluator import get_cooldown_minutes

    if not math.isfinite(risk_pct) or risk_pct <= 0:
        return None, "invalid new risk"
    symbol = signal_values["symbol"]
    timeframe = signal_values["timeframe"]
    async with self._session_factory() as session:
        # A read-only asyncio lock is insufficient: writers must be serialized.
        if self._engine.dialect.name == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
        elif self._engine.dialect.name == "postgresql":
            await session.execute(text(
                "LOCK TABLE signal_outcomes IN SHARE ROW EXCLUSIVE MODE"
            ))
        else:
            raise RuntimeError("Atomic portfolio gate supports SQLite/PostgreSQL only")

        rows = list((await session.execute(
            select(SignalOutcome, Signal.symbol)
            .outerjoin(Signal, SignalOutcome.signal_id == Signal.id)
            .where(SignalOutcome.status == "OPEN")
        )).all())
        if any(active_symbol is None for _, active_symbol in rows):
            return None, "orphan_open_outcome: reconcile OPEN outcomes before admission"
        same_symbol = sum(1 for _, active_symbol in rows if active_symbol == symbol)
        if same_symbol >= config.max_active_signals_per_symbol:
            return None, f"symbol_limit: {same_symbol}/{config.max_active_signals_per_symbol}"
        if len(rows) >= config.max_active_signals:
            return None, f"active_limit: {len(rows)}/{config.max_active_signals}"
        if any(row.risk_pct is None or not math.isfinite(row.risk_pct) or row.risk_pct <= 0
               for row, _ in rows):
            return None, "unknown_open_risk: reconcile OPEN outcomes before admission"
        current_risk = sum(row.risk_pct for row, _ in rows)
        if round(current_risk + risk_pct, 10) > config.max_portfolio_risk_pct:
            return None, (f"risk_budget: {current_risk:.4f}+{risk_pct:.4f}"
                          f">{config.max_portfolio_risk_pct:.4f}")

        now = datetime.now(timezone.utc)
        key = f"cooldown:{symbol}:{timeframe}"
        cooldown = await session.get(BotSetting, key)
        if cooldown is not None:
            try:
                last = datetime.fromisoformat(cooldown.value)
            except (TypeError, ValueError):
                return None, "invalid_persisted_cooldown"
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            minutes = get_cooldown_minutes(
                timeframe, config.signal_cooldown_minutes,
                config.signal_cooldown_tf_multiplier,
            )
            if (now - last).total_seconds() < minutes * 60:
                return None, f"cooldown: {minutes}m"

        values = dict(signal_values)
        values["reasons"] = "\n".join(values["reasons"])
        factors = values.get("confidence_v2_factors")
        values["confidence_v2_factors"] = json.dumps(factors) if factors else None
        values["sent_at"] = now
        signal = Signal(**values)
        session.add(signal)
        await session.flush()
        session.add(SignalOutcome(signal_id=signal.id, status="OPEN", risk_pct=risk_pct))
        if cooldown is None:
            session.add(BotSetting(key=key, value=now.isoformat(), updated_at=now))
        else:
            cooldown.value = now.isoformat()
            cooldown.updated_at = now
        await session.commit()
        return signal, None
```


2. В scanner целиком заменить `saved_signal = await db.save_signal(...)` строк 1046–1073 блоком ниже. Это полные arguments, не сокращённый пример. Сразу следом остаётся существующий trace.set_signal и snapshot.


```python
saved_signal, _admission_reason = await db.save_signal_with_risk(
    risk_pct=risk_decision.risk_pct,
    symbol=result.symbol,
    timeframe=result.timeframe,
    signal_type=result.signal.value,
    close_price=result.close,
    sl=result.sl,
    tp=result.tp,
    score=result.score,
    reasons=result.reasons,
    confirmed=False,
    factor_fingerprint=factor_fingerprint,
    confidence_v2_pct=p_tp * 100,
    confidence_v2_factors=[],
    entry_candle_open=_entry_candle_open,
    # Execution snapshot
    entry_price_source="LIVE_TICKER",
    entry_open=float(_last_candle["open"]) if _last_candle is not None else None,
    entry_mid=float((_last_candle["high"] + _last_candle["low"]) / 2) if _last_candle is not None else None,
    entry_bid=_ticker.get("bid") if _ticker else None,
    entry_ask=_ticker.get("ask") if _ticker else None,
    entry_spread=(_ticker.get("ask") - _ticker.get("bid")) if _ticker and _ticker.get("ask") and _ticker.get("bid") else None,
    entry_atr=_atr,
    entry_tick_size=_tick_size,
    signal_detected_at=datetime.now(timezone.utc),
)

if saved_signal is None:
    _funnel.log_gate(symbol, timeframe, "portfolio_admission", "BLOCKED", _admission_reason)
    trace.blocked("portfolio_admission", _admission_reason)
    trace.set_version(VERSION, _config_snapshot)
    await trace.save(db)
    return None
trace.passed("portfolio_admission")
```


3. Из scanner удалить прежние вызовы `await db.create_outcome(saved_signal.id, risk_pct=risk_decision.risk_pct)` и `await _set_cooldown(symbol, timeframe)` после trace.save. Эти операции уже committed новым методом; повторный create_outcome удвоит риск. Phase0 и Phase4 оставить как быстрые предварительные фильтры; заменить misleading comments atomic/reserve на preliminary check.

4. В `web/webhook.py` production writer тоже использовать admission API: заменить полный save_signal block 307–322 следующим. Здесь заодно DateTime получает datetime вместо time.time float, а close_price=entry_price совпадает с ценой, для которой посчитан риск. В result и returned payload цены также должны использовать entry_price. Webhook HTTP route smoke test обязателен при его включении; внешних запросов аудит не делал.


```python
from datetime import datetime, timezone
saved_signal, admission_reason = await db.save_signal_with_risk(
    risk_pct=risk_decision.risk_pct,
    symbol=result.symbol,
    timeframe=result.timeframe,
    signal_type=result.signal.value,
    close_price=entry_price,
    sl=result.sl,
    tp=result.tp,
    score=result.score,
    reasons=result.reasons,
    confirmed=False,
    factor_fingerprint=f"webhook|components={setup.components_count}",
    confidence_v2_pct=p_tp * 100,
    confidence_v2_factors=[],
    entry_price_source="WEBHOOK",
    signal_detected_at=datetime.now(timezone.utc),
)

if saved_signal is None:
    return {"status": "rejected", "reason": admission_reason}
```


**Verification:** `verify_fixes.py`: четыре параллельных admissions BTC дают ровно1 signal/outcome при symbol cap1; риск1+2.1>3 rejected, риск1+2=3 accepted; искусственный exception на INSERT outcome откатывает Signal, Outcome, BotSetting. Начальный source test показывает 2.9+1=>3.9. Добавить тест concurrent разных symbols при global count cap и разных DB connections/process (текущий asyncio-test использует разные sessions реальной SQLite). Подключить webhook к тому же API. Не создавать резерв до дорогостоящего анализа без процедуры release/TTL.

---


### B-004 — Live entry_override заменяется незаполненным FVG; market/backtest используют разные сделки — Severity: CRITICAL

**File:** `strategy/trade_engine.py`, `scheduler/scanner.py`, `backtest/engine.py`, `config/settings.py`
**Lines:** `strategy/trade_engine.py:56-81; scheduler/scanner.py:565-601; backtest/engine.py:850-857,950-955; config/settings.py:724-734`

**Current behavior:** Даже переданный live entry_override переписывается медианой первого близкого активного FVG. Direction check пропускает BUY ниже live и SELL выше live, хотя это ещё не исполнившиеся лимитные заявки. Backtest по умолчанию median_immediate подтверждает такие заполнения; режим close меняет только entry после построения SL/TP. Market-backtest дополнительно делает lifetime per-FVG dedup, которого нет в live.

**Expected behavior:** Market-план использует переданную цену или close, не заменяет её лимитом. Только явно запрошенный limit-план допускает FVG median и требует состояния pending. В backtest market entry/SL/TP/overrides считаются вместе; phantom model больше не допускается. Для market-модели убрать дополнительный FVG-dedup, сохранить обычный cooldown/dedup.

**Why it matters:** При live=101 production выдаёт BUY entry=99 и SELL entry=103; обе записи проходят direction gate. Это неверные сигналы/сделки и искажённый RR, даже при безошибочном RiskEngine. Старые результаты median_immediate нельзя использовать для оценки PF.

**Evidence (verbatim):**
```python
        # ═══ FVG ENTRY: use median (50%) of active FVG as entry price ═══
        if fvgs:
            fvg_proximity_pct = config.pattern_engine.fvg_proximity_pct
            for f in fvgs:
                f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
                if f.is_active and f_dir == direction:
                    fvg_median = (f.top + f.bottom) / 2.0
                    dist_pct = abs(fvg_median - entry) / entry * 100
                    if dist_pct > fvg_proximity_pct:
                        logger.debug(
                            f"FVG entry skipped: median {fvg_median:.4f} is "
                            f"{dist_pct:.1f}% from price {entry:.4f} "
                            f"(max {fvg_proximity_pct}%)"
                        )
                        continue
                    entry = round(fvg_median, 8)
                    logger.debug(
                        f"FVG entry: using median {entry:.4f} "
                        f"(top={f.top:.4f}, bottom={f.bottom:.4f}, type={f.type}, "
                        f"dist={dist_pct:.1f}%)"
                    )
                    break
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('strategy/trade_engine.py',
'''        entry_override: Optional[float] = None,
''',
'''        entry_override: Optional[float] = None,
        entry_mode: Literal["market", "limit"] = "market",
'''),
    ('strategy/trade_engine.py',
'''        # ═══ FVG ENTRY: use median (50%) of active FVG as entry price ═══
        if fvgs:
''',
'''        if entry_mode not in ("market", "limit"):
            raise ValueError(f"Unsupported entry_mode: {entry_mode}")

        # A limit price is a pending order, never an executed market fill.
        if fvgs and entry_mode == "limit":
'''),
    ('config/settings.py',
'''    execution_model: str = os.getenv("EXECUTION_MODEL", "median_immediate")
''',
'''    execution_model: str = os.getenv("EXECUTION_MODEL", "close")
'''),
    ('backtest/engine.py',
'''        """Internal: fetch data and walk through candles."""
''',
'''        """Internal: fetch data and walk through candles."""
        if config.execution_model not in ("close", "limit_pending"):
            raise ValueError(
                "EXECUTION_MODEL must be close or limit_pending; "
                "median_immediate creates unfilled trades"
            )
'''),
    ('backtest/engine.py',
'''                    sweeps=sweeps,
                    fvgs=fvgs,
                    df=_df_clean,
                    timeframe=self.timeframe,
                )
''',
'''                    sweeps=sweeps,
                    fvgs=fvgs,
                    df=_df_clean,
                    timeframe=self.timeframe,
                    entry_mode=(
                        "limit" if config.execution_model == "limit_pending" else "market"
                    ),
                )
'''),
    ('backtest/engine.py',
'''                # === Execution model ===
                # "close": the signal fires at candle close, so the fill price is
                # the signal bar's close (mirrors live P&L tracking, which computes
                # from signal.close_price). "median_immediate" (default) keeps the
                # FVG median even if price never traded there; "limit_pending" keeps
                # the median as a resting limit and fills only on a later bar's touch.
                if config.execution_model == "close":
                    entry_price = float(ind.close) if ind.close else entry_price
''',
'''                # Entry, SL, TP and RR are one plan. Do not mutate entry after
                # constructing the plan; the market model already uses close.
'''),
    ('backtest/engine.py',
'''                _ref_fvg = None
                for _f in fvgs or []:
                    _f_dir = "buy" if _f.type == "bullish" else "sell" if _f.type == "bearish" else _f.type
                    if _f.is_active and _f_dir == setup.direction:
                        _ref_fvg = _f
                        break
''',
'''                _ref_fvg = None
                if config.execution_model == "limit_pending":
                    for _f in fvgs or []:
                        _f_dir = {"bullish": "buy", "bearish": "sell"}.get(_f.type)
                        if (
                            _f.is_active and _f_dir == setup.direction
                            and abs((_f.top + _f.bottom) / 2.0 - entry_price) <= 1e-8
                        ):
                            _ref_fvg = _f
                            break
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Два mirrored теста проверяют live override, fallback к close, RR от итоговых цен и отдельный explicit limit. Regression: close-model и market-план должны иметь одинаковые entry/SL/TP; median_immediate должен завершаться понятной ошибкой. Нет сетевых запросов или реальных ордеров.

**Integration / tests / limitations:** Server .env: EXECUTION_MODEL=close. Live всегда оставлять default entry_mode=market. Не включать REQUIRE_ENTRY_ZONE как замену: он проверяет границу FVG, а не исполнение медианы. limit_pending остаётся отдельным исследовательским режимом, не parity с live и не доказательством actual fill; обработка intrabar лимитов требует отдельной проверки. Старые тесты, утверждающие phantom fill по умолчанию, заменить на tests/test_market_entry_contract.py. Докстринги к режимам обновить вместе с конфигом.

---

### B-005 — FVG проверяется на касание до своего возникновения — Severity: CRITICAL

**File:** `liquidity/fvg.py`
**Lines:** `liquidity/fvg.py:82,94,103-108`

**Current behavior:** Список fvgs содержит только найденные гэпы, а zip(..., range(...)) присваивает им порядковые позиции всех свечей. При пропусках между паттернами fill_start попадает до candle3. Timestamp дополнительно относится к candle2, хотя index относится к candle3.

**Expected behavior:** Проверять только свечи строго после candle3 конкретного FVG и хранить время candle3. Существующую семантику first-touch mitigation оставить без изменений.

**Why it matters:** Непотроганные FVG исчезают из active-set; меняются подтверждения, цены входа, TP и число сигналов. Это не настройка min_size или max_age.

**Evidence (verbatim):**
```python
    # Проверка: какие FVG уже закрыты ценой
    # Use local index (i+2) — highs/lows are local to the sliced data.
    for fvg, local_idx in zip(fvgs, range(1, len(data) - 1)):
        _fill_start = local_idx + 2
        if _fill_start < len(highs):
            fvg.filled = _is_fvg_filled_np(fvg, highs[_fill_start:], lows[_fill_start:])
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('liquidity/fvg.py',
'''                ts = _to_datetime(_orig_index[i])
                fvgs.append(FairValueGap(
                    type="bullish",
''',
'''                ts = _to_datetime(_orig_index[i + 1])
                fvgs.append(FairValueGap(
                    type="bullish",
'''),
    ('liquidity/fvg.py',
'''                ts = _to_datetime(_orig_index[i])
                fvgs.append(FairValueGap(
                    type="bearish",
''',
'''                ts = _to_datetime(_orig_index[i + 1])
                fvgs.append(FairValueGap(
                    type="bearish",
'''),
    ('liquidity/fvg.py',
'''    # Проверка: какие FVG уже закрыты ценой
    # Use local index (i+2) — highs/lows are local to the sliced data.
    for fvg, local_idx in zip(fvgs, range(1, len(data) - 1)):
        _fill_start = local_idx + 2
        if _fill_start < len(highs):
            fvg.filled = _is_fvg_filled_np(fvg, highs[_fill_start:], lows[_fill_start:])
''',
'''    # A gap exists only after candle 3; inspect strictly later bars.
    for fvg in fvgs:
        fill_start = fvg.index - offset + 1
        fvg.filled = _is_fvg_filled_np(
            fvg, highs[fill_start:], lows[fill_start:]
        )
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Воспроизведение: formation index=7, top=104, единственный следующий low=105; production filled=True. Четыре parameterized теста proposals покрывают BUY/SELL, lookback offset=0/20 и настоящее последующее касание.

**Integration / tests / limitations:** Применять после B-004: восстановление активных гэпов при старом market-entry увеличивает число фантомных заполнений. Обновить ожидания timestamp на candle3; новые тесты tests/test_fvg_causality.py.

---

### B-006 — MSS и reversal требуют несовместимые sweep; причинные поля берутся от другой свечи — Severity: CRITICAL

**File:** `market_structure/structure.py`, `strategy/pattern_engine.py`, `scheduler/scanner.py`
**Lines:** `market_structure/structure.py:143-166,172-195; strategy/pattern_engine.py:319-328,399-413; scheduler/scanner.py:334-342`

**Current behavior:** classify_choch пропускает только противоположный тип sweep/CHoCH, а reversal требует одинаковые типы sweep/MSS. Одиночный валидный sweep никогда не проходит оба слоя. Классификатор берёт reclaim_bars от первого sweep, переданного сканером, и максимизирует исторический displacement с body текущей свечи, которая может идти позже CHoCH. Pattern выбирает newest sweep вообще, даже если он позже MSS или противоположного типа.

**Expected behavior:** Единый deterministic selector: bullish low sweep -> bullish CHoCH, bearish high sweep -> bearish CHoCH; sweep строго раньше CHoCH, в causal window, reclaim уже завершён. Один и тот же выбранный sweep используется в обоих слоях; displacement измеряется только внутри соответствующего leg при доступном df.

**Why it matters:** Блокируются правильные reversals и создаются ложные MSS-компоненты/качество. Этот дефект симметричен; он не доказывает причину отрицательных SELL из статистики.

**Evidence (verbatim):**
```python
    # Find matching sweep (OPPOSITE direction, within causal window)
    matching_sweep = None
    bars_since = 999

    for s in sweeps:
        if not s.is_valid:
            continue
        # Sweep direction must OPPOSE CHoCH direction
        # Bullish CHoCH = structure shifts up AFTER bearish sweep (sell-side grab)
        # Bearish CHoCH = structure shifts down AFTER bullish sweep (buy-side grab)
        sweep_dir = "buy" if s.type == "bullish" else "sell"
        choch_dir = "buy" if choch.type == "bullish" else "sell"
        if sweep_dir == choch_dir:
            continue

        # Check causal window
        if choch.candle_index >= 0 and s.candle_index >= 0:
            delta = choch.candle_index - s.candle_index
        else:
            delta = 0  # unknown index, assume close
        if 0 <= delta <= max_causal_bars:
            if matching_sweep is None or delta < bars_since:
                matching_sweep = s
                bars_since = delta
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('market_structure/structure.py',
'''def classify_choch(
    choch: CHoCH,
    sweeps: list,
    displacement_atr: float = 0.0,
    reclaim_bars: int = 0,
    volume_ratio: float = 1.0,
    htf_aligned: bool = False,
    max_causal_bars: int = 5,
    df: Optional[pd.DataFrame] = None,
    atr_value: float = 0.0,
) -> CHoCH:
    """Classify CHoCH strength as weak/normal/mss.

    MSS criteria (all must pass):
    1. Sweep within causal window (max_causal_bars, default 5)
    2. Displacement >= 1 ATR (measured as max body between sweep and CHoCH)
    3. Reclaim <= 2 bars
    """
    choch.displacement_score = displacement_atr
    choch.reclaim_bars = reclaim_bars

    # Find matching sweep (OPPOSITE direction, within causal window)
    matching_sweep = None
    bars_since = 999

    for s in sweeps:
        if not s.is_valid:
            continue
        # Sweep direction must OPPOSE CHoCH direction
        # Bullish CHoCH = structure shifts up AFTER bearish sweep (sell-side grab)
        # Bearish CHoCH = structure shifts down AFTER bullish sweep (buy-side grab)
        sweep_dir = "buy" if s.type == "bullish" else "sell"
        choch_dir = "buy" if choch.type == "bullish" else "sell"
        if sweep_dir == choch_dir:
            continue

        # Check causal window
        if choch.candle_index >= 0 and s.candle_index >= 0:
            delta = choch.candle_index - s.candle_index
        else:
            delta = 0  # unknown index, assume close
        if 0 <= delta <= max_causal_bars:
            if matching_sweep is None or delta < bars_since:
                matching_sweep = s
                bars_since = delta

    if matching_sweep is not None:
        choch.has_sweep_reference = True
        choch.causality_score = calc_causality(bars_since)

        # Measure displacement as max body/ATR between sweep and CHoCH
        # (not just the CHoCH candle — ICT: displacement leg causes the structure break)
        if df is not None and atr_value > 0 and matching_sweep.candle_index >= 0 and choch.candle_index >= 0:
            start = max(0, matching_sweep.candle_index)
            end = min(choch.candle_index + 1, len(df))
            max_disp = 0.0
            for idx in range(start, end):
                candle = df.iloc[idx]
                body = abs(float(candle["close"]) - float(candle["open"]))
                disp = body / atr_value
                if disp > max_disp:
                    max_disp = disp
            if max_disp > displacement_atr:
                displacement_atr = max_disp
                choch.displacement_score = displacement_atr
    else:
        choch.has_sweep_reference = False
        choch.causality_score = 0.0

    # Classify
    is_mss = (
        choch.has_sweep_reference
        and displacement_atr >= 1.0
        and reclaim_bars <= 2
    )

    if is_mss:
        choch.strength = "mss"
        choch.mss_score = calc_mss_score(
            sweep_strength=matching_sweep.strength if matching_sweep else 0.0,
            displacement_atr=displacement_atr,
            reclaim_bars=reclaim_bars,
            volume_ratio=volume_ratio,
            htf_aligned=htf_aligned,
        )
    elif choch.has_sweep_reference and displacement_atr >= 0.5:
        choch.strength = "normal"
        choch.mss_score = calc_mss_score(
            sweep_strength=matching_sweep.strength if matching_sweep else 0.0,
            displacement_atr=displacement_atr,
            reclaim_bars=reclaim_bars,
            volume_ratio=volume_ratio,
            htf_aligned=htf_aligned,
        ) * 0.6  # partial credit
    else:
        choch.strength = "weak"
        choch.mss_score = 0.0

    return choch
''',
'''def select_causal_sweep(sweeps, event, max_causal_bars: int = 5):
    """Same type means bullish low sweep -> bullish structure shift (and mirror)."""
    if event is None or event.candle_index < 0:
        return None
    candidates = [
        s for s in sweeps or []
        if s.is_valid and s.type == event.type and s.candle_index >= 0
        and 0 < event.candle_index - s.candle_index <= max_causal_bars
        and s.candle_index + s.reclaim_candles <= event.candle_index
    ]
    return max(candidates, key=lambda s: s.candle_index, default=None)


def classify_choch(
    choch: CHoCH,
    sweeps: list,
    displacement_atr: float = 0.0,
    reclaim_bars: int = 0,
    volume_ratio: float = 1.0,
    htf_aligned: bool = False,
    max_causal_bars: int = 5,
    df: Optional[pd.DataFrame] = None,
    atr_value: float = 0.0,
) -> CHoCH:
    """Classify using one causally matched sweep and that sweep's reclaim."""
    matching_sweep = select_causal_sweep(sweeps, choch, max_causal_bars)
    choch.strength = "weak"
    choch.mss_score = 0.0
    choch.has_sweep_reference = matching_sweep is not None
    choch.causality_score = 0.0
    choch.displacement_score = 0.0
    choch.reclaim_bars = 0
    if matching_sweep is None:
        return choch

    bars_since = choch.candle_index - matching_sweep.candle_index
    choch.causality_score = calc_causality(bars_since)
    reclaim_bars = matching_sweep.reclaim_candles
    choch.reclaim_bars = reclaim_bars
    if df is not None and atr_value > 0:
        start = matching_sweep.candle_index
        end = choch.candle_index + 1
        if end > len(df):
            return choch
        leg = df.iloc[start:end]
        displacement_atr = float((leg["close"] - leg["open"]).abs().max()) / atr_value
    choch.displacement_score = displacement_atr
    score = calc_mss_score(
        sweep_strength=matching_sweep.strength,
        displacement_atr=displacement_atr,
        reclaim_bars=reclaim_bars,
        volume_ratio=volume_ratio,
        htf_aligned=htf_aligned,
    )
    if displacement_atr >= 1.0 and reclaim_bars <= 2:
        choch.strength = "mss"
        choch.mss_score = score
    elif displacement_atr >= 0.5:
        choch.strength = "normal"
        choch.mss_score = score * 0.6
    return choch
'''),
    ('strategy/pattern_engine.py',
'''        # 1. Sweep required
        has_sweep = False
        sweep_type = None
        sweep_strength = 0.0
        sweep_reclaim = 0
        sweep_candle_index = -1

        valid_sweeps = [s for s in sweeps if s.is_valid]
        if valid_sweeps:
            # Use the NEWEST valid sweep (last in chronological order),
            # not the oldest. Earlier sweeps may be stale/irrelevant.
            s = valid_sweeps[-1]
            has_sweep = True
            sweep_type = s.type
            sweep_strength = s.strength
            sweep_reclaim = s.reclaim_candles
            sweep_candle_index = s.candle_index
''',
'''        from market_structure.structure import select_causal_sweep

        mss = getattr(structure, "last_mss", None)
        if mss is None:
            return ICTSetup(
                detected=False,
                rejection_reason="reversal: no MSS (strong CHoCH)",
            )
        s = select_causal_sweep(sweeps, mss)
        has_sweep = s is not None
        sweep_type = s.type if s is not None else None
        sweep_strength = s.strength if s is not None else 0.0
        sweep_reclaim = s.reclaim_candles if s is not None else 0
        sweep_candle_index = s.candle_index if s is not None else -1
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Production: для обеих сторон matching sweep даёт strength=weak; opposite sweep даёт mss, но reversal отклоняет direction. Предложение проходит mirrored test; после MSS добавленные sweeps не меняют выбранный sweep, same-bar/future/unknown/stale индексы отвергаются; unrelated reclaim и displacement после CHoCH не повышают MSS.

**Integration / tests / limitations:** Это correction семантики стратегии, поэтому paper/shadow и повторный OOS backtest обязательны до live. Не ослаблять causality window ради объёма. Добавить tests/test_mss_sweep_contract.py; пересмотреть старые тесты, кодирующие opposite sweep ошибку. Патч не меняет существующую counter-trend sweep policy для continuation: её прибыльность требует отдельного A/B.

---

### B-007 — Backtest видит будущие закрытия HTF — Severity: CRITICAL

**File:** `backtest/engine.py`, `backtest/resampler.py`
**Lines:** `backtest/engine.py:485-496,781-802; backtest/resampler.py:85-86,108-110`

**Current behavior:** OHLCV помечен OPEN time. .loc[:signal_open] включает уже полностью агрегированный, но на момент решения ещё не закрытый H4/D1/W1. При live-source один текущий HTF bias используется для всех исторических баров.

**Expected behavior:** Сначала вычислить decision_time=primary_open+primary_duration. Каждый HTF bar доступен лишь при open+duration<=decision_time, одинаково для local и downloaded истории. Никакого _htf_once current state в исторических решениях.

**Why it matters:** Решения HTF gate и вероятность используют будущее; A/B, PF и throughput таких backtest нельзя считать причинными доказательствами улучшений. Это не означает, что live scanner сам использует будущее.

**Evidence (verbatim):**
```python
                        try:
                            _ts = df.index[i]
                            _f1h = df.iloc[:i + 1]
                            _f4h = htf.get("4h")
                            _f1d = htf.get("1d")
                            _f1w = htf.get("1w")
                            _htf_result = get_htf_bias_v2(
                                _f1w.loc[:_ts].tail(60) if _f1w is not None and len(_f1w) else None,
                                _f1d.loc[:_ts].tail(60) if _f1d is not None and len(_f1d) else None,
                                _f4h.loc[:_ts].tail(60) if _f4h is not None and len(_f4h) else None,
                                _f1h,
                            )
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('backtest/engine.py',
'''def _normalize_symbol(symbol: str) -> str:
''',
'''def timeframe_duration(timeframe: str) -> pd.Timedelta:
    unit = timeframe[-1].lower()
    seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in seconds or int(timeframe[:-1]) <= 0:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return pd.Timedelta(seconds=int(timeframe[:-1]) * seconds[unit])


def closed_htf_history(frame, timeframe: str, decision_time, limit: int = 60):
    """OHLCV index is OPEN time; a row is usable only after its close."""
    if frame is None or len(frame) == 0:
        return None
    available = frame.index + timeframe_duration(timeframe) <= decision_time
    return frame.loc[available].tail(limit)


def _normalize_symbol(symbol: str) -> str:
'''),
    ('backtest/engine.py',
'''        # Pre-fetch HTF data for bias.
        # local path: full HTF history available → compute per-candle (no look-ahead).
        # live path: fetch current HTF state once (mirrors the live scanner).
        _htf_once: Optional[HTFBiasResult] = None
        if config.htf_bias_v2 and htf is None:
            try:
                df_1d = await exchange_client.fetch_ohlcv(self.symbol, "1d", limit=60)
                df_4h = await exchange_client.fetch_ohlcv(self.symbol, "4h", limit=60)
                df_1w = await exchange_client.fetch_ohlcv(self.symbol, "1w", limit=60)
                _htf_once = get_htf_bias_v2(df_1w, df_1d, df_4h, df)
            except Exception:
                _htf_once = None
''',
'''        # Fetch history once, then evaluate only rows closed at each decision.
        if config.htf_bias_v2 and htf is None:
            htf = {}
            span = df.index[-1] - df.index[0]
            for tf in ("1w", "1d", "4h"):
                needed = 61 + int(span / timeframe_duration(tf))
                history = await exchange_client.fetch_ohlcv_paginated(
                    self.symbol, tf, total_limit=needed,
                )
                if history is None or history.empty:
                    raise RuntimeError(f"Missing {tf} history for causal HTF backtest")
                htf[tf] = history
'''),
    ('backtest/engine.py',
'''                # === Phase 1.45: HTF Bias (per-candle in local path → no look-ahead) ===
                # Local path slices HTF history up to the current candle; the
                # live-fetch path reuses `_htf_once` (mirrors the live scanner,
                # which also reads the current HTF state).
                _htf_result: Optional[HTFBiasResult] = _htf_once
                _htf_penalty = 1.0
                if config.htf_bias_v2:
                    if htf is not None:
                        try:
                            _ts = df.index[i]
                            _f1h = df.iloc[:i + 1]
                            _f4h = htf.get("4h")
                            _f1d = htf.get("1d")
                            _f1w = htf.get("1w")
                            _htf_result = get_htf_bias_v2(
                                _f1w.loc[:_ts].tail(60) if _f1w is not None and len(_f1w) else None,
                                _f1d.loc[:_ts].tail(60) if _f1d is not None and len(_f1d) else None,
                                _f4h.loc[:_ts].tail(60) if _f4h is not None and len(_f4h) else None,
                                _f1h,
                            )
                        except Exception:
                            _htf_result = None
''',
'''                # Signal computation uses the just-closed primary candle.
                decision_time = df.index[i] + timeframe_duration(self.timeframe)
                _htf_result: Optional[HTFBiasResult] = None
                _htf_penalty = 1.0
                if config.htf_bias_v2:
                    _history = htf or {}
                    _htf_result = get_htf_bias_v2(
                        closed_htf_history(_history.get("1w"), "1w", decision_time),
                        closed_htf_history(_history.get("1d"), "1d", decision_time),
                        closed_htf_history(_history.get("4h"), "4h", decision_time),
                        df.iloc[:i + 1] if self.timeframe == "1h" else None,
                    )
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Production при signal open01:00/decision02:00 читает close130 из H4, доступный только04:00. Проверено закрытие точно на границе для H4/D1/W1 и инвариантность результата при изменении future-only row. Нужен end-to-end backtest prefix-invariance на сохранённых исторических данных: изменение всех OHLCV после cutoff не меняет предыдущие сигналы.

**Integration / tests / limitations:** Сначала применить эту HTF-часть, затем дополнение B-007 о resampler. Когда HTF fetch не удался, backtest прерывается явно вместо отчёта с выключенным gate. <55 закрытых HTF свечей остаются unknown по текущему контракту: отмечать warmup coverage в отчёте и не выдавать недостаток истории за подтверждённую нейтральность. OOS сравнения требуют достаточной предыстории. tests/test_backtest_htf_causality.py.

---

#### B-007: дополнение — dropna не удаляет неполные resampled свечи — Severity: CRITICAL

**File:** `backtest/resampler.py`, `backtest/engine.py`
**Lines:** `backtest/resampler.py:71-120; backtest/engine.py:398-404`

**Current behavior:** Агрегация 3×15m создаёт OHLCV без NaN, поэтому текущий resample возвращает якобы полноценную 1h свечу. Аналогично сохраняются неполные первая/последняя неделя и интервалы с пропусками. При dataframe override источник может быть 1h/4h, хотя helper предполагает 15m.

**Expected behavior:** Явно знать source timeframe; принимать bucket только при полном числе исходных свечей, правильных границах, отсутствии duplicates/missing OHLCV. Нельзя строить H4 из D1 путём upsampling.

**Why it matters:** Backtest анализирует бары, отличающиеся от live closed candles; edge buckets и missing history создают ложные структуры/индикаторы. Процент влияния на реальные сигналы без истории неизвестен.

**Evidence (verbatim):**
```python
    resampled = df.resample(
        pandas_rule, closed="left", label="left",
    ).agg(_AGG)

    # dropna() убирает неполные бары (например, последний бар, в который
    # попали не все 4 x 15m свечи — если исходные данные обрываются посередине).
    resampled = resampled.dropna()
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('backtest/resampler.py',
'''def resample_ohlcv(df_15m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Resample a 15m OHLCV DataFrame to *target_tf*.

    Агрегация: open=first, high=max, low=min, close=last, volume=sum.
    Неполные бары (например, последний, не закончившийся на момент исходных
    данных) удаляются через dropna().

    Args:
        df_15m: DataFrame с DatetimeIndex (UTC) и колонками
                open/high/low/close/volume.
        target_tf: один из "1h", "2h", "4h", "1d", "1w".

    Returns:
        pd.DataFrame с тем же DatetimeIndex (UTC) и теми же колонками.
        Индекс — closed="left", label="left": бар 00:00 содержит данные
        с 00:00 (включительно) до 00:59 (исключительно).
    """
    tf = _normalize_target_tf(target_tf)
    pandas_rule = _TF_MAP[tf]

    if df_15m is None or len(df_15m) == 0:
        logger.warning(f"resample_ohlcv: empty input for {target_tf}")
        return df_15m

    df = _ensure_datetime_index(df_15m.copy())

    # Берём только OHLCV-колонки, игнорируем посторонние (taker_buy_volume и т.д.)
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    if not cols:
        raise ValueError(
            f"df_15m must contain at least one of open/high/low/close/volume, "
            f"got columns: {list(df.columns)}"
        )
    df = df[cols]

    # closed="left", label="left" — стандарт для OHLCV:
    # бар [00:00, 01:00) помечается меткой 00:00.
    resampled = df.resample(
        pandas_rule, closed="left", label="left",
    ).agg(_AGG)

    # dropna() убирает неполные бары (например, последний бар, в который
    # попали не все 4 x 15m свечи — если исходные данные обрываются посередине).
    resampled = resampled.dropna()

    logger.debug(
        f"resample_ohlcv: {len(df_15m)} x 15m -> {len(resampled)} x {target_tf} "
        f"({df.index[0]} -> {df.index[-1]})"
    )
    return resampled
''',
'''def resample_ohlcv(
    df_15m: pd.DataFrame,
    target_tf: str,
    source_tf: str = "15m",
) -> pd.DataFrame:
    """Aggregate closed source bars; reject partial buckets and missing samples."""
    tf = _normalize_target_tf(target_tf)
    if df_15m is None or len(df_15m) == 0:
        return df_15m

    def duration(value: str) -> pd.Timedelta:
        units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        unit = value[-1].lower()
        if unit not in units or int(value[:-1]) <= 0:
            raise ValueError(f"Unsupported timeframe: {value}")
        return pd.Timedelta(seconds=int(value[:-1]) * units[unit])

    source_step = duration(source_tf)
    target_step = duration(tf)
    if source_step > target_step or target_step.value % source_step.value:
        raise ValueError(f"Cannot aggregate {source_tf} into {tf}")
    expected = target_step.value // source_step.value
    df = _ensure_datetime_index(df_15m.copy()).sort_index()
    cols = ["open", "high", "low", "close", "volume"]
    if not set(cols).issubset(df.columns):
        raise ValueError("All OHLCV columns are required")
    if df.index.has_duplicates:
        raise ValueError("Duplicate source candle timestamps")
    if any(ts.value % source_step.value != 0 for ts in df.index):
        raise ValueError(f"Source candles are not aligned to {source_tf}")
    df = df[cols]
    rule = _TF_MAP[tf]
    grouped = df.resample(rule, closed="left", label="left")
    result = grouped.agg(_AGG)
    counts = grouped.count().min(axis=1)
    times = pd.Series(df.index, index=df.index).resample(
        rule, closed="left", label="left",
    )
    first = times.first()
    last = times.last()
    complete = (
        (counts == expected)
        & (first == result.index)
        & (last + source_step == result.index + target_step)
    )
    return result.loc[complete].dropna()
'''),
    ('backtest/engine.py',
'''        src = htf_base if htf_base is not None else df
        htf: dict = {}
        for tf in ("1w", "1d", "4h"):
            if tf == self.timeframe:
                htf[tf] = df
            else:
                htf[tf] = resample_ohlcv(src, tf)
''',
'''        src = htf_base if htf_base is not None else df
        source_tf = "15m" if htf_base is not None else self.timeframe
        htf: dict = {}
        for tf in ("1w", "1d", "4h"):
            if tf == self.timeframe:
                htf[tf] = df
            elif timeframe_duration(source_tf) > timeframe_duration(tf):
                htf[tf] = None
            else:
                htf[tf] = resample_ohlcv(src, tf, source_tf=source_tf)
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Production 3×15m ->1×1h; предложение ->0. Проверены 3/4/7/8 samples, missing внутренний бар, частичный первый bucket, полный Monday week+частичный хвост, source1h->4h и запрет source1d->4h.

**Integration / tests / limitations:** Зависит от timeframe_duration из B-007 (HTF). htf_base по существующему API — исходный 15m frame; для другого source_tf требуется передать явный формат либо расширить API. Старые tests/test_resampler.py, ожидающие неполную W1, обновить осознанно, не возвращать прежнее поведение ради зелёных тестов.

---


### B-008 — Трекер использует некаузальные свечи и теряет касания — Severity: CRITICAL

**File:** `scheduler/outcome_tracker.py`; новый `scheduler/outcome_window.py`.
**Lines:** 157–194, 217–244, 288, 333; `scheduler/scanner.py:1013–1025`.
**Current behavior:** `entry_candle_open` — время последней закрытой аналитической свечи, а не фактического входа. Трекер рассматривает весь текущий бар 1h/4h, включая фитиль до входа; читает только `iloc[-1]`, пропускает предыдущий бар; заменяет ticker свечными экстремумами; при TP и SL одновременно выбирает TP.
**Expected behavior:** последовательно проверять доступные минутные наблюдения после входа, восстанавливать интервал после последней проверки, отдельно учитывать текущий ticker; неоднозначный порядок внутри минуты учитывать консервативно. Не использовать аналитическую свечу как время исполнения.
**Why it matters:** воспроизведены ложный TP по предшествующему входу фитилю; пропуск SL на предыдущем баре; сохранение OPEN при ticker=94, SL=95; TP при high=111/low=94 и TP=110/SL=95. Это ошибки виртуальной разметки; историческую частоту по имеющимся данным определить нельзя.

**Evidence:**
```python
last_candle = candle_df.iloc[-1]
candle_high = float(last_candle["high"])
candle_low = float(last_candle["low"])
```

**Fix:** создать `scheduler/outcome_window.py` со следующим полным содержимым:
```python
from datetime import datetime, timezone
from math import ceil, floor, isfinite


def as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def first_touch(signal, bars, created_at, now, current_price):
    """Virtual fills; SL first only when both barriers share one minute."""
    created_at, now = as_utc(created_at), as_utc(now)
    observations = []
    for opened, row in bars:
        opened = as_utc(opened)
        if opened < created_at or opened > now:
            continue
        observations.append((float(row["high"]), float(row["low"])))
    # A ticker is observed after the historical bars; never overwrite it.
    observations.append((current_price, current_price))
    for high, low in observations:
        if not all(isfinite(x) and x > 0 for x in (high, low)):
            raise ValueError("invalid outcome observation")
        if signal.signal_type == "BUY":
            hit_sl, hit_tp = low <= signal.sl, high >= signal.tp
        else:
            hit_sl, hit_tp = high >= signal.sl, low <= signal.tp
        if hit_sl or hit_tp:
            return bool(hit_tp and not hit_sl), bool(hit_sl), high, low
    return False, False, current_price, current_price


async def load_outcome_window(client, signal, outcome, now):
    """Read every available minute from the last check, with overlap."""
    created_at = as_utc(signal.created_at)
    checked_at = as_utc(outcome.checked_at) if outcome.checked_at else created_at
    # Include a full entry-minute only when entry coincides with its opening.
    start_ms = max(ceil(created_at.timestamp() / 60) * 60000,
                   floor(min(checked_at, now).timestamp() / 60) * 60000)
    last_ms = floor(now.timestamp() / 60) * 60000
    bars = []
    cursor = start_ms
    while cursor <= last_ms:
        frame = await client.fetch_ohlcv(
            signal.symbol, "1m", limit=1000, since=cursor, drop_last=False,
        )
        if frame is None or frame.empty:
            raise ValueError("outcome history unavailable")
        progressed = False
        for opened, row in frame.sort_index().iterrows():
            stamp = int(opened.timestamp() * 1000)
            if stamp < cursor or stamp > last_ms:
                continue
            if stamp != cursor:
                raise ValueError("gap in outcome minute history")
            bars.append((opened.to_pydatetime(), row))
            cursor += 60000
            progressed = True
        if not progressed:
            raise ValueError("outcome history did not advance")
    return bars
```

В `scheduler/outcome_tracker.py` добавить импорт:
```python
from scheduler.outcome_window import first_touch, load_outcome_window
```

Удалить блок от комментария `# Skip if current candle is the same as the entry candle.` до следующего `# Skip symbols on cooldown after repeated fetch failures`. Блок от `# Also check recent candle high/low` до комментария `# Calculate hold bars for ScenarioMemory` заменить следующим кодом внутри цикла `for outcome in outcomes`:
```python
try:
    bars = await load_outcome_window(exchange_client, signal, outcome, now)
    hit_tp, hit_sl, candle_high, candle_low = first_touch(
        signal, bars, signal.created_at, now, float(current_price),
    )
except (ValueError, TypeError, OverflowError) as exc:
    logger.warning(f"Outcome history incomplete: signal={signal.id}: {exc}")
    continue
```

Оба вызова `_send_close_notification` в ветках HIT_TP и HIT_SL передавать с `close_price`, а не `current_price`; это та же цена, которая сохранена в БД:
```python
await _send_close_notification(signal, "HIT_TP", close_price, net_pnl)
```
```python
await _send_close_notification(signal, "HIT_SL", close_price, net_pnl)
```

**Ограничение исправления:** это консервативный монитор виртуальных исходов, не доказательство биржевого исполнения. OHLCV первой неполной минуты не позволяет отличить фитиль до входа от фитиля после него: helper исключает такую минуту и принимает только наблюдённый ticker. Реальный spike между входом и первой проверкой может остаться неизвестным. В минуте, где задеты оба барьера, SL-first — явно выбранная модель, а не известная последовательность сделок. Для точного восстановления нужны trade tape/биржевые fills; не использовать эти интервалы как подтверждённые метки ML. При разрыве истории проверка не продвигает checked_at и не освобождает риск. Исторические строки не переписывать этим алгоритмом автоматически. `closed_at` пока означает время обнаружения; funding оценивается до обнаружения, не до неизвестного точного времени касания.

**Verification:** семь изолированных тестов helper: обе границы; ticker за SL при свече внутри; TP в ранней минуте и SL в поздней; фитиль до входа; SELL; pagination; разрыв истории. Все семь прошли. Добавить интеграционные тесты в `tests/test_outcome_tracker.py` с mock обеих API, проверкой `since`, восстановления после рестарта и отсутствия продвижения checked_at на неполной истории. Старые тесты мокируют только ticker: дополнить mock OHLCV, иначе они зависят от внешней сети. Выполнять совместно с B-009.

---


### B-009 — EXPIRED закрывает по цене входа с нулевым PnL — Severity: CRITICAL

**File:** `scheduler/outcome_tracker.py`; `storage/database.py`.
**Lines:** 148–155, 334–339; `storage/database.py:1188–1196`; `config/settings.py:728`.
**Current behavior:** после 7 дней позиция закрывается до получения рыночной цены, с `close_price=entry` и `pnl_pct=0`. Девять таких строк есть в базе; сигнал может быть как убыточным, так и прибыльным. `MAX_TRADE_DURATION_BARS=72` применяется в backtest, но отсутствует в live tracker.
**Expected behavior:** проверить доступные касания до закрытия; если касаний нет, зафиксировать текущую цену и net PnL. Не превращать отсутствие наблюдения в безубыточную сделку. Для live оставить существующий TTL 7 дней; перенос 72-bar policy проверять отдельным A/B, не смешивать с исправлением бухгалтерии.
**Why it matters:** fake breakeven искажают WR/PF и длительность занятости риска. Воспроизведение: после 8 дней entry=100/ticker=96 текущий код пишет EXPIRED по 100 с PnL=0. Нельзя утверждать, что четыре OPEN в снимке просрочены: их возраст на дату снимка ниже 7 дней.

**Evidence:**
```python
if age > timedelta(days=OUTCOME_TTL_DAYS):
    await db.close_outcome(
        outcome.id, "EXPIRED",
        close_price=signal.close_price, pnl_pct=0.0,
    )
    continue
```

**Fix:** удалить ранний блок 148–155. В самом конце текущей конструкции `if hit_tp / elif hit_sl / else` заменить только тело последнего `else` (строки 335–339, включая закрывающую скобку старого logger.debug) следующим кодом, сохранив сам `else`:
```python
if now - signal.created_at.replace(tzinfo=timezone.utc) > timedelta(days=OUTCOME_TTL_DAYS):
    close_price = float(current_price)
    gross_pnl, net_pnl = _calculate_net_pnl(
        signal.signal_type, signal.close_price, close_price,
        signal.created_at.replace(tzinfo=timezone.utc), now,
    )
    await db.close_outcome(outcome.id, "EXPIRED", close_price, net_pnl)
    await db.update_candidate_outcome_by_signal(signal.id, "EXPIRED", net_pnl)
    from storage.database import DecisionTrace
    from sqlalchemy import select
    async with db._session_factory() as session:
        rows = (await session.execute(
            select(DecisionTrace).where(DecisionTrace.signal_id == signal.id)
        )).scalars().all()
        for row in rows:
            row.outcome = "EXPIRED"
            row.pnl_pct = net_pnl
        await session.commit()
    logger.info(
        f"Outcome EXPIRED: signal={signal.id} price={close_price} net={net_pnl:+.2f}%"
    )
else:
    await db.touch_outcome_checked(outcome.id, checked_at=now)
```

Полностью заменить метод `Database.touch_outcome_checked` (`storage/database.py:1188–1196`) следующим определением с отступом 4 пробела внутри класса. `Optional`, `datetime`, `timezone`, `select` и `SignalOutcome` уже импортированы/определены. Сохраняется прежний вызов без второго аргумента для остальных клиентов:
```python
async def touch_outcome_checked(
    self, outcome_id: int, checked_at: Optional[datetime] = None,
) -> None:
    """Persist the observation cutoff, not the later network completion time."""
    observed_at = checked_at if checked_at is not None else datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    else:
        observed_at = observed_at.astimezone(timezone.utc)
    async with self._session_factory() as session:
        result = await session.execute(
            select(SignalOutcome).where(SignalOutcome.id == outcome_id)
        )
        row = result.scalar_one()
        if row.status != "OPEN":
            return
        row.checked_at = observed_at
        await session.commit()
```

Это обязательная часть ROOT-01/B-009: `now` фиксирует границу запрошенного интервала. Если API отвечает несколько минут, запись текущего времени при завершении вместо `now` пропустит ещё не проверенные минуты в следующем цикле. Значение `checked_at` здесь — покрытая граница наблюдения, а не время завершения запроса. Незакрытая текущая минута будет запрошена повторно благодаря overlap.

**Verification:** expired BUY 100→96 фиксирует около −4% минус costs; expired SELL 100→96 — около +4% минус costs; SL до TTL сохраняет HIT_SL; unavailable ticker/history оставляет OPEN; повторный проход не закрывает ещё раз. Независимая проверка собрала полные outcome_tracker.py / outcome_window.py / database.py по этим инструкциям вне production: все три компилируются. Девять дополнительных integration/watermark тестов прошли; вместе с семью helper-тестами — 16 passed. Проверены сохранение observation cutoff, совместимость старого вызова touch без второго аргумента и отсутствие обновления уже закрытой строки. Существующий тест EXPIRED дополнить проверкой цены и PnL. Нужен алерт на ошибки истории/длительные OPEN: не удалять их для искусственного освобождения лимита. Девять исторических EXPIRED восстановить только из исходных котировок/fills; до восстановления пометить отдельной категорией качества данных в аналитике.

---


### B-010 — WR считается по статусу, а ценовой PnL называется equity — Severity: MEDIUM

**File:** `storage/database.py`, `analytics/daily_report.py`, `analytics/performance.py`, `analytics/full_report.py`.
**Lines:** database1198–1216; daily109–132,169–187; performance587–600,686–694; full_report198–215,237–238,632–633.
**Current behavior:** wins — число HIT_TP, хотя девять таких строк убыточны; manual/zero/unknown считаются непоследовательно. `cumsum(pnl_pct)` подписан Equity Curve, хотя это сумма ценовых процентов разных позиций.
**Expected behavior:** WR по знаку известного конечного net PnL с явно указанным знаменателем; TP events отдельно. Обозначать сумму ценовых доходностей в п.п., не как equity. Existing event-only аналитике сохранить scope, но явно подписать его; полная статистика БД включает все закрытия.
**Why it matters:** неверная цель оптимизации может привести к повышению риска у проигрывающей системы. Корректный raw PF0.5846 и WR25.90% всех139 либо28.35% ненулевых отличаются от prompt; это всё ещё оценки по сохранённым виртуальным исходам с ограничениями B-008/B-009.

**Evidence:**
```python
"wins": sum(1 for r in closed_rows if r.status == "HIT_TP"),
```
```python
cumulative_pnl = df_sorted["pnl_pct"].cumsum()
ax.set_title("Equity Curve (Cumulative PnL %)", fontsize=14, fontweight="bold")
```

**Fix:** заменить целиком `Database.get_outcome_stats` (внутри класса):
```python
async def get_outcome_stats(self) -> dict:
    import math

    async with self._session_factory() as session:
        result = await session.execute(select(SignalOutcome))
        rows = list(result.scalars().all())
    closed_rows = [r for r in rows if r.status != "OPEN"]
    pnls = [
        float(r.pnl_pct) for r in closed_rows
        if r.pnl_pct is not None and math.isfinite(float(r.pnl_pct))
    ]
    wins = sum(p > 0 for p in pnls)
    losses = sum(p < 0 for p in pnls)
    zero = sum(p == 0 for p in pnls)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "closed": len(closed_rows),
        "open": sum(r.status == "OPEN" for r in rows),
        "observed": len(pnls),
        "unknown_pnl": len(closed_rows) - len(pnls),
        "wins": wins,
        "losses": losses,
        "zero_pnl": zero,
        "tp_events": sum(r.status == "HIT_TP" for r in closed_rows),
        "winrate_observed_pct": 100.0 * wins / len(pnls) if pnls else None,
        "winrate_nonzero_pct": 100.0 * wins / (wins + losses) if wins + losses else None,
        "price_pnl_profit_factor": gross_profit / gross_loss if gross_loss else None,
        "price_pnl_sum_pp": sum(pnls),
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        "best_pnl": max(pnls) if pnls else 0.0,
        "worst_pnl": min(pnls) if pnls else 0.0,
    }
```

Затем применить следующие точные замены отображения и счётчиков. Они не меняют исходные исторические outcomes и не превращают proxy в account return.

#### analytics/daily_report.py — finite inputs and undefined PF

Add `import math` beside the existing imports. Replace full `_calc_profit_factor` at lines 79–85 with:

```python
def _calc_profit_factor(pnls: list[float]) -> float | None:
    values = [float(p) for p in pnls if p is not None and math.isfinite(float(p))]
    gains = sum(p for p in values if p > 0)
    losses = -sum(p for p in values if p < 0)
    return gains / losses if losses > 0 else None
```

None means PF is undefined because no observed loss exists (including empty/all-zero input); format it as an em dash, never NaN/Infinity. Apply all three formatting updates below together.

#### analytics/daily_report.py:108–125

Replace the contiguous block beginning `pnls = [o.pnl_pct ... closed ...]` and ending `lines.append("")` immediately before `# Details table` with this block, preserving 8-space function/else indentation:

```python
        pnls = [float(o.pnl_pct) for o, s in closed if o.pnl_pct is not None and math.isfinite(float(o.pnl_pct))]
        wins = sum(p > 0 for p in pnls)
        losses_count = sum(p < 0 for p in pnls)
        zero = sum(p == 0 for p in pnls)
        unknown = len(closed) - len(pnls)
        expired = sum(o.status == "EXPIRED" for o, s in closed)
        tp_events = sum(o.status == "HIT_TP" for o, s in closed)
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        pf = _calc_profit_factor(pnls)
        pf_text = f"{pf:.2f}" if pf is not None else "—"
        wr_text = f"{wins / len(pnls) * 100:.1f}%" if pnls else "—"
        lines.append("| Метрика | Значение |")
        lines.append("|---------|----------|")
        lines.append(f"| Всего закрыто | {len(closed)} |")
        lines.append(f"| Известный PnL / неизвестный | {len(pnls)} / {unknown} |")
        lines.append(f"| WIN (PnL > 0) | {wins} |")
        lines.append(f"| LOSS (PnL < 0) | {losses_count} |")
        lines.append(f"| Нулевой сохраненный PnL | {zero} |")
        lines.append(f"| События HIT_TP | {tp_events} |")
        lines.append(f"| EXPIRED (закрытие требует сверки) | {expired} |")
        lines.append(f"| Винрейт по известному PnL | {wr_text} |")
        lines.append(f"| Средний ценовой PnL | {avg_pnl:+.2f}% |")
        lines.append(f"| Ценовой Profit Factor | {pf_text} |")
        lines.append("")
```

At line 132 replace:

```python
pnl_str = f"{outcome.pnl_pct:+.2f}%" if outcome.pnl_pct else "—"
```

with:

```python
pnl_str = f"{outcome.pnl_pct:+.2f}%" if outcome.pnl_pct is not None and math.isfinite(float(outcome.pnl_pct)) else "—"
```

#### analytics/daily_report.py:169–187

Replace contiguous block beginning `all_pnls = ...` and ending `lines.append("")` immediately before `# Breakdown by TF` with:

```python
        all_pnls = [float(o.pnl_pct) for o, s in all_closed if o.pnl_pct is not None and math.isfinite(float(o.pnl_pct))]
        all_wins = sum(p > 0 for p in all_pnls)
        all_losses = sum(p < 0 for p in all_pnls)
        all_zero = sum(p == 0 for p in all_pnls)
        all_unknown = len(all_closed) - len(all_pnls)
        all_tp_events = sum(o.status == "HIT_TP" for o, s in all_closed)
        all_expired = sum(o.status == "EXPIRED" for o, s in all_closed)
        all_avg = sum(all_pnls) / len(all_pnls) if all_pnls else 0.0
        all_pf = _calc_profit_factor(all_pnls)
        all_pf_text = f"{all_pf:.2f}" if all_pf is not None else "—"
        all_best = max(all_pnls) if all_pnls else 0.0
        all_worst = min(all_pnls) if all_pnls else 0.0
        all_wr_text = f"{all_wins / len(all_pnls) * 100:.1f}%" if all_pnls else "—"
        lines.append("| Метрика | Значение |")
        lines.append("|---------|----------|")
        lines.append(f"| Всего закрыто | {len(all_closed)} |")
        lines.append(f"| Известный PnL / неизвестный | {len(all_pnls)} / {all_unknown} |")
        lines.append(f"| WIN (PnL > 0) | {all_wins} |")
        lines.append(f"| LOSS (PnL < 0) | {all_losses} |")
        lines.append(f"| Нулевой сохраненный PnL | {all_zero} |")
        lines.append(f"| События HIT_TP | {all_tp_events} |")
        lines.append(f"| EXPIRED (закрытие требует сверки) | {all_expired} |")
        lines.append(f"| Винрейт по известному PnL | {all_wr_text} |")
        lines.append(f"| Средний ценовой PnL | {all_avg:+.2f}% |")
        lines.append(f"| Ценовой Profit Factor | {all_pf_text} |")
        lines.append(f"| Лучшая | {all_best:+.2f}% |")
        lines.append(f"| Худшая | {all_worst:+.2f}% |")
        lines.append("")
```

#### analytics/daily_report.py — timeframe summary

Replace the two-line `if outcome.pnl_pct is not None:` block at lines 198–199 with:

```python
if outcome.pnl_pct is not None and math.isfinite(float(outcome.pnl_pct)):
    tf_stats[tf].append(float(outcome.pnl_pct))
```

Replace the contiguous block from `pf_tf = _calc_profit_factor(pnls_tf)` through the following TF `lines.append` with:

```python
                pf_tf = _calc_profit_factor(pnls_tf)
                pf_tf_text = f"{pf_tf:.2f}" if pf_tf is not None else "—"
                wr_tf = wins_tf / total_tf * 100 if total_tf else 0.0
                lines.append(f"| {tf} | {total_tf} | {wr_tf:.1f}% | {avg_tf:+.2f}% | {pf_tf_text} |")
```

#### analytics/performance.py — preserve event-only query, replace format functions

Replace full `format_overall_stats` at 587–600:

```python
def format_overall_stats(s: WinrateStats, period_label: str = "all time") -> str:
    import math
    pf_text = f"{s.profit_factor:.2f}" if s.losses and math.isfinite(s.profit_factor) else "—"
    if s.total == 0:
        return f"Нет исходов HIT_TP/HIT_SL ({period_label})"
    return (
        f"<b>HIT_TP/HIT_SL only ({period_label})</b>\n"
        "MANUAL_CLOSE и EXPIRED исключены из PnL/WR.\n"
        f"Всего: <b>{s.total}</b> (прибыль: {s.wins} | убыток: {s.losses}; "
        f"EXPIRED отдельно: {s.expired})\n"
        f"Winrate по знаку PnL: <b>{s.winrate:.1f}%</b>\n"
        f"Ценовой Profit Factor: <b>{pf_text}</b>\n"
        f"Expectancy: <b>{s.expectancy_r:+.3f}R</b>\n"
        f"Сумма ценовых доходностей: <b>{s.net_pnl_pct:+.2f} п.п.</b>\n"
        f"Просадка этой суммы: <b>{s.max_drawdown_pct:.2f} п.п.</b>\n"
        "Эти суммы не являются доходностью или просадкой капитала.\n"
        f"Avg Win: <b>{s.avg_win_pct:+.2f}%</b> | Avg Loss: <b>{s.avg_loss_pct:+.2f}%</b>\n"
        f"Best: <b>{s.best_trade_pct:+.2f}%</b> | Worst: <b>{s.worst_trade_pct:+.2f}%</b>"
    )
```

Replace full `format_stats_telegram` at 686–694:

```python
def format_stats_telegram(s: WinrateStats, period_label: str) -> str:
    import math
    pf_text = f"{s.profit_factor:.1f}" if s.losses and math.isfinite(s.profit_factor) else "—"
    if s.total == 0:
        return f"Нет исходов HIT_TP/HIT_SL ({period_label})"
    return (
        f"<b>{period_label}</b>: {s.total} исходов HIT_TP/HIT_SL | "
        f"WR по PnL <b>{s.winrate:.0f}%</b> | ценовой PF <b>{pf_text}</b> | "
        f"Exp <b>{s.expectancy_r:+.2f}R</b> | "
        f"Сумма ценовых PnL <b>{s.net_pnl_pct:+.1f} п.п.</b> | "
        f"Просадка суммы <b>{s.max_drawdown_pct:.1f} п.п.</b>\n"
        "Без MANUAL_CLOSE/EXPIRED; не доходность капитала."
    )
```

Do not make speculative database predicate or expectancy changes under this finding. Test the scope label explicitly, and keep existing arithmetic behavior until separate intended scope migration.

#### analytics/full_report.py — exact display edits

Replace lines 213–214:

```python
ax.set_title("Equity Curve (Cumulative PnL %)", fontsize=14, fontweight="bold")
ax.set_ylabel("Cumulative PnL %")
```

with:

```python
ax.set_title("Sum of price returns (not account equity)", fontsize=14, fontweight="bold")
ax.set_ylabel("Price-return sum, percentage points")
```

Replace lines 237–238:

```python
ax.set_title("Drawdown (%)", fontsize=14, fontweight="bold")
ax.set_ylabel("Drawdown %")
```

with:

```python
ax.set_title("Drawdown of price-return sum (not account drawdown)", fontsize=14, fontweight="bold")
ax.set_ylabel("Drawdown of sum, percentage points")
```

In graph descriptor list at 632–633 replace the two tuple expressions:

```python
("equity_curve.png", "Sum of price returns (not account equity)", plot_equity_curve),
("drawdown.png", "Drawdown of price-return sum", plot_drawdown),
```

Keep filenames stable to avoid breaking generated-report references; names do not redefine the accounting.


**Verification:** fixture HIT_TP=-2, HIT_TP=3, MANUAL_CLOSE=1, HIT_SL=-1, EXPIRED=0, MANUAL_CLOSE=None, OPEN: closed6, observed5, unknown1, wins2, losses2, zero1, TP events2; WR observed40%, nonzero50%, PF4/3. Проверить empty/no-loss/NaN и однозначную подпись scope. На копии snapshot получить36/91/12 и PF0.584553. До reconciliation нулевые EXPIRED имеют только сохранённое значение, а не доказанное закрытие без прибыли/убытка.


---


### B-011 — V2 gates/features теряются, downstream-аналитика использует legacy pipeline — MEDIUM

**File:** `storage/trace.py`:163–165,215–238,272; `storage/database.py`:869–974; `scheduler/scanner.py`:1108–1117.

**Current behavior:** build_gate_path итерирует старый короткий GATE_ORDER, поэтому htf_bias/compression_regime/direction_filter/portfolio_risk_recheck теряются из пути. Legacy boolean columns новых gates тоже не имеют. `get_trace_stats` требует True во всех gates старого pipeline, вследствие чего даже прибыльный v2 signal даёт WR/PF=None. set_features заменяет, а не дополняет прежний snapshot; из финальных live keys сохраняются только atr_pct/context_score. P(TP), quality, risk и component count нет в trace. `save` ловит любые ошибки на DEBUG.

**Expected behavior:** сохранять фактическую последовательность записанных gates, полный whitelist feature JSON, дополнять snapshot, агрегировать реальные visited gates; trace failure виден оператору.

**Why it matters:** без этого невозможно различить три причины portfolio_risk, обосновать увеличение лимита и оценить качество прошедших/отброшенных сигналов. Невыполненный gate не является PASS и не является BLOCK. Fresh SQLite `trace.save` на текущем commit успешно создаёт row; утверждение о пустой server table требует проверки deployed commit, DB path, migrations/permissions/logs. Не объявлять доказанной причиной пустоты несовместимые kwargs: репродукция этого не показала.

**Evidence:**

```python
    def set_features(self, features: dict) -> None:
        """Set feature snapshot — only known keys are kept."""
        self._features = {k: v for k, v in features.items() if k in FEATURE_KEYS}
```


```python
        path = []
        for gate in GATE_ORDER:
            result = self._gates.get(gate)
            if result is None:
                continue
            status = "PASS" if result else "BLOCK"
            entry = f"{gate}:{status}"
            if not result and self._final_stage == gate and self._blocked_reason:
                entry += f":{self._blocked_reason[:80]}"
            path.append(entry)
        # Also add compression_block if recorded (not in canonical GATE_ORDER)
        if "compression_block" in self._gates:
            status = "PASS" if self._gates["compression_block"] else "BLOCK"
            path.append(f"compression_block:{status}")
        return json.dumps(path)

    async def save(
        self,
```


```python
            downstream_signals = []
            for t in traces:
                if getattr(t, col, None) is not True:
                    continue
                all_later_pass = True
                for later_gate in gate_order[gate_idx + 1:]:
                    later_col = gate_col_map[later_gate]
                    later_val = getattr(t, later_col, None)
                    if later_val is not True:
                        all_later_pass = False
                        break
                if all_later_pass and t.signal_generated:
                    downstream_signals.append(t)
```


**Fix:** заменить `DecisionTraceBuilder.set_features`, `DecisionTraceBuilder.build_gate_path`, `Database.get_trace_stats` полными методами ниже. Первые два относятся к storage/trace.py, третий к storage/database.py; соответствующие imports уже есть. Порядок отчёта — порядок первого наблюдения gates в traces; для визуального фиксированного pipeline можно отдельно сортировать при отображении, но нельзя терять неизвестные gates.


```python
def set_features(self, features: dict) -> None:
    """Keep earlier snapshots and all explicitly registered feature names."""
    self._features.update({key: value for key, value in features.items() if key in FEATURE_KEYS})


def build_gate_path(self) -> str:
    """Preserve actual execution order, including gates absent from old schemas."""
    path = []
    for gate, passed in self._gates.items():
        if passed is None:
            continue
        value = f"{gate}:{'PASS' if passed else 'BLOCK'}"
        if not passed and gate == self._final_stage and self._blocked_reason:
            value += f":{self._blocked_reason[:80]}"
        path.append(value)
    return json.dumps(path)


async def get_trace_stats(self, symbol: Optional[str] = None) -> list[dict]:
    """Aggregate recorded gates; skipped legacy gates do not veto v2 outcomes."""
    import math
    async with self._session_factory() as session:
        query = select(DecisionTrace)
        if symbol:
            query = query.where(DecisionTrace.symbol == symbol)
        traces = list((await session.execute(query.order_by(DecisionTrace.id))).scalars().all())

    columns = [column.name for column in DecisionTrace.__table__.columns
               if column.name.startswith("gate_") and column.name != "gate_path"]
    parsed = []
    order = []
    for trace in traces:
        gates = {}
        if trace.gate_path:
            try:
                path = json.loads(trace.gate_path)
                if isinstance(path, list):
                    for item in path:
                        if not isinstance(item, str):
                            continue
                        parts = item.split(":", 2)
                        if len(parts) >= 2 and parts[1] in ("PASS", "BLOCK"):
                            gates[parts[0]] = parts[1] == "PASS"
            except (ValueError, TypeError):
                pass
        for column in columns:
            value = getattr(trace, column, None)
            if value is not None:
                gates.setdefault(column[5:], bool(value))
        if not trace.signal_generated and trace.final_stage:
            gates[trace.final_stage] = False
        for gate in gates:
            if gate not in order:
                order.append(gate)
        parsed.append((trace, gates))

    stats = []
    for gate in order:
        entered = [(trace, gates[gate]) for trace, gates in parsed if gate in gates]
        downstream = [trace for trace, passed in entered if passed and trace.signal_generated]
        closed = [trace for trace in downstream
                  if trace.outcome in ("HIT_TP", "HIT_SL", "MANUAL_CLOSE", "EXPIRED")
                  and trace.pnl_pct is not None and math.isfinite(trace.pnl_pct)]
        wins = sum(trace.pnl_pct > 0 for trace in closed)
        pnls = [trace.pnl_pct for trace in closed]
        profit = sum(pnl for pnl in pnls if pnl > 0)
        loss = -sum(pnl for pnl in pnls if pnl < 0)
        passed = sum(bool(value) for _, value in entered)
        stats.append({
            "gate": gate, "entered": len(entered), "passed": passed,
            "dropped": len(entered) - passed,
            "wr_downstream": round(wins / len(closed) * 100, 1) if closed else None,
            "pf_downstream": round(profit / loss, 2) if loss > 0 else None,
        })
    return stats
```


Для сохранения новых features выполнить все четыре точные вставки (SQLite production; PostgreSQL нуждается в штатной миграции вне PRAGMA):

A. В storage/trace.py после определения FEATURE_KEYS добавить:


```python
FEATURE_KEYS.update({
    "components", "overall_quality", "setup_confidence", "mtf_aligned",
    "p_tp", "risk_pct", "expected_rr", "setup_type", "git_sha",
})
```


B. В класс DecisionTrace (`storage/database.py`, рядом с execution_snapshot) добавить:


```python
feature_snapshot = Column(Text, nullable=True)
```


C. В Database._migrate после закрытия dict trace_migrations, до for col_name,col_type, добавить:


```python
trace_migrations["feature_snapshot"] = "TEXT"
```


D. В Database.save_decision_trace внутри constructor DecisionTrace сразу после candidate_id=candidate_id добавить argument:


```python
feature_snapshot=json.dumps(f, default=str),
```


E. В scanner финальный trace feature `"expected_rr": 0.0` заменить на `"expected_rr": risk_decision.rr_ratio`. Не хранить заведомый ноль. Сразу после создания trace в scan_symbol_v2 и `_config_snapshot = build_config_snapshot()` вставить:

```python
import os
trace.set_version(VERSION, _config_snapshot)
trace.set_features({"git_sha": os.getenv("BOT_GIT_SHA", "unknown")})
```

В `trace.set_features` после успешного pattern_engine gate (scanner379–383), до Direction/Symbol filter, заменить весь вызов:

```python
trace.set_features({
    "components": setup.components_count,
    "overall_quality": setup.overall_quality,
    "setup_confidence": setup.setup_confidence,
    "setup_type": setup.setup_type,
})
```

Так Phase0 early returns получат version/config, а обнаруженный setup_type не потеряется на позднем фильтре. VERSION2.7.0 — строка версии приложения, не Git SHA. При развёртывании задавать BOT_GIT_SHA полным SHA реально развёрнутого checkout; unknown оставлять явно неизвестным, не подставлять SHA аудита. В тесте merge snapshots проверить сохранение setup_type/git_sha после следующего set_features с p_tp/risk_pct.

В `config/settings.py:build_config_snapshot` непосредственно перед `return _json.dumps(snapshot, sort_keys=True)` добавить недостающие параметры admission/execution/новых паттернов (существующий snapshot сохранить):

```python
snapshot.update({
    "max_portfolio_risk_pct": config.max_portfolio_risk_pct,
    "max_active_signals": config.max_active_signals,
    "max_active_signals_per_symbol": config.max_active_signals_per_symbol,
    "primary_timeframes": config.trading.primary_timeframes,
    "max_sl_atr": config.trading.max_sl_atr,
    "symbol_overrides": config.trading.symbol_overrides,
    "max_sweep_age_bars": config.pattern_engine.max_sweep_age_bars,
    "max_fvg_age_candles": config.pattern_engine.max_fvg_age_candles,
    "poi_entry_enabled": config.pattern_engine.poi_entry_enabled,
    "poi_min_quality": config.pattern_engine.poi_min_quality,
    "htf_bias_v2": config.htf_bias_v2,
    "htf_hard_gate": config.htf_hard_gate,
    "htf_bias_continuation_penalty": config.htf_bias_continuation_penalty,
    "require_entry_zone": config.require_entry_zone,
    "min_p_tp": config.min_p_tp,
    "execution_model": config.execution_model,
    "execution_pending_max_bars": config.execution_pending_max_bars,
    "max_trade_duration_bars": config.max_trade_duration_bars,
    "signal_cooldown_minutes": config.signal_cooldown_minutes,
    "signal_cooldown_tf_multiplier": config.signal_cooldown_tf_multiplier,
})
```

Не добавлять Telegram/API secrets или весь environment в snapshot. Наличие настройки `poi_min_quality` в snapshot не означает, что текущий PatternEngine её применяет: этот параметр сохраняется, но в HEAD не используется в gate. Настройку качества POI уточнять/проверять отдельно, не объявлять уже действующим фильтром.
F. В trace.save заменить единственную строку DEBUG логирования на следующий error вызов; return -1 сохранить, чтобы сбой телеметрии не создавал повторные сигналы после successful admission:


```python
logger.error(f"Decision trace save failed for {self.symbol} {self.timeframe}: {e}")
```


**Verification:** source reproduction: сохранение fresh SQLite id1 успешно; htf_bias absent gate_path; retained features only atr_pct/context_score; successful HIT_TP v2 trace даёт downstream WR=None. `verify_fixes.py` после proposed patch сохраняет htf_bias, components/quality/p_tp/risk JSON, cumulative snapshots; trace stats для TP+2 и SL−1 => WR50, PF2. Legacy bool traces по-прежнему читаются. WR/PF считают знак конечного net pnl_pct, не ярлык HIT_TP/HIT_SL; учитывают HIT_TP, HIT_SL, MANUAL_CLOSE и EXPIRED с конечным PnL, нулевой PnL остаётся в знаменателе WR. Миграцию дополнительно прогнать на копии существующей БД, проверив сохранность row count и наличие nullable feature_snapshot. Это описание не обещает counterfactual outcome для кандидатов, не дошедших до анализа.

---


### B-012 — Новые live-правила continuation и lookback отсутствуют в backtest — Severity: CRITICAL

**File:** `scheduler/scanner.py`, `backtest/engine.py`
**Lines:** `scheduler/scanner.py:329-350,492-513; backtest/engine.py:678-697,752-779`

**Current behavior:** Live scanner в9d0da04 сделал sweep необязательным для continuation и увеличил lookback sweep/OB/structure до100/150/100. Backtest сохранил обязательный sweep и прежние50/100/50, а отдельный POI component gate отсутствует. Одинаковые входные свечи проходят разные правила.

**Expected behavior:** В parity-режиме replay использовать те же detector windows и setup-type gates, что текущий live scanner. Sweep остаётся обязательным для reversal; continuation требует BOS; poi_entry требует OB либо FVG. Не вводить более строгие live-правила под видом исправления тестирования.

**Why it matters:** Воспроизведение фактического AST backtest gate: continuation(has_bos=True,has_ob=True,has_sweep=False) всегда rejected, хотя соответствующий live gate PASS. Результаты backtest и расчёты throughput не относятся к текущей live-стратегии. Процент исторически затронутых сигналов не измерен.

**Evidence (verbatim):**
```python
                    elif setup.setup_type == "continuation":
                        if not setup.has_bos:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                        if not setup.has_sweep:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
```

**Fix (exact source replacements):**
```python
from pathlib import Path

ROOT = Path.cwd()
EDITS = [
    ('backtest/engine.py',
'''                        sweeps = detect_sweeps(_df_clean, lookback=50)
                        order_blocks = detect_order_blocks(_df_clean, lookback=100)
''',
'''                        sweeps = detect_sweeps(_df_clean, lookback=100)
                        order_blocks = detect_order_blocks(_df_clean, lookback=150)
'''),
    ('backtest/engine.py',
'''                        structure = analyze_structure(
                            _df_clean, lookback=50,
''',
'''                        structure = analyze_structure(
                            _df_clean, lookback=100,
'''),
    ('backtest/engine.py',
'''                        fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 100))
''',
'''                        fvgs = detect_fvg(_df_clean, lookback=getattr(config, "liquidity_fvg_lookback", 150))
'''),
    ('backtest/engine.py',
'''                    elif setup.setup_type == "continuation":
                        if not setup.has_bos:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                        if not setup.has_sweep:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
''',
'''                    elif setup.setup_type == "continuation":
                        if not setup.has_bos:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
                        # A continuation sweep is optional in the live scanner.
                    elif setup.setup_type == "poi_entry":
                        if not setup.has_ob and not setup.has_fvg:
                            reject_stats.setup_type_gate += 1
                            reject_stats.total_rejected += 1
                            if self.instrument:
                                _funnel_counts["SETUP_TYPE_GATE"] += 1
                            continue
'''),
]

changed = {}
for relative, old, new in EDITS:
    path = ROOT / relative
    source = changed.get(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one exact source block: {relative}")
    changed[path] = source.replace(old, new, 1)
for path, source in changed.items():
    compile(source, str(path), "exec")
for path, source in changed.items():
    path.write_text(source, encoding="utf-8")
```

**Verification:** Семь дополнительных тестов выполняют реальный AST Phase1.4: production rejects BOS+OB without sweep, proposal passes; отсутствующий BOS, reversal без MSS и POI без обеих зон блокируются; корректные reversal/POI проходят. AST-проверка сравнивает четыре lookback выражения live и replay. Все вместе с остальными trade proposals:41 passed.

**Integration / tests / limitations:** Это ограниченное выравнивание изменённых в9d0da04 правил. Оно не утверждает полного совпадения live с backtest: context/MTF, portfolio, funding, pending-fill model остаются отдельными ограничениями. PATTERN_POI_MIN_QUALITY сейчас только сохраняется в init и не читается фильтрацией; это известное ограничение конфигурации, не дополнительное обязательное изменение в этом аудите. Не писать, что POI override затирает успешный continuation: код присваивает reversal=continuation, поэтому такой overwrite не подтверждён.

---


## III. Summary table

| ID | Title | Severity | File | Status |
|---|---|---|---|---|
| B-001 | MAX_SL_ATR обходится широким swing и финальным buffer | MEDIUM | strategy/invalidation.py, strategy/trade_engine.py | Воспроизведён на новой версии; замена предложена |
| B-002 | Направленная геометрия, finite inputs и Kelly/admission budget | CRITICAL | risk/engine.py | Воспроизведён; proposal проверен |
| B-003 | Портфель и outcome сохраняются неатомарно | CRITICAL | storage/database.py, scheduler/scanner.py | Конкурентная проверка proposal пройдена |
| B-004 | FVG подменяет фактическую цену market entry | CRITICAL | strategy/trade_engine.py, scheduler/scanner.py | Воспроизведён; proposal проверен |
| B-005 | FVG «заполняется» свечами до своего появления | CRITICAL | liquidity/fvg.py | Воспроизведён; proposal проверен |
| B-006 | Несовместимые направления sweep/MSS и временная атрибуция | CRITICAL | market_structure/structure.py, strategy/pattern_engine.py | Воспроизведён; proposal проверен |
| B-007 | HTF lookahead и неполные resampled bars | CRITICAL | backtest/engine.py, backtest/resampler.py | Воспроизведён; proposal проверен |
| B-008 | Некорректный порядок и временное окно outcome observations | CRITICAL | scheduler/outcome_tracker.py | Воспроизведён; helper проверен, интеграция впереди |
| B-009 | Истечение срока превращается в нулевой PnL | CRITICAL | scheduler/outcome_tracker.py | Воспроизведён; замена предложена |
| B-010 | WR по статусу и ценовая сумма под видом equity | MEDIUM | storage/database.py, analytics | Подтверждён на SQLite; замены предложены |
| B-011 | Trace теряет gates/features и неправильно агрегирует v2 | MEDIUM | storage/trace.py, storage/database.py | Воспроизведён; proposal проверен |
| B-012 | Live/backtest расходятся по continuation gate и lookbacks | CRITICAL | scheduler/scanner.py, backtest/engine.py | Подтверждён на новой версии; замена предложена |

HIGH по условию prompt требует доказать >5% потери сигналов. Для отдельных code paths нет корректного исторического знаменателя; процент эффекта не выдумывается. CRITICAL здесь — падение или неверные сигналы/labels, не заявление о размере денежного ущерба. Причина исторической пустоты traces **(UNCERTAIN)**: текущий `trace.save` работает на свежей SQLite; отсутствие данных может зависеть от deployed версии, очистки или пути исполнения.

---

## IV. Priority order

Ранжирование по ожидаемому влиянию на корректно обработанные сигналы × уверенности; числовые uplift не идентифицируются. Защита капитала важнее простого увеличения количества сигналов.

1. **B-002, B-003** — запретить неправильные планы и превышение cap; атомарно сохранять сигнал, OPEN outcome и cooldown. В логах подтверждены 3.4/3.8% при cap3%. Найденный на первом commit scanner crash уже исправлен upstream.
2. **B-001** — обеспечить определённый контракт MAX_SL_ATR и явный отказ от плана вне cap; не подрезать структурный SL до выдуманной цены. Проверить объём изменившихся сигналов на replay.
3. **B-004, B-008, B-009** — согласовать цену входа и виртуальные исходы. Пересчёт старых результатов выполнять отдельным read-only replay с provenance, не UPDATE исходной истории.
4. **B-010, B-011** — до новых экспериментов включить проверяемые метрики и полные traces; иначе нельзя оценить эффект последующих изменений.
5. **B-005, B-006** — исправить детекцию/каузальность. Это меняет множество setups: регрессии плюс paper/shadow и сравнение OOS, а не немедленное увеличение риска.
6. **B-007, B-012** — исправить replay и согласовать live/backtest правила перед любым A/B выводом; после этого заново измерить FVG/MSS/HTF эффекты и обе стороны рынка. Старые PF=1.28 и подобные результаты в AGENTS/reports не считать доказанными на новом execution model.

---

## V. Config recommendations

| Param | Current | Recommended | Reason |
|---|---|---|---|
| MAX_PORTFOLIO_RISK_PCT | code 3%; logs cap3% | **3% без повышения** | Сначала корректный prospective/atomic gate; 5/8/10% только независимый shadow replay |
| MAX_ACTIVE_SIGNALS | code3; prompt сообщает server10; .env не предоставлен | Сохранить существующее resolved значение, не менять автоматически | Значение10 — утверждение prompt, не проверенная server настройка; в sample 0 global-count blocks |
| MAX_ACTIVE_SIGNALS_PER_SYMBOL | 1 | 1 | Не обходить общий риск увеличением дублей символа |
| RISK_ENGINE_BASE_RISK_PCT | 1.0% | Не повышать | Heuristic P(TP) не калиброван, confidence производная от него |
| RISK_ENGINE_MAX_RISK_PCT | 2.0% | Не повышать | Base1% не является абсолютным cap после MSS/SL adjustment; итоговый portfolio cap обязателен |
| MIN_RR_THRESHOLD | 1.5 | Сохранить 1.5; вариант2.0 только A/B | Медиана планового RR уже2.49; увеличение TP-дальности не создаёт edge |
| RISK_ENGINE_MIN_RR | 2.0, deprecated/dead | Не использовать как рабочую настройку | Singleton RiskEngine получает trading.min_rr_threshold |
| SIGNAL_COOLDOWN_MINUTES / TF_MULTIPLIER | 45 / 2.0 | Сохранить | 1h=120min, 4h=480min; проверять общий helper и атомарный admission |
| BLOCK_ALL_SELL | false | Не менять глобально без OOS | SELL хуже, но causal mechanism и OOS эффект blanket block не установлены |
| HTF_BIAS_V2 / HTF_HARD_GATE | true / true | Сохранить на этапе code fixes | Сначала убрать leakage в проверке; live gate симметричен |
| HTF_BIAS_CONTINUATION_PENALTY | code0.85, prompt0.7 | Сохранить0.85 до отдельной проверки | Не подменять фактический default описанием из prompt |
| REQUIRE_ENTRY_ZONE | false | Не включать как замену исправлению цены | Active FVG и уже touched FVG имеют разные состояния; gate не симулирует fill limit order |
| OUTCOME_TTL_DAYS | 7 | Сохранить policy, исправить accounting | PnL по текущей цене после проверки касаний, не0 |
| MAX_TRADE_DURATION_BARS | 72, применяется в backtest | Не переносить в live скрыто; отдельный A/B | 72×1h=3д, 72×4h=12д — не тот же срок, что TTL7д |
| PRIMARY_TIMEFRAMES | code1h,4h; sample фактически4h | Сначала проверить resolved server config | Дополнительный TF меняет нагрузку/корреляцию/воронку; не выводить его работу из prompt |
| SL absolute limits | soft checks, structural exceptions | Не затягивать/расширять автоматически | 38 планов SL>5%, max44.66%; нужен replay SL-distance buckets после фикса геометрии |
| MAX_SL_ATR | Новый default3.0, сейчас не гарантирован | Сохранить3.0; обеспечить однозначный контракт B-001 | Swing fallback и дополнительный buffer должны учитываться в финальном SL; не обещать уменьшение historical losses |
| BACKTEST_SOURCE / entry model | local/live и market/fvg_limit | Только исторически причинный источник для A/B | Current HTF на весь прошлый период недопустим; B-007 задаёт точные требования |

Для OOS сравнения сначала фиксировать один baseline с комиссиями/slippage, funding-моделью и одинаковыми fill/exit правилами. Разбить данные по времени, не random split; результаты смотреть по BUY/SELL, setup, TF, regime и символу, с количеством сделок и интервалом неопределённости. Отчётный порог PF>1.2 и WR>50% — критерий приёмки эксперимента, не обещание результата исправлений. Не оптимизировать blacklist на этих же 139 исходах.

---

## VI. Questions for the team

Эти вопросы не блокировали аудит; они нужны для выпуска и воспроизводимого измерения эффекта.

1. Каковы deployed commit, startup resolved config и timezone логов для каждого исторического периода? Почему в сентябрьском sample только4h, хотя prompt описывает1h+4h?
2. Где точный источник фактических fills/quantity/equity/funding? Если бот только публикует сигналы, кто и по какой цене их исполняет, и как отличить виртуальный outcome от реального?
3. Почему historical decision_traces/candidates/context_snapshots пусты при рабочем на fresh DB trace.save? Был ли другой код, отключение записи или очистка?
4. Восстановимы ли девять wrong-side TP и девять EXPIRED по исходным market/execution данным? Без этого достоверный historical WR/PF недоступен.
5. Перерывы логов 10.5 и80.5 часа — downtime или неполный архив? Это нужно для weekly throughput.
6. Какова согласованная цель частоты: prompt одновременно говорит «8 в неделю слишком мало» и «нужно2–5 качественных в неделю»? До уточнения не оптимизировать volume вместо качества.
7. Включён ли webhook, несколько процессов/инстансов и ручная публикация? Все пути создания реального active signal должны использовать один admission; нужна ли outbox/retry семантика Telegram? Неполученная пользователем публикация сейчас не означает автоматической отмены виртуальной позиции.

---

## VII. Implementation notes for mimo

**Safe to apply immediately после regression checks:** B-002/B-003; finite/geometry validation; корректные названия/знаменатели метрик; lossless trace serialization. «Immediately» не означает прямой запуск на сервере без проверки совместимости и snapshot. В этой работе подготовлен отчёт; production fixes не применены и бот не запускался в live режиме.

**Need A/B testing:** детекция FVG/MSS после bugfix, требование sweep для continuation, BUY/SELL restrictions, RR2+, TP selection, SL caps, 72-bar exits, увеличение budget или добавление1h. B-007 обязателен до получения валидных результатов A/B. Для market-entry нельзя переиспользовать FVG median как fill; limit model требует отдельного pending order и последующего касания.

**Require server config changes:** обязательного повышения risk settings нет. Снять и сохранить resolved config и commit hash; убедиться в работающем tracker/trace, планировщике и single-writer/transaction discipline. Реконцилировать неизвестный риск OPEN до включения fail-closed admission. Исторический `checked_at` старого tracker не доказывает, что интервал реально проверен: существующие OPEN сначала read-only replay от created_at; обычную работу нового cursor начинать с подтверждённой границы. Не закрывать непроверенные позиции только ради освобождения слота.

**Dependencies between fixes:** B-002 требует явное направление во всех живых callers; B-003 заменяет пару save_signal/create_outcome на общую транзакцию и не должен оставлять второй create_outcome; B-004 согласует market/limit execution; B-005/B-006 должны менять и соответствующие возрастные/causality tests; B-008 и B-009 применять совместно; B-010 определяет PnL-based WR, и telemetry B-011 должен использовать ту же семантику. До release обновить `plan/01-architecture.md`, `plan/07-scheduler.md`, `plan/11-pipeline.md`, `plan/14-env-config.md`, удалить из них описания отсутствующих FeatureBuilder/ProbabilityEngine/POI путей или явно обозначить историю. Сам AGENTS не переписывать вместо plan.

**Test files to update:** `tests/test_scanner.py`, `tests/test_new_pipeline.py`, `tests/test_database.py`, `tests/test_outcome_tracker.py`, `tests/test_liquidity.py`, `tests/test_market_structure.py`, `tests/test_mss_diagnostic.py`, `tests/test_backtest_parity.py`, `tests/test_resampler.py`, `tests/test_decision_trace.py`, `tests/test_analytics.py`, `tests/test_webhook.py`. Добавить тесты не только pass-path, но simultaneous admissions, rollback после неудачного outcome insert, BUY/SELL mirrored geometry, non-finite values, missing data, TTL с ненулевой ценой, ambiguous same-minute hit, timestamp causality и отсутствие future leakage. Все network/Telegram APIs в regression тестах должны быть mocked.

### Что проверено в ходе аудита

Полный исходный suite на актуальном `9d0da04`: **116 failed, 939 passed, 1 skipped, 2 warnings** (1056 tests). На первоначальном `311c98e` было119 failed/936 passed; новый upstream исправил NameError и три связанные проверки. Остались tests, ссылающиеся на удалённые FeatureBuilder/ProbabilityEngine; устаревшие поля BacktestTrade и scanner mocks; отсутствует optional parquet engine `pyarrow`; часть ожиданий config/UI не соответствует текущей ветке. Поэтому baseline failures нельзя объявлять116 production дефектами. Полный short traceback актуального запуска сохранён в `fix/audit_v2/baseline-pytest.txt` (локальные пути заменены относительными).

Python 3.12.14; установленные версии: pytest=8.4.2, pytest-asyncio=0.26.0, numpy=2.2.6, pandas=3.0.6, pandas-ta=0.4.71b0, ccxt=4.2.15, SQLAlchemy=2.0.23. .env/server secrets не использовались; актуальная server конфигурация не подтверждена из файла. Диапазоны requirements позволяют отличия окружений.

Изолированные reproductions на актуальной версии подтвердили ошибки risk, FVG/live entry/MSS/HTF, tracker, SL cap/live-backtest parity и агрегаты DB. Предложенные risk/atomic/telemetry методы исполнялись с временной SQLite и mocked I/O: geometry/NaN/Kelly/budget, concurrent admission, exact cap, rollback, trace fields/WR/PF — PASS. В trade proposal **41 tests passed**; семь exact-replacement scripts последовательно применены на актуальной контрольной копии и дали девять протестированных изменённых модулей. Outcome proposal **16 tests passed**: 7 helper + 9 integration/watermark, включая expiry BUY/SELL, недоступную историю, timestamp cutoff и сохранённую цену уведомления. Совместное применение risk+всех семи trade scripts в обоих порядках дало идентичные файлы; **258 Python-файлов compiled**, включая audit scripts. Миграция feature_snapshot проверена на legacy SQLite: sentinel сохранён, повторный init безопасен; signed-PnL trace metrics подтверждены. Отдельно проверены сериализация20 дополнительных config keys и сохранение setup_type/git_sha при merge snapshots. Metrics draft:14 Python-блоков compile, edge/render tests на NaN/Inf/no-loss прошли. Это не end-to-end validation всего будущего исправленного приложения. Финальная компиляция Python Fix-блоков отчёта выполняется с учётом контекста вставки; результаты — `fix/audit_v2/report-validation.json`. Полный suite после будущего применения всех fixes ещё не запускался, и live/OOS backtest не выполнялся.


### Порядок приёмки реализации

1. Применить связанные safety fixes в отдельной ветке от того же commit; перенести приведённые reproductions в regression tests с assertions на исправленное поведение. Проверить, что старые пути webhook/manual не обходят admission.
2. Добиться прохождения нового regression набора и необходимых existing tests; baseline failures классифицировать и исправлять по сути, не менять ожидаемые значения под результат без контракта. Не восстанавливать удалённые модули-заглушки только ради green suite.
3. На копии БД проверить миграции, snapshots, complete traces и арифметику portfolio risk. Проверить две одновременно проходящие заявки на последнем доступном risk budget. На исходной БД отчётный скрипт остаётся read-only.
4. Выполнить causal replay с historical HTF, cost model, chronological portfolio state; отдельно показать invalid/unknown outcomes, а не исключить их молча. Сравнить baseline и варианты на неизменном OOS периоде, со списком trade IDs и воспроизводимой конфигурацией.
5. Только после измеренного результата решать выпуск стратегии и risk/config изменения. Ожидаемый рост количества/WR/PF до replay остаётся **(UNCERTAIN)**.

---

## Appendix A. Проверенные данные

### Подтвержденные цифры и исправления предпосылок

| Метрика | Проверено |
|---|---:|
| Сигналов / закрыто / OPEN | 143 / 139 / 4 |
| Положительный / отрицательный / нулевой сохраненный PnL | 36 / 91 / 12 |
| WR, все закрытые с числовым PnL | 36/139 = 25.8993% |
| WR, только ненулевой PnL | 36/127 = 28.3465% |
| TP-event rate (это отдельная метрика) | 37/139 = 26.6187% |
| Сумма ценовых доходностей | -126.1245385 **процентных пункта**, не доходность капитала |
| PF по знаку ценового PnL | 177.4628764 / 303.5874149 = **0.584553** |
| Risk-weighted proxy sum | -37.574365 п.п. |
| Risk-weighted proxy PF | 0.503072 |
| Средняя / медианная длительность | 41.6763 / 17.9417 ч |
| Минимальный / максимальный PnL сделки | -12.2882% / +24.1674% |

Число «49 wins» можно арифметически получить как 37 HIT_TP + 12 MANUAL_CLOSE, но это не 49 прибыльных сделок. Распределение сохраненных исходов:

| Статус | Всего | PnL>0 | PnL<0 | PnL=0 | Сумма ценового PnL, п.п. |
|---|---:|---:|---:|---:|---:|
| HIT_SL | 81 | 0 | 81 | 0 | -258.526173 |
| HIT_TP | 37 | 28 | 9 | 0 | +118.902066 |
| MANUAL_CLOSE | 12 | 8 | 1 | 3 | +13.499569 |
| EXPIRED | 9 | 0 | 0 | 9 | 0 |

Девять EXPIRED записаны с нулевой доходностью по правилам tracker, а не на основании фактического закрытия; это ограничивает экономический смысл даже исправленных метрик. Нулевые MANUAL_CLOSE тоже требуют проверки происхождения. Нужно показывать статус, знак PnL и неизвестные исходы отдельно.

Risk-weighted proxy рассчитан отдельно для каждой записи:

```python
sl_distance_pct = abs(entry - sl) / entry * 100
net_r = pnl_pct / sl_distance_pct
capital_pnl_proxy_pp = risk_pct * net_r
```

Это арифметическая оценка при трактовке `risk_pct` как запланированного убытка капитала на SL. Она **не является восстановленной equity**: нет фактических количеств, остатков equity, broker fills, истинных комиссий/funding и логики реинвестирования. Складывать ценовые проценты разных позиций и называть это потерей 126% депозита нельзя. Нельзя также заменить одно такое заявление на «потеря 37.57% депозита».

### Направление, TF и плановый R:R

| Сегмент | Все / закрытые | Средний ценовой PnL | Ценовой PF | Proxy capital sum, п.п. | Proxy PF |
|---|---:|---:|---:|---:|---:|
| BUY | 78 / 76 | +0.114901% | 1.061308 | -7.572985 | 0.795480 |
| SELL | 65 / 63 | -2.140587% | 0.163162 | -30.001380 | 0.222464 |
| 1h | 59 / 59 | -0.657891% | 0.523328 | -11.978161 | 0.643595 |
| 4h | 84 / 80 | -1.091362% | 0.606994 | -25.596204 | 0.390639 |

«BUY прибыльны» справедливо только для невзвешенного ценового агрегата; sizing-proxy отрицателен и для BUY. SELL существенно хуже в обоих расчетах, но причинную роль HTF/паттернов доказать этими данными невозможно. Все 1h сигналы заканчиваются 14 августа; сравнение TF смешано со временем/изменениями реализации. Данные по каждому из 32 торгованных символов есть в `fix/audit_v2/aggregate.json/by_symbol`; не строить blacklist на микровыборках по 1–11 исходов.

| Плановая метрика по 143 сигналам | Все | BUY | SELL |
|---|---:|---:|---:|
| Медиана абсолютного RR | 2.493699 | 2.118411 | 2.899592 |
| Среднее абсолютного RR | 3.208448 | 2.413308 | 4.162616 |
| Медиана SL-distance | 2.891225% | 3.532528% | 2.819883% |
| Среднее SL-distance | 5.429697% | 7.231868% | 3.267092% |
| Планов RR<1.5 | 34 | 30 | 4 |
| Планов SL-distance>5% | 38 | 28 | 10 |
| Неверная направленная геометрия TP | 9 | 8 | 1 |

Максимальный SL-distance **44.664105%**. Деление условного среднего результата HIT_TP на средний HIT_SL не измеряет RR торгового плана; группы состоят из разных сделок, а 9 TP вообще стоят с убыточной стороны. Данные не поддерживают вывод, что SELL хуже из-за меньшего планового RR или что общая главная проблема — слишком тесный SL. Слишком далекие TP и SL остаются гипотезами до временно честного replay.

Ошибочная геометрия зафиксирована у ID **71, 100, 101, 102, 109, 110, 111, 113, 114**. Все получили `HIT_TP` с отрицательным PnL. Проверяемые примеры:

```python
# ID 71, BCH SELL: TP находится ВЫШЕ entry
entry = 202.24
sl = 216.07835778
tp = 205.16212577
stored_pnl_pct = -1.6454499030917222

# ID 113, ZRO BUY: TP находится НИЖЕ entry
entry = 1.216
sl = 0.67288448
tp = 1.0690499999999998
stored_pnl_pct = -12.288194063278155
```

Это доказательство исторически неверных планов. Каким commit/config они выпущены, не сохранено; нельзя автоматически приписывать все 34 RR-нарушения текущей реализации.

### Воронка: точный знаменатель и подпричины

Единый базовый файл: `logs/bot.log` из Git, 2026-09-08 13:45:35 — 2026-09-14 22:55:53 local. Время лога выглядит UTC+3 при сопоставлении notifier и created_at БД; timezone явно не сохранен. Rotated файлы перекрываются с bot.log; суммировать их записи нельзя.

| Шаг / исход | Число |
|---|---:|
| Завершенные циклы | 250 |
| Scan entries (symbol × TF attempts; еще не найденные setups) | 8750 |
| Cooldown BLOCK | 128 |
| Initial portfolio gate BLOCK | 8526 |
| Дошли после initial portfolio gate | 96 |
| Pattern engine BLOCK | 34 |
| Portfolio recheck BLOCK | 34 |
| Risk engine BLOCK | 5 |
| HTF bias BLOCK | 14 |
| Symbol filter BLOCK | 1 |
| Отправлено | 8 |
| Циклы без сигналов | 245/250 = 98% |

Initial `portfolio_risk` = **8526/8750 = 97.44% всех scan entries**, либо 8526/(8750−128) = 98.8866% дошедших до gate. Это не доля валидных паттернов: gate стоит до их анализа. Подпричины:

| Подпричина | Число | % всех 8750 entries |
|---|---:|---:|
| Risk budget | 7591 | 86.7543% |
| Per-symbol count limit | 935 | 10.6857% |
| Global count limit | 0 | 0% |

Risk-budget блокировки: `3.0>=3.0` 4714 раз, `3.4>=3.0` 2654 раз, `3.8>=3.0` 223 раза. Примеры с точными номерами строк лежат в `logs[0].portfolio_subreason_examples`. Состояния 3.4/3.8% показывают, что установленный cap уже превышался; это не повод дополнительно расширять его.

Все 8742 строки BLOCK в этой выборке имеют **4h**, все 8 emitted тоже 4h; 250×35 entries согласуются с одним TF на цикл. В prompt «1h/4h одновременно» не является наблюдаемым фактом этой выборки. Есть провалы между summary 9 сентября 04:17 → 14:48:30 (**10.525 ч**) и 10 сентября 14:47:01 → 13 сентября 23:18:15 (**80.5206 ч**). Это не непрерывная неделя; пересчет `8 / week` некорректен без учета uptime/пропуска логов.

При механической замене порога на 5 или 8% и **замороженных** исторических состояниях все 7591 budget-only строки перестали бы блокироваться. При этом 935 per-symbol отказов остаются. Это лишь статическое число повторных scan attempts, не новых уникальных setups и не сигналов. Уже первый дополнительный сигнал меняет следующий portfolio state, и ранжирование/последовательность тоже меняются. Нельзя честно получить число новых weekly signals ни для 3→5, ни для 3→8 без независимого shadow candidate stream и последовательного replay портфеля.

### Открытые сделки и stale

На snapshot OPEN risk = **0.6 + 1 + 1 + 0.8 = 3.4%**, count=4. `get_portfolio_risk_sum` складывает только OPEN, без дублей и null-risk в этом снимке; арифметическая сумма верна.

| ID / символ | Возраст на snapshot | 4h бары | risk |
|---|---:|---:|---:|
| 136 ADA | 128.1321 ч | 32.0330 | 0.6% |
| 139 BTC | 23.6391 ч | 5.9098 | 1.0% |
| 142 BNB | 21.6352 ч | 5.4088 | 1.0% |
| 143 LTC | 9.8856 ч | 2.4714 | 0.8% |

Все checked_at обновлены в пределах последних 3 секунд snapshot. **Ни одна не старше 7 дней или 72 баров на дату снимка.** Нельзя вычислять stale на 21 сентября и выдавать замороженные записи за живое состояние. Но текущий tracker действительно имеет 7-day expiry с PnL=0; долгие позы занимают риск, и отсутствие единых live/backtest exit semantics требует отдельной правки.

### Невосстановимые данные

`decision_traces=0`, `signal_candidates=0`, `context_snapshots=0`; у всех 143 `execution_snapshot=NULL`, `confidence_v2_factors=NULL`, `telegram_sent_at=NULL`. `entry_candle_open` есть у 25, MFE/MAE у 118. На `signals` нет сохраненных `setup_type` и точного `strategy_version`. Fingerprint у ранних 62 строк содержит **имена** фич, не значения; у 81 поздней строки — components/regime. `fix/audit_v2/aggregate.json` показывает эти logging cohorts отдельно, но не выдает их за релизы стратегии.

Нельзя посчитать надежный setup×direction×version PF, доказать, что HTF отсекал хорошие SELL, восстановить отвергнутые setups, или вывести throughput по лимитам. Рекомендация: зафиксировать current commit + resolved config hash + setup_type + candidate decision chain + complete execution snapshot, затем проверять OOS по времени и без утечек; не обещать PF>1.2/WR>50% до проверки.


Дополнительная сверка новых daily reports: PF0.61 воспроизводится как0.607484 для более раннего среза136 закрытых (до2026-09-13 22:00UTC). Для полного снимка139 закрытых PF=0.584553. Поэтому0.61 может относиться к иному моменту; нельзя смешивать его с поздними количеством/WR.


## Appendix B. Разбивка по символам

| Symbol | Signals | Closed / OPEN | + / − / 0 PnL | Mean price PnL | Price PF |
|---|---:|---:|---:|---:|---:|
| 1000PEPE/USDT | 1 | 1 / 0 | 1 / 0 / 0 | +1.798% | н/д |
| AAVE/USDT | 1 | 1 / 0 | 0 / 1 / 0 | -2.119% | 0.000 |
| ADA/USDT | 4 | 3 / 1 | 0 / 2 / 1 | -1.541% | 0.000 |
| APE/USDT | 3 | 3 / 0 | 1 / 2 / 0 | -0.637% | 0.751 |
| APT/USDT | 3 | 3 / 0 | 2 / 1 / 0 | +1.631% | 6.279 |
| ARB/USDT | 2 | 2 / 0 | 1 / 1 / 0 | -1.476% | 0.231 |
| ATOM/USDT | 11 | 11 / 0 | 2 / 7 / 2 | -1.285% | 0.114 |
| AVAX/USDT | 3 | 3 / 0 | 1 / 2 / 0 | -2.369% | 0.263 |
| BCH/USDT | 3 | 3 / 0 | 1 / 1 / 1 | +0.249% | 1.453 |
| BNB/USDT | 9 | 8 / 1 | 6 / 1 / 1 | +0.889% | 8.339 |
| BTC/USDT | 9 | 8 / 1 | 1 / 6 / 1 | -0.709% | 0.221 |
| CRV/USDT | 2 | 2 / 0 | 0 / 2 / 0 | -2.880% | 0.000 |
| DOT/USDT | 3 | 3 / 0 | 0 / 3 / 0 | -3.371% | 0.000 |
| ETH/USDT | 2 | 2 / 0 | 0 / 2 / 0 | -2.929% | 0.000 |
| FET/USDT | 1 | 1 / 0 | 1 / 0 / 0 | +5.291% | н/д |
| INJ/USDT | 3 | 3 / 0 | 0 / 3 / 0 | -4.951% | 0.000 |
| LDO/USDT | 7 | 7 / 0 | 4 / 3 / 0 | +6.654% | 5.177 |
| LINK/USDT | 7 | 7 / 0 | 5 / 2 / 0 | +1.038% | 2.249 |
| LTC/USDT | 2 | 1 / 1 | 0 / 1 / 0 | -1.081% | 0.000 |
| NEAR/USDT | 5 | 5 / 0 | 3 / 2 / 0 | +6.722% | 4.926 |
| OP/USDT | 7 | 7 / 0 | 0 / 7 / 0 | -3.949% | 0.000 |
| PENDLE/USDT | 7 | 7 / 0 | 2 / 5 / 0 | -1.208% | 0.297 |
| POL/USDT | 3 | 3 / 0 | 0 / 3 / 0 | -1.928% | 0.000 |
| SNX/USDT | 4 | 4 / 0 | 1 / 3 / 0 | -1.643% | 0.239 |
| SOL/USDT | 6 | 6 / 0 | 1 / 4 / 1 | -0.593% | 0.463 |
| STG/USDT | 7 | 7 / 0 | 0 / 4 / 3 | -4.096% | 0.000 |
| SUI/USDT | 3 | 3 / 0 | 1 / 2 / 0 | -2.002% | 0.229 |
| TIA/USDT | 2 | 2 / 0 | 0 / 2 / 0 | -2.866% | 0.000 |
| UNI/USDT | 1 | 1 / 0 | 1 / 0 / 0 | +17.227% | н/д |
| XRP/USDT | 5 | 5 / 0 | 1 / 3 / 1 | -1.371% | 0.103 |
| ZEC/USDT | 6 | 6 / 0 | 0 / 5 / 1 | -3.014% | 0.000 |
| ZRO/USDT | 11 | 11 / 0 | 0 / 11 / 0 | -5.191% | 0.000 |

Небольшие выборки по символу не обосновывают blacklist. Строки с OPEN показывают их отдельно; WR рассчитывается по сохранённому PnL, не по названию события.
