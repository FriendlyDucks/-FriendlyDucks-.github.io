"""Marknadsdata: hämtar intradagskurser och räknar ut enkla indikatorer."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import yfinance as yf


@dataclass
class Snapshot:
    ticker: str
    price: float
    change_pct_today: float
    rsi_14: float
    sma_20: float
    sma_50: float
    vwap: float
    volume_vs_avg: float
    day_high: float
    day_low: float
    headlines: list[str]

    def to_text(self) -> str:
        lines = [
            f"Ticker: {self.ticker}",
            f"Senaste pris: {self.price:.2f}",
            f"Förändring idag: {self.change_pct_today:+.2f} %",
            f"Dagens högsta/lägsta: {self.day_high:.2f} / {self.day_low:.2f}",
            f"RSI(14, 5m): {self.rsi_14:.1f}",
            f"SMA20 / SMA50 (5m): {self.sma_20:.2f} / {self.sma_50:.2f}",
            f"VWAP idag: {self.vwap:.2f}",
            f"Volym senaste 5m jämfört med snitt: {self.volume_vs_avg:.2f}x",
            "Rubriker:",
        ]
        lines += [f"  - {h}" for h in self.headlines] or ["  (inga)"]
        return "\n".join(lines)


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    value = rsi.iloc[-1]
    return float(value) if pd.notna(value) else 50.0


def _headlines(ticker: yf.Ticker, limit: int = 5) -> list[str]:
    # yfinance ändrar formatet på nyheter ibland, så läs defensivt.
    try:
        items = ticker.news or []
    except Exception:
        return []
    out = []
    for item in items[:limit]:
        title = item.get("title") or item.get("content", {}).get("title")
        if title:
            out.append(title)
    return out


def get_snapshot(symbol: str) -> Snapshot:
    ticker = yf.Ticker(symbol)
    bars = ticker.history(period="5d", interval="5m")
    if bars.empty:
        raise RuntimeError(f"Ingen kursdata för {symbol}")

    last_day = bars.index[-1].date()
    today = bars[bars.index.date == last_day]
    prev_close = bars[bars.index.date < last_day]["Close"]
    ref = float(prev_close.iloc[-1]) if not prev_close.empty else float(today["Open"].iloc[0])

    close = bars["Close"]
    typical = (today["High"] + today["Low"] + today["Close"]) / 3
    vwap = float((typical * today["Volume"]).sum() / max(today["Volume"].sum(), 1))
    avg_vol = float(bars["Volume"].tail(100).mean()) or 1.0
    price = float(close.iloc[-1])

    return Snapshot(
        ticker=symbol,
        price=price,
        change_pct_today=(price / ref - 1) * 100,
        rsi_14=_rsi(close),
        sma_20=float(close.tail(20).mean()),
        sma_50=float(close.tail(50).mean()),
        vwap=vwap,
        volume_vs_avg=float(bars["Volume"].iloc[-1]) / avg_vol,
        day_high=float(today["High"].max()),
        day_low=float(today["Low"].min()),
        headlines=_headlines(ticker),
    )
