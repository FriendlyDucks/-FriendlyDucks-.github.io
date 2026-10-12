"""Pappershandel (simulerad mäklare). Inga riktiga pengar används."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Position:
    shares: int
    entry_price: float
    stop_loss: float
    take_profit: float


@dataclass
class PaperBroker:
    cash: float = 100_000.0
    start_of_day_equity: float = 100_000.0
    day: str = ""
    positions: dict[str, Position] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    path: Path = Path("portfolio.json")

    @classmethod
    def load(cls, path: Path, starting_cash: float) -> "PaperBroker":
        if not path.exists():
            return cls(cash=starting_cash, start_of_day_equity=starting_cash, path=path)
        data = json.loads(path.read_text())
        positions = {k: Position(**v) for k, v in data.pop("positions").items()}
        return cls(**data, positions=positions, path=path)

    def save(self) -> None:
        data = asdict(self)
        data.pop("path")
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.shares * prices.get(t, p.entry_price) for t, p in self.positions.items())
        return self.cash + held

    def new_day_check(self, prices: dict[str, float]) -> None:
        today = datetime.now().date().isoformat()
        if self.day != today:
            self.day = today
            self.start_of_day_equity = self.equity(prices)

    def buy(self, ticker: str, shares: int, price: float, stop_pct: float, take_pct: float, reason: str) -> None:
        cost = shares * price
        if shares <= 0 or cost > self.cash:
            return
        self.cash -= cost
        pos = self.positions.get(ticker)
        if pos:
            total = pos.shares + shares
            pos.entry_price = (pos.entry_price * pos.shares + cost) / total
            pos.shares = total
        else:
            self.positions[ticker] = Position(
                shares=shares,
                entry_price=price,
                stop_loss=price * (1 - stop_pct),
                take_profit=price * (1 + take_pct),
            )
        self._record("KÖP", ticker, shares, price, reason)

    def sell_all(self, ticker: str, price: float, reason: str) -> None:
        pos = self.positions.pop(ticker, None)
        if not pos:
            return
        self.cash += pos.shares * price
        pnl = (price - pos.entry_price) * pos.shares
        self._record("SÄLJ", ticker, pos.shares, price, f"{reason} (resultat {pnl:+.2f})")

    def _record(self, side: str, ticker: str, shares: int, price: float, reason: str) -> None:
        entry = {
            "tid": datetime.now().isoformat(timespec="seconds"),
            "typ": side,
            "ticker": ticker,
            "antal": shares,
            "pris": round(price, 4),
            "motivering": reason,
        }
        self.log.append(entry)
        print(f"  >>> {side} {shares} st {ticker} @ {price:.2f} – {reason}")
