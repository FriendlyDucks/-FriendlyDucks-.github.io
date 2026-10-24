"""Marknadsdata: Nordnet (realtid via feed), Twelve Data eller EODHD."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from orders import Quote, Tradable

QuoteListener = Callable[[Tradable, Quote], None]


@dataclass
class Snapshot:
    tradable: Tradable
    source: str
    quote: Quote
    change_pct_today: float
    rsi_14: float
    sma_20: float
    sma_50: float
    vwap: float
    volume_vs_avg: float
    day_high: float
    day_low: float
    bars: int
    headlines: list[str]

    @property
    def price(self) -> float:
        return self.quote.last or self.quote.mid

    def to_text(self) -> str:
        q = self.quote
        spread = (q.ask - q.bid) / q.mid * 100 if q.bid and q.ask else 0.0
        lines = [
            f"Värdepapper: {self.tradable.symbol} ({self.tradable.currency}), datakälla: {self.source}",
            f"Senast: {q.last:.2f}  Köp (bid): {q.bid:.2f} x {q.bid_volume:.0f}  Sälj (ask): {q.ask:.2f} x {q.ask_volume:.0f}  Spread: {spread:.2f} %",
            f"Förändring idag: {self.change_pct_today:+.2f} %",
            f"Dagens högsta/lägsta: {self.day_high:.2f} / {self.day_low:.2f}",
            f"RSI(14, 1m): {self.rsi_14:.1f}",
            f"SMA20 / SMA50 (1m): {self.sma_20:.2f} / {self.sma_50:.2f}",
            f"VWAP idag: {self.vwap:.2f}",
            f"Volym senaste minuten jämfört med snitt: {self.volume_vs_avg:.2f}x",
            f"Antal minutstaplar i underlaget: {self.bars}",
            "Rubriker:",
        ]
        lines += [f"  - {h}" for h in self.headlines] or ["  (inga)"]
        return "\n".join(lines)


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    value = (100 - 100 / (1 + rs)).iloc[-1]
    return float(value) if pd.notna(value) else 50.0


def build_snapshot(
    tradable: Tradable, source: str, quote: Quote, bars: pd.DataFrame, ref_close: float, headlines: list[str]
) -> Snapshot:
    """bars: minutstaplar för dagen med kolumnerna Open, High, Low, Close, Volume."""
    price = quote.last or quote.mid
    if bars.empty:
        bars = pd.DataFrame({"Open": [price], "High": [price], "Low": [price], "Close": [price], "Volume": [0]})
    close = bars["Close"]
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    volume = bars["Volume"]
    vwap = float((typical * volume).sum() / volume.sum()) if volume.sum() else price
    avg_vol = float(volume.mean()) or 1.0
    return Snapshot(
        tradable=tradable,
        source=source,
        quote=quote,
        change_pct_today=(price / ref_close - 1) * 100 if ref_close else 0.0,
        rsi_14=_rsi(close),
        sma_20=float(close.tail(20).mean()),
        sma_50=float(close.tail(50).mean()),
        vwap=vwap,
        volume_vs_avg=float(volume.iloc[-1]) / avg_vol,
        day_high=float(bars["High"].max()),
        day_low=float(bars["Low"].min()),
        bars=len(bars),
        headlines=headlines,
    )


class MarketData:
    """Gemensamt gränssnitt för datakällor."""

    name = "okänd"

    def __init__(self) -> None:
        self.listeners: list[QuoteListener] = []

    def add_listener(self, fn: QuoteListener) -> None:
        self.listeners.append(fn)

    def _emit(self, tradable: Tradable, quote: Quote) -> None:
        for fn in self.listeners:
            fn(tradable, quote)

    def start(self, tradables: list[Tradable]) -> None: ...
    def quote(self, tradable: Tradable) -> Optional[Quote]: ...
    def snapshot(self, tradable: Tradable) -> Snapshot: ...
    def poll(self) -> None:
        """Anropas regelbundet av huvudloopen (behövs bara för källor utan push)."""


class NordnetMarketData(MarketData):
    """Realtidsdata från Nordnets publika feed. Minutstaplar byggs av avslutsflödet."""

    name = "Nordnet (realtid)"

    def __init__(self, client) -> None:
        super().__init__()
        from nordnet.feed import Feed

        self.client = client
        info = client.login_info["public_feed"]
        self.feed = Feed(info["hostname"], int(info["port"]), client.session_key, self._on_message, info.get("encrypted", True))
        self.by_key: dict[str, Tradable] = {}
        self.quotes: dict[str, Quote] = {}
        self.ref_close: dict[str, float] = {}
        self.trades: dict[str, list[tuple[float, float, float]]] = defaultdict(list)  # (ts, pris, volym)
        self.lock = threading.Lock()

    def start(self, tradables: list[Tradable]) -> None:
        for t in tradables:
            self.by_key[t.key] = t
            self._seed(t)
        self.feed.start()
        for t in tradables:
            self.feed.subscribe("price", t.market_id, t.identifier)
            self.feed.subscribe("trade", t.market_id, t.identifier)

    def _seed(self, t: Tradable) -> None:
        try:
            info = self.client.tradable_info(t).get("price_info", {}) or {}
            self.ref_close[t.key] = float(info.get("close") or 0)
            if info.get("last"):
                self.quotes[t.key] = Quote(
                    bid=float(info.get("bid") or 0), ask=float(info.get("ask") or 0), last=float(info["last"]),
                    bid_volume=float(info.get("bid_volume") or 0), ask_volume=float(info.get("ask_volume") or 0),
                    timestamp=time.time(),
                )
        except Exception as e:
            print(f"[nordnet] kunde inte hämta info för {t.symbol}: {e}")
        try:
            for tr in self.client.public_trades(t):
                self.trades[t.key].append((tr["trade_timestamp"] / 1000, float(tr["price"]), float(tr["volume"])))
            self.trades[t.key].sort()
        except Exception as e:
            print(f"[nordnet] kunde inte hämta dagens avslut för {t.symbol}: {e}")

    def _on_message(self, kind: str, data: dict) -> None:
        key = f"{data.get('m')}:{data.get('i')}"
        t = self.by_key.get(key)
        if t is None:
            return
        with self.lock:
            if kind == "price":
                old = self.quotes.get(key) or Quote(0, 0, 0)
                q = Quote(
                    bid=float(data.get("bid", old.bid) or 0),
                    ask=float(data.get("ask", old.ask) or 0),
                    last=float(data.get("last", old.last) or 0),
                    bid_volume=float(data.get("bid_volume", old.bid_volume) or 0),
                    ask_volume=float(data.get("ask_volume", old.ask_volume) or 0),
                    timestamp=time.time(),
                )
                if data.get("close"):
                    self.ref_close[key] = float(data["close"])
                self.quotes[key] = q
            elif kind == "trade":
                ts = float(data.get("trade_timestamp", time.time() * 1000)) / 1000
                self.trades[key].append((ts, float(data["price"]), float(data.get("volume", 0))))
                q = self.quotes.get(key)
                if q:
                    q.last = float(data["price"])
            else:
                return
            quote = self.quotes.get(key)
        if quote:
            self._emit(t, quote)

    def quote(self, tradable: Tradable) -> Optional[Quote]:
        return self.quotes.get(tradable.key)

    def _bars(self, key: str) -> pd.DataFrame:
        with self.lock:
            rows = list(self.trades[key])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["ts", "price", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="s")
        today = df[df.index.date == df.index[-1].date()]
        bars = today["price"].resample("1min").ohlc().dropna()
        bars.columns = ["Open", "High", "Low", "Close"]
        bars["Volume"] = today["volume"].resample("1min").sum()
        return bars.dropna()

    def _headlines(self) -> list[str]:
        try:
            items = self.client.news(limit=5) or []
            return [n.get("headline") or n.get("title", "") for n in items if n.get("headline") or n.get("title")]
        except Exception:
            return []

    def snapshot(self, tradable: Tradable) -> Snapshot:
        quote = self.quote(tradable)
        if quote is None:
            raise RuntimeError(f"Inga kurser från Nordnet för {tradable.symbol} än")
        return build_snapshot(
            tradable, self.name, quote, self._bars(tradable.key), self.ref_close.get(tradable.key, 0.0), self._headlines()
        )


class PollingMarketData(MarketData):
    """Bas för datakällor som hämtas med jämna mellanrum över HTTP (utan orderbok)."""

    refresh_seconds = 60

    def __init__(self, api_key: str, spread_pct: float = 0.10) -> None:
        super().__init__()
        if not api_key:
            raise RuntimeError(f"API-nyckel saknas för {self.name}")
        self.api_key = api_key
        self.spread_pct = spread_pct
        self.tradables: list[Tradable] = []
        self.quotes: dict[str, Quote] = {}
        self.bars: dict[str, tuple[float, pd.DataFrame, float]] = {}  # nyckel -> (hämtad, dagens staplar, gårdagens stängning)
        self.news: dict[str, tuple[float, list[str]]] = {}
        self.requests_today = 0
        self.last_error = ""

    def start(self, tradables: list[Tradable]) -> None:
        self.tradables = list(tradables)
        self.poll()

    def _get(self, url: str, params: dict) -> object:
        import requests

        self.requests_today += 1
        r = requests.get(url, params=params, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.name} svarade {r.status_code}: {r.text[:200]}")
        return r.json()

    def _fetch(self, t: Tradable) -> tuple[pd.DataFrame, float, Optional[float]]:
        """Returnerar (dagens minutstaplar, gårdagens stängningskurs, senaste pris om separat)."""
        raise NotImplementedError

    def _fetch_news(self, t: Tradable) -> list[str]:
        return []

    @staticmethod
    def _split_days(bars: pd.DataFrame) -> tuple[pd.DataFrame, float]:
        bars = bars.sort_index()
        last_day = bars.index[-1].date()
        today = bars[bars.index.date == last_day]
        before = bars[bars.index.date < last_day]["Close"]
        ref = float(before.iloc[-1]) if not before.empty else float(today["Open"].iloc[0])
        return today, ref

    def poll(self) -> None:
        for t in self.tradables:
            cached = self.bars.get(t.key)
            if cached and time.time() - cached[0] < self.refresh_seconds:
                continue
            try:
                today, ref, last = self._fetch(t)
                self.bars[t.key] = (time.time(), today, ref)
                self.last_error = ""
            except Exception as e:
                self.last_error = f"{t.symbol}: {e}"
                print(f"[{self.name}] {self.last_error}")
                continue
            price = last if last else float(today["Close"].iloc[-1])
            half = price * self.spread_pct / 100 / 2
            q = Quote(bid=price - half, ask=price + half, last=price, timestamp=time.time())
            self.quotes[t.key] = q
            self._emit(t, q)

    def quote(self, tradable: Tradable) -> Optional[Quote]:
        return self.quotes.get(tradable.key)

    def _headlines(self, t: Tradable) -> list[str]:
        cached = self.news.get(t.key)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        try:
            items = self._fetch_news(t)
        except Exception:
            items = []
        self.news[t.key] = (time.time(), items)
        return items

    def snapshot(self, tradable: Tradable) -> Snapshot:
        cached = self.bars.get(tradable.key)
        quote = self.quotes.get(tradable.key)
        if not cached or quote is None:
            raise RuntimeError(f"Inga kurser för {tradable.symbol} än ({self.last_error or 'väntar på data'})")
        return build_snapshot(tradable, self.name, quote, cached[1], cached[2], self._headlines(tradable))


class TwelveDataMarketData(PollingMarketData):
    """Twelve Data (twelvedata.com). Stockholmsbörsen (XSTO) i realtid kräver Pro-planen;
    med gratisnyckeln fungerar testaktien INVE.B."""

    name = "Twelve Data"
    url = "https://api.twelvedata.com"

    def __init__(self, api_key: str, spread_pct: float = 0.10, exchange: str = "XSTO") -> None:
        super().__init__(api_key, spread_pct)
        self.exchange = exchange

    @staticmethod
    def symbol_for(t: Tradable) -> str:
        return t.data_symbol or t.symbol.replace(" ", ".")

    def _fetch(self, t: Tradable) -> tuple[pd.DataFrame, float, Optional[float]]:
        data = self._get(f"{self.url}/time_series", {
            "symbol": self.symbol_for(t), "exchange": self.exchange, "interval": "1min",
            "outputsize": 1000, "timezone": "Europe/Stockholm", "apikey": self.api_key,
        })
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise RuntimeError(data.get("message", data) if isinstance(data, dict) else data)
        df = pd.DataFrame(data["values"])
        df.index = pd.to_datetime(df["datetime"])
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df.columns = ["Open", "High", "Low", "Close", "Volume"]
        today, ref = self._split_days(df)
        return today, ref, None


class EODHDMarketData(PollingMarketData):
    """EOD Historical Data (eodhd.com). Stockholmsaktier har cirka 15 minuters fördröjning."""

    name = "EODHD (fördröjd ~15 min)"
    url = "https://eodhd.com/api"

    @staticmethod
    def symbol_for(t: Tradable) -> str:
        return t.data_symbol or t.symbol.replace(" ", "-") + ".ST"

    def _fetch(self, t: Tradable) -> tuple[pd.DataFrame, float, Optional[float]]:
        sym = self.symbol_for(t)
        rows = self._get(f"{self.url}/intraday/{sym}", {
            "interval": "1m", "from": int(time.time()) - 4 * 86400, "api_token": self.api_key, "fmt": "json",
        })
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"inga minutdata för {sym}: {rows}")
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["timestamp"], unit="s", utc=True).tz_convert("Europe/Stockholm").tz_localize(None)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df.columns = ["Open", "High", "Low", "Close", "Volume"]
        today, ref = self._split_days(df)
        rt = self._get(f"{self.url}/real-time/{sym}", {"api_token": self.api_key, "fmt": "json"})
        last = None
        if isinstance(rt, dict):
            try:
                last = float(rt["close"])
                ref = float(rt.get("previousClose") or ref)
            except (KeyError, TypeError, ValueError):
                pass
        return today, ref, last

    def _fetch_news(self, t: Tradable) -> list[str]:
        items = self._get(f"{self.url}/news", {"s": self.symbol_for(t), "limit": 5, "api_token": self.api_key, "fmt": "json"})
        return [i["title"] for i in items if isinstance(i, dict) and i.get("title")] if isinstance(items, list) else []


def market_is_open(now: Optional[datetime] = None, open_hm: str = "09:00", close_hm: str = "17:30") -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return open_hm <= hm < close_hm
