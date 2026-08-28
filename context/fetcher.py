"""
context/fetcher.py — Асинхронный клиент для получения данных из открытых источников.
"""
import asyncio
import re
import html
from typing import Optional, Dict, Any, Callable, Awaitable
from datetime import datetime, timezone, timedelta
from functools import wraps
from loguru import logger

import aiohttp
import feedparser
from config.settings import config


async def _with_retry(fn: Callable[[], Awaitable[Any]], retries: int = 3, delay: float = 0.5, backoff: float = 2) -> Any:
    """Universal retry wrapper for async network calls."""
    last_exc = None
    current_delay = delay
    for attempt in range(retries):
        try:
            return await fn()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt < retries - 1:
                logger.debug(f"Network attempt {attempt + 1}/{retries} failed: {e}, retrying in {current_delay}s")
                await asyncio.sleep(current_delay)
                current_delay *= backoff
    logger.warning(f"All {retries} network attempts failed: {last_exc}")
    raise last_exc


class ContextFetcher:
    """Асинхронный клиент для опроса источников данных."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=8)
        self._rss_cache: Dict[str, tuple] = {}
        self._fng_cache: tuple = (None, None)
        self._trending_cache: tuple = (None, None)
        # Последнее наблюдённое значение OI per-symbol — для расчёта дельты.
        # In-memory: после рестарта первый расчёт даст delta=0.0.
        self._last_oi: Dict[str, float] = {}
        self._MAX_OI_CACHE = 200  # cap memory usage

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_base_currency(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def _is_cached(self, cache_entry: tuple, ttl: int) -> bool:
        if cache_entry[0] is None:
            return False
        age = datetime.now(timezone.utc) - cache_entry[1]
        return age.total_seconds() < ttl

    # --- 1. Fear & Greed Index ---

    async def fetch_fear_greed(self) -> Optional[Dict[str, Any]]:
        """Alternative.me Fear & Greed Index."""
        if self._is_cached(self._fng_cache, 3600):
            return self._fng_cache[0]

        try:
            session = await self._get_session()
            url = "https://api.alternative.me/fng/?limit=1"

            async def _do():
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"Fear & Greed API returned status {resp.status}")
                        return None
                    return await resp.json()

            data = await _with_retry(_do)
            if data is None:
                return None
            result = data.get("data", [{}])[0]
            value = int(result.get("value", 0))
            label = result.get("value_classification", "Unknown")
            self._fng_cache = ({"value": value, "label": label}, datetime.now(timezone.utc))
            logger.debug(f"Fear & Greed: {value} ({label})")
            return {"value": value, "label": label}
        except Exception as e:
            logger.warning(f"Error fetching Fear & Greed: {e}")
            return None

    # --- 2. CoinGecko market data ---

    async def fetch_coingecko(self, coin_id: str) -> Optional[Dict[str, Any]]:
        """CoinGecko market data for a coin."""
        try:
            session = await self._get_session()
            url = (
                f"https://api.coingecko.com/api/v3/coins/{coin_id}"
                f"?localization=false&tickers=false&market_data=true"
                f"&community_data=false&developer_data=false"
            )
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"CoinGecko API returned status {resp.status} for {coin_id}")
                    return None
                data = await resp.json()
                market_data = data.get("market_data", {})
                result = {
                    "price_change_24h": market_data.get("price_change_percentage_24h"),
                    "price_change_7d": market_data.get("price_change_percentage_7d"),
                    "total_volume": market_data.get("total_volume", {}).get("usd"),
                    "market_cap_rank": market_data.get("market_cap_rank"),
                }
                logger.debug(f"CoinGecko {coin_id}: rank={result['market_cap_rank']}")
                return result
        except Exception as e:
            logger.warning(f"Error fetching CoinGecko for {coin_id}: {e}")
            return None

    # --- 3. CoinGecko trending coins ---

    async def fetch_trending(self) -> list:
        """CoinGecko trending coins."""
        if self._is_cached(self._trending_cache, 1800):
            return self._trending_cache[0]

        try:
            session = await self._get_session()
            url = "https://api.coingecko.com/api/v3/search/trending"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"CoinGecko trending returned status {resp.status}")
                    return []
                data = await resp.json()
                coins = []
                for item in data.get("coins", [])[:7]:
                    coin = item.get("item", {})
                    coins.append(coin.get("symbol", "").upper())
                self._trending_cache = (coins, datetime.now(timezone.utc))
                logger.debug(f"CoinGecko trending: {coins}")
                return coins
        except Exception as e:
            logger.warning(f"Error fetching CoinGecko trending: {e}")
            return []

    # --- 4. Funding Rate ---

    async def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Funding rate через ccxt unified API (поддерживает Binance, BingX, Bybit)."""
        try:
            from data.exchange_client import exchange_client
            if not exchange_client._exchange:
                return None
            exchange = exchange_client._exchange

            if not exchange.has.get("fetchFundingRate"):
                logger.debug(f"fetchFundingRate not supported by {config.exchange.name}")
                return None

            ccxt_symbol = exchange_client._resolve_symbol(symbol)
            funding = await asyncio.get_event_loop().run_in_executor(
                None, lambda: exchange.fetch_funding_rate(ccxt_symbol)
            )
            if funding and funding.get("fundingRate") is not None:
                result = float(funding["fundingRate"])
                logger.debug(f"Funding rate {symbol}: {result:.6f}")
                return result
            return None
        except Exception as e:
            logger.warning(f"Error fetching funding rate for {symbol}: {e}")
            return None

    # --- 5. Open Interest ---

    async def fetch_open_interest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Open Interest через ccxt unified API.

        Возвращает текущее абсолютное значение и % изменение относительно
        предыдущего вызова для того же символа.
        """
        try:
            from data.exchange_client import exchange_client
            if not exchange_client._exchange:
                return None
            exchange = exchange_client._exchange

            if not exchange.has.get("fetchOpenInterest"):
                logger.debug(f"fetchOpenInterest not supported by {config.exchange.name}")
                return None

            ccxt_symbol = exchange_client._resolve_symbol(symbol)
            oi_data = await asyncio.get_event_loop().run_in_executor(
                None, lambda: exchange.fetch_open_interest(ccxt_symbol)
            )
            if oi_data:
                current = float(
                    oi_data.get("openInterestAmount")
                    or oi_data.get("openInterestValue")
                    or oi_data.get("info", {}).get("openInterest", 0)
                    or 0
                )

            # Warm-up: при первом запросе для символа подтянем предыдущее значение
            # из historиical эндпоинта, чтобы delta уже на первом скане была осмысленной.
            if symbol not in self._last_oi:
                try:
                    if exchange.has.get("fetchOpenInterestHistory"):
                        hist = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: exchange.fetch_open_interest_history(
                                ccxt_symbol, timeframe="5m", limit=2
                            )
                        )
                        if isinstance(hist, list) and len(hist) >= 2:
                            prev_oi = hist[-2].get("openInterestAmount") or hist[-2].get("openInterest", 0)
                            if prev_oi:
                                self._last_oi[symbol] = float(prev_oi)
                                logger.debug(f"OI warm-up {symbol}: prev={self._last_oi[symbol]}")
                except Exception as e:
                    logger.warning(f"OI warm-up failed for {symbol}: {e}")

            previous = self._last_oi.get(symbol)
            if previous and previous > 0:
                delta_pct = (current - previous) / previous * 100.0
            else:
                delta_pct = 0.0
            self._last_oi[symbol] = current
            # Cap memory: keep only most recent symbols
            if len(self._last_oi) > self._MAX_OI_CACHE:
                # Remove oldest half
                keys = list(self._last_oi.keys())
                for k in keys[:len(keys) // 2]:
                    del self._last_oi[k]
            result = {
                "open_interest": current,
                "open_interest_delta": delta_pct,
                "is_warmup": previous is None or previous == 0,
                "timestamp": datetime.now(timezone.utc),
            }
            logger.debug(f"OI {symbol}: {current} (Δ {delta_pct:+.2f}%)")
            return result
        except Exception as e:
            logger.warning(f"Error fetching OI for {symbol}: {e}")
            return None

    # --- 6. Long/Short Ratio ---

    async def fetch_long_short_ratio(self, symbol: str) -> Optional[float]:
        """Long/Short Account Ratio.

        BingX: эндпоинт topLongShortRatio был удалён из API — возвращаем None.
        Binance: прямой HTTP к fapi.binance.com/futures/data/globalLongShortAccountRatio
        """
        try:
            session = await self._get_session()
            exchange_name = config.exchange.name

            if exchange_name == "bingx":
                # BingX deprecated this endpoint — no data available
                return None
            else:
                # Binance fallback
                binance_symbol = symbol.replace("/", "")
                url = (
                    f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                    f"?symbol={binance_symbol}&period=1h&limit=1"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"Long/Short Ratio API returned status {resp.status}")
                        return None
                    data = await resp.json()
                    if data:
                        ratio = float(data[0].get("longShortRatio", 0))
                        logger.debug(f"Long/Short ratio {symbol}: {ratio}")
                        return ratio
                    return None
        except Exception as e:
            logger.warning(f"Error fetching long/short ratio for {symbol}: {e}")
            return None

    # --- 7. CryptoPanic news ---

    async def fetch_cryptopanic(self, symbol: str) -> Optional[Dict[str, Any]]:
        """CryptoPanic news sentiment (requires API key)."""
        if not config.cryptopanic_api_key:
            return None

        base_currency = self._get_base_currency(symbol)
        try:
            session = await self._get_session()
            url = (
                f"https://cryptopanic.com/api/v1/posts/"
                f"?auth_token={config.cryptopanic_api_key}"
                f"&currencies={base_currency}"
                f"&filter=important&public=true"
            )
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"CryptoPanic API returned status {resp.status}")
                    return None
                data = await resp.json()
                posts = data.get("results", [])
                positive = 0
                negative = 0
                for post in posts:
                    votes = post.get("votes", {})
                    pos = votes.get("positive", 0)
                    neg = votes.get("negative", 0)
                    if pos > neg:
                        positive += 1
                    elif neg > pos:
                        negative += 1
                total = positive + negative
                if total == 0:
                    score = 0.0
                else:
                    score = (positive - negative) / total
                logger.debug(f"CryptoPanic {symbol}: score={score:.2f} (pos={positive}, neg={negative})")
                return {"score": score, "count": len(posts), "positive": positive, "negative": negative}
        except Exception as e:
            logger.warning(f"Error fetching CryptoPanic for {symbol}: {e}")
            return None

    # --- 8. RSS feeds ---

    async def fetch_rss_news(self, symbol: str) -> Optional[Dict[str, Any]]:
        """RSS news from CoinDesk and Cointelegraph."""
        base_currency = self._get_base_currency(symbol)

        # Check cache
        cache_key = f"rss_{base_currency}"
        if self._is_cached(self._rss_cache.get(cache_key, (None, None)), 300):
            return self._rss_cache.get(cache_key, (None, None))[0]

        negative_words = [
            "hack", "crash", "ban", "lawsuit", "exploit", "fraud", "scam",
            "liquidation", "sec", "charge", "sued", "fine", "collapse",
            "bankruptcy", "rug", "pump", "dump",
        ]
        positive_words = [
            "partnership", "launch", "upgrade", "adoption", "etf", "approval",
            "listing", "integration", "milestone", "record", "growth",
            "bullish", "surge", "rally", "high", "all-time",
        ]

        try:
            session = await self._get_session()
            rss_urls = [
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "https://cointelegraph.com/rss",
            ]
            positive_count = 0
            negative_count = 0
            total_articles = 0

            for url in rss_urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text(errors="replace")
                        feed = feedparser.parse(text)
                        for entry in feed.entries[:20]:
                            title = entry.get("title", "").lower()
                            if not re.search(rf"\b{re.escape(base_currency.lower())}\b", title):
                                continue
                            total_articles += 1
                            title_clean = re.sub(rf"\b{re.escape(base_currency.lower())}\b", "", title)
                            for word in negative_words:
                                if word in title_clean:
                                    negative_count += 1
                                    break
                            else:
                                for word in positive_words:
                                    if word in title_clean:
                                        positive_count += 1
                                        break
                except Exception as e:
                    logger.warning(f"RSS fetch error for {url}: {e}")
                    continue

            if total_articles == 0:
                return None

            score = (positive_count - negative_count) / total_articles if total_articles > 0 else 0.0
            result = {
                "score": score,
                "count": total_articles,
                "positive": positive_count,
                "negative": negative_count,
            }
            self._rss_cache[cache_key] = (result, datetime.now(timezone.utc))
            logger.debug(f"RSS {symbol}: score={score:.2f} (articles={total_articles})")
            return result
        except Exception as e:
            logger.warning(f"Error fetching RSS for {symbol}: {e}")
            return None


# Singleton
context_fetcher = ContextFetcher()
