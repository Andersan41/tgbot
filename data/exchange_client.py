"""
data/exchange_client.py — Получение OHLCV данных через ccxt

Workaround для Windows: aiohappyeyeballs (aiohttp 3.10+) ломает DNS resolution.
Все сетевые запросы идут через sync ccxt в run_in_executor.
"""
import asyncio
import sys
from typing import Optional
import ccxt as ccxt_sync
import pandas as pd
from loguru import logger
from config.settings import config, get_active_symbols


class ExchangeClient:
    def __init__(self):
        self._exchange: Optional[ccxt_sync.Exchange] = None
        self._markets_loaded = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._available_symbols: set[str] = set()
        # Маппинг: bot symbol (BTC/USDT) -> ccxt unified symbol (BTC/USDT:USDT)
        self._symbol_map: dict[str, str] = {}

    def _build_symbol_map(self):
        """Строим маппинг bot symbol -> ccxt symbol для деривативных рынков.

        Swap (perpetual) использует формат BTC/USDT:USDT, expiring futures —
        BTC/USDT:USDT-260926 (с датой истечения). Для spot маппинг не нужен:
        символы совпадают (BTC/USDT).
        """
        self._symbol_map = {}
        if config.exchange.market_type == "spot":
            return
        market_key = "swap" if config.exchange.market_type == "swap" else "future"
        for sym, m in self._exchange.markets.items():
            if m.get(market_key) and m.get("active", True):
                # BTC/USDT:USDT[-260926] -> ищем matching bot symbol "BTC/USDT"
                bot_sym = sym.split(":")[0]
                if bot_sym not in self._symbol_map:
                    self._symbol_map[bot_sym] = sym
        if self._symbol_map:
            logger.info(
                f"Symbol map: {len(self._symbol_map)} "
                f"{config.exchange.market_type} pairs mapped"
            )

    def _resolve_symbol(self, symbol: str) -> str:
        """Конвертируем bot symbol в ccxt symbol (если есть маппинг)."""
        return self._symbol_map.get(symbol, symbol)

    async def connect(self):
        """Создаём подключение к бирже (sync exchange для Windows compatibility)"""
        exchange_class = getattr(ccxt_sync, config.exchange.name)
        self._exchange = exchange_class({
            "apiKey": config.exchange.api_key,
            "secret": config.exchange.api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": config.exchange.market_type,
            },
        })
        self._exchange.has["fetchCurrencies"] = False

        max_retries = 5
        retry_delay = 5
        for attempt in range(1, max_retries + 1):
            try:
                markets = await asyncio.get_event_loop().run_in_executor(
                    None, self._exchange.load_markets
                )
                logger.info(f"Markets loaded: {len(markets)} symbols")
                self._markets_loaded = True
                active = set()
                for sym, m in self._exchange.markets.items():
                    if m.get("active", True) and m.get(config.exchange.market_type, False):
                        active.add(sym)
                self._available_symbols = active
                self._build_symbol_map()
                break
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        f"Failed to load markets (attempt {attempt}/{max_retries}), "
                        f"retrying in {retry_delay}s: {e}"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error(f"Failed to load markets after {max_retries} attempts: {e}")
                    raise

        logger.info(f"Exchange client created: {config.exchange.name}")

        # Семафор для сериализации запросов — ccxt rate limiter не thread-safe
        self._semaphore = asyncio.Semaphore(1)

    async def close(self):
        if self._exchange:
            # Sync ccxt exchange — просто обнуляем ссылку, requests session закроется сама
            self._exchange = None
            logger.info("Exchange connection closed")

    async def _ensure_markets_loaded(self):
        if not self._markets_loaded and self._exchange:
            logger.warning("Markets not loaded, attempting to reload...")
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._exchange.load_markets
                )
                self._markets_loaded = True
                active = set()
                for sym, m in self._exchange.markets.items():
                    if m.get("active", True) and m.get(config.exchange.market_type, False):
                        active.add(sym)
                self._available_symbols = active
                self._build_symbol_map()
                logger.info(
                    f"Markets reloaded: {len(self._exchange.markets)} loaded, "
                    f"{len(active)} active {config.exchange.market_type} symbols"
                )
            except Exception as e:
                logger.error(f"Failed to reload markets: {e}")
                raise

    def _fetch_ohlcv_raw(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        max_retries: int = 3,
        since: int | None = None,
        drop_last: bool = True,
        end_time: int | None = None,
    ) -> Optional[list]:
        """Sync fetch_ohlcv — вызывается через run_in_executor

        Встроенные повторные попытки при сетевых ошибках.
        RateLimitExceeded/DDoSProtection получают больше попыток.
        BadSymbol/BadRequest не ретраются.

        ``since`` — UNIX timestamp в миллисекундах; биржа вернёт свечи,
        начиная с этой даты (передаётся как kwarg ccxt → ``startTime``).
        ``end_time`` — UNIX timestamp в миллисекундах; биржа вернёт свечи,
        заканчивающиеся не позже этой даты (``endTime`` param). Используется
        загрузчиком истории: BingX не поддерживает ``since``-пагинацию
        для глубокой истории, но корректно работает с ``endTime``.
        ``drop_last=True`` отбрасывает последнюю (самую старую в ответе ccxt)
        свечу — нужно для живых запросов, где последняя свеча может быть
        незавершённой. При загрузке истории установите ``drop_last=False``.
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        if ccxt_symbol not in self._available_symbols:
            logger.warning(
                f"Symbol {symbol} (ccxt: {ccxt_symbol}) not available on "
                f"{config.exchange.market_type}, skipping"
            )
            return None

        params: dict = {}
        if end_time is not None:
            params["endTime"] = end_time

        last_error = None
        for attempt in range(max_retries):
            try:
                raw = self._exchange.fetch_ohlcv(
                    ccxt_symbol, timeframe, since=since, limit=limit,
                    params=params,
                )
                if raw and drop_last:
                    raw = raw[:-1]
                return raw
            except (ccxt_sync.BadSymbol, ccxt_sync.BadRequest) as e:
                logger.warning(
                    f"Invalid symbol/request for {symbol} {timeframe}: {e}"
                )
                return None
            except (ccxt_sync.RateLimitExceeded, ccxt_sync.DDoSProtection) as e:
                last_error = e
                if attempt < max_retries - 1:
                    import time
                    delay = 5 * (2 ** attempt)
                    logger.warning(
                        f"Rate limited fetching {symbol} {timeframe} "
                        f"(attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {delay}s: {e}"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Rate limited fetching {symbol} {timeframe} "
                        f"after {max_retries} attempts: {e}"
                    )
            except ccxt_sync.NetworkError as e:
                last_error = e
                if attempt < max_retries - 1:
                    import time
                    delay = 2 ** attempt
                    logger.warning(
                        f"Network error fetching {symbol} {timeframe} "
                        f"(attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {delay}s: {e}"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Network error fetching {symbol} {timeframe} "
                        f"after {max_retries} attempts: {e}"
                    )
            except ccxt_sync.ExchangeError as e:
                logger.error(f"Exchange error fetching {symbol} {timeframe}: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error fetching {symbol} {timeframe}: {e}")
                return None
        return None

    async def _fetch_taker_buy_volumes(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> Optional[list]:
        """Fetch taker buy base asset volume.

        Binance-specific: uses fapiPublicGetKlines index 9.
        BingX и другие биржи не поддерживают этот эндпоинт — возвращаем None.
        """
        if config.exchange.name != "binance":
            return None
        if config.exchange.market_type not in ("future", "swap"):
            return None

        try:
            symbol_for_api = symbol.replace("/", "")
            if hasattr(self._exchange, "fapiPublicGetKlines"):
                params = {"symbol": symbol_for_api, "interval": timeframe, "limit": limit}
                klines = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._exchange.fapiPublicGetKlines(params)
                )
                return [float(k[9]) for k in klines]
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch taker buy volumes for {symbol}: {e}")
            return None

    async def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        """Получаем текущую цену через ticker (real-time bid/last)."""
        await self._ensure_markets_loaded()
        ccxt_symbol = self._resolve_symbol(symbol)
        if ccxt_symbol not in self._available_symbols:
            logger.warning(f"Symbol {symbol} not available for ticker fetch")
            return None
        try:
            async with self._semaphore:
                ticker = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_ticker(ccxt_symbol)
                )
                return ticker.get("last")
        except Exception as e:
            logger.warning(f"Failed to fetch ticker for {symbol}: {e}")
            return None

    async def fetch_ticker_full(self, symbol: str) -> Optional[dict]:
        """Получаем полный ticker с bid/ask/last."""
        await self._ensure_markets_loaded()
        ccxt_symbol = self._resolve_symbol(symbol)
        if ccxt_symbol not in self._available_symbols:
            return None
        try:
            async with self._semaphore:
                ticker = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._exchange.fetch_ticker(ccxt_symbol)
                )
                return {
                    "last": ticker.get("last"),
                    "bid": ticker.get("bid"),
                    "ask": ticker.get("ask"),
                    "timestamp": ticker.get("timestamp"),
                }
        except Exception as e:
            logger.warning(f"Failed to fetch full ticker for {symbol}: {e}")
            return None

    def get_tick_size(self, symbol: str) -> Optional[float]:
        """Получаем минимальный шаг цены (tick size) из market info."""
        ccxt_symbol = self._resolve_symbol(symbol)
        market = self._exchange.markets.get(ccxt_symbol)
        if market is None:
            return None
        # precision.price — количество десятичных знаков
        # tick_size = 10^(-precision.price)
        precision = market.get("precision", {}).get("price")
        if precision is None:
            return None
        return 10 ** (-precision)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        since: int | None = None,
        drop_last: bool = True,
        end_time: int | None = None,
    ) -> Optional[pd.DataFrame]:
        """
        Получаем OHLCV свечи и возвращаем как DataFrame.
        Колонки: timestamp, open, high, low, close, volume
        Для futures: также добавляем taker_buy_volume (index 9 из Binance API).

        ``since`` — UNIX timestamp в миллисекундах; биржа вернёт свечи, начиная
        с этой даты (ccxt kwarg → ``startTime``).
        ``end_time`` — UNIX timestamp в миллисекундах; биржа вернёт свечи, не
        позже этой даты (``endTime`` param). Используется загрузчиком истории,
        т.к. BingX не отдаёт глубокую историю через ``since``.
        ``drop_last=True`` (по умолчанию) отбрасывает последнюю свечу — она может
        быть незавершённой. Для середины истории передайте ``drop_last=False``,
        чтобы не терять валидные свечи.
        """
        await self._ensure_markets_loaded()

        async with self._semaphore:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._fetch_ohlcv_raw(
                    symbol, timeframe, limit, since=since, drop_last=drop_last,
                    end_time=end_time,
                )
            )
            if raw is None:
                return None
            if not raw:
                logger.warning(f"No data for {symbol} {timeframe}")
                return None

            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp")
            df = df.astype(float)
            df = df.dropna()

            taker_buy_volumes = await self._fetch_taker_buy_volumes(symbol, timeframe, limit)
            if taker_buy_volumes and len(taker_buy_volumes) == len(raw):
                df["taker_buy_volume"] = taker_buy_volumes[:len(raw)]

            # drop_last is already handled by _fetch_ohlcv_raw — no second drop needed.

        logger.debug(f"Fetched {len(df)} candles: {symbol} {timeframe}")
        return df

    async def fetch_ohlcv_paginated(
        self,
        symbol: str,
        timeframe: str,
        total_limit: int = 4000,
        page_size: int = 998,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV with pagination to overcome exchange per-request limits.

        Uses BingX endTime parameter to page backwards in time.
        Returns up to total_limit candles (minus 1 dropped for open candle).
        """
        await self._ensure_markets_loaded()

        ccxt_symbol = self._resolve_symbol(symbol)
        if ccxt_symbol not in self._available_symbols:
            logger.warning(f"Symbol {symbol} not available for paginated fetch")
            return None

        all_dfs: list[pd.DataFrame] = []
        end_time_ms: Optional[int] = None
        remaining = total_limit

        while remaining > 0:
            batch_limit = min(remaining, page_size)
            params = {}
            if end_time_ms is not None:
                params["endTime"] = end_time_ms

            async with self._semaphore:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda bl=batch_limit, p=params, tf=timeframe: self._exchange.fetch_ohlcv(
                        ccxt_symbol, tf, limit=bl, params=p,
                    ),
                )

            if not raw:
                break

            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp")
            df = df.astype(float).dropna()

            if df.empty:
                break

            all_dfs.append(df)
            remaining -= len(df)

            # Set end_time to just before the oldest candle in this batch
            end_time_ms = int(df.index[0].timestamp() * 1000) - 1

            logger.debug(
                f"Paginated fetch {symbol}: got {len(df)} candles, "
                f"oldest={df.index[0]}, remaining={remaining}"
            )

            # Safety: stop if we got significantly less than requested
            if len(raw) < batch_limit - 3:
                break

            # Small delay between pages
            await asyncio.sleep(0.3)

        if not all_dfs:
            return None

        # Concatenate and sort (newest first from each batch, need to reverse)
        result = pd.concat(all_dfs).sort_index()
        result = result[~result.index.duplicated(keep="first")]

        # Take the most recent total_limit candles
        if len(result) > total_limit:
            result = result.iloc[-total_limit:]

        # Drop last candle (currently forming)
        result = result.iloc[:-1]

        logger.debug(f"Paginated fetch {symbol}: total {len(result)} candles, "
                      f"{result.index[0]} to {result.index[-1]}")
        return result

    async def fetch_ohlcv_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int = 998,
        max_batches: int = 1000,
    ) -> list[dict]:
        """Forward pagination starting from ``since_ms`` until exchange runs out.

        Используется загрузчиком истории (backtest/cache_ohlcv.py) для
        построения полного 3-летнего ряда свечей. Каждый батч запрашивается
        с ``since = last_ts_ms + 1``; цикл останавливается, когда биржа
        возвращает пустой или дублирующийся батч.

        Returns:
            Список свечей [ts_ms, o, h, l, c, v] (без последней незавершённой).
        """
        await self._ensure_markets_loaded()
        ccxt_symbol = self._resolve_symbol(symbol)
        if ccxt_symbol not in self._available_symbols:
            logger.warning(
                f"Symbol {symbol} (ccxt: {ccxt_symbol}) not available for since-fetch"
            )
            return []

        all_candles: list[list] = []
        since = since_ms
        batch_num = 0
        last_ts: int | None = None

        while batch_num < max_batches:
            batch_num += 1
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda s=since, l=limit: self._fetch_ohlcv_raw(
                    symbol, timeframe, limit=l, since=s, drop_last=False,
                ),
            )
            if not raw:
                logger.debug(
                    f"since-fetch {symbol} {timeframe}: empty batch at since={since}, "
                    f"{len(all_candles)} candles so far"
                )
                break

            first_ts = int(raw[0][0])
            new_last_ts = int(raw[-1][0])

            # защита от зацикливания: если батч дублирует предыдущий — стоп
            if last_ts is not None and new_last_ts <= last_ts:
                logger.warning(
                    f"since-fetch {symbol} {timeframe}: duplicate batch "
                    f"(last_ts={last_ts} == new_last={new_last_ts}), stopping"
                )
                break

            all_candles.extend(raw)
            last_ts = new_last_ts

            # следующий батч — после последней полученной свечи
            since = new_last_ts + 1

            if len(raw) < limit:
                # биржа выдала меньше лимита — история закончилась
                break

            # rate limit: не чаще 1 запроса в 50 мс (enableRateLimit на exchange
            # уже включён, но делаем явную паузу для safety)
            await asyncio.sleep(0.05)

        logger.debug(
            f"since-fetch {symbol} {timeframe}: {len(all_candles)} candles "
            f"in {batch_num} batches"
        )
        return all_candles

    async def fetch_all_symbols(
        self,
        timeframe: str,
        limit: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """Загружаем данные по всем символам параллельно"""
        tasks = {
            symbol: self.fetch_ohlcv(symbol, timeframe, limit)
            for symbol in get_active_symbols()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        data = {}
        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Error fetching {symbol}: {result}")
            elif result is not None:
                data[symbol] = result
        return data


# Singleton
exchange_client = ExchangeClient()
