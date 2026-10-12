"""Pappershandel som efterliknar Nordnets ordermodell.

Ordern simuleras mot riktiga köp-/säljkurser (bid/ask) från datakällan:
- LIMIT / NORMAL: ligger kvar tills priset nås eller dagen tar slut (DAY).
- FAK: fyller det som går direkt mot motsatt sida, resten makuleras.
- FOK: allt eller inget, direkt.
- STOP_ACTPRICE: vilande tills senast betalt når triggerpriset, blir sedan en limitorder på `price`.
- STOP_ACTPRICE_PERC (trailing): följer kursen; triggas när den rör sig `trigger_value` % mot dig.
  Limitpriset blir triggerpriset justerat med `target_value` % (antagande, kontrollera mot Nordnet).
- OCO_STOP_ACTPRICE: limitorder på `price` (t.ex. vinstmål) plus stop-loss. Triggas stoppen
  ersätts limitordern av en order på `target_value`.
Blankning: SELL utan innehav ger negativt innehav om blankning är tillåten för papperet.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from orders import Activation, OrderRequest, OrderType, Quote, Side, Tradable, Validity


@dataclass
class PaperOrder:
    order_id: str
    tradable: Tradable
    side: Side
    volume: int
    price: Optional[float]
    order_type: OrderType
    validity: Validity
    activation: Activation
    trigger_value: Optional[float]
    trigger_condition: Optional[str]
    target_value: Optional[float]
    reference: Optional[str]
    state: str = "ON_MARKET"  # WAITING (vilande), ON_MARKET, DONE, DELETED
    extreme: Optional[float] = None  # För trailing stop
    note: str = ""


@dataclass
class PaperPosition:
    qty: int = 0  # Negativt = blankat
    avg_price: float = 0.0


@dataclass
class CostModel:
    courtage_pct: float = 0.0025
    courtage_min: float = 1.0
    short_margin: float = 1.5  # Eget kapital måste vara minst så här mycket gånger blankat värde

    def courtage(self, value: float) -> float:
        return max(self.courtage_min, value * self.courtage_pct)


class PaperBroker:
    name = "Pappershandel"

    def __init__(
        self,
        path: Path,
        starting_cash: float,
        costs: CostModel,
        allow_short: bool = True,
        shortable: Optional[set[str]] = None,
    ) -> None:
        self.path = path
        self.costs = costs
        self.allow_short = allow_short
        self.shortable = shortable  # None = alla får blankas
        self.cash = starting_cash
        self.positions: dict[str, PaperPosition] = {}
        self.tradables: dict[str, Tradable] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.log: list[dict] = []
        self.realized_today = 0.0
        self.day = ""
        self.start_of_day_equity = starting_cash
        self.last_quotes: dict[str, Quote] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._load()

    # ---------- Persistens ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        self.cash = data["cash"]
        self.realized_today = data.get("realized_today", 0.0)
        self.day = data.get("day", "")
        self.start_of_day_equity = data.get("start_of_day_equity", self.cash)
        self.log = data.get("log", [])
        for key, t in data.get("tradables", {}).items():
            self.tradables[key] = Tradable(**t)
        for key, p in data.get("positions", {}).items():
            self.positions[key] = PaperPosition(**p)
        for o in data.get("orders", []):
            o["tradable"] = Tradable(**o["tradable"])
            for name, enum in (("side", Side), ("order_type", OrderType), ("validity", Validity), ("activation", Activation)):
                o[name] = enum(o[name])
            self.orders[o["order_id"]] = PaperOrder(**o)
        if self.orders:
            self._ids = itertools.count(max(int(i.split("-")[1]) for i in self.orders) + 1)

    def save(self) -> None:
        with self._lock:
            active = [o for o in self.orders.values() if o.state in ("WAITING", "ON_MARKET")]
            data = {
                "cash": self.cash,
                "realized_today": self.realized_today,
                "day": self.day,
                "start_of_day_equity": self.start_of_day_equity,
                "tradables": {k: asdict(t) for k, t in self.tradables.items()},
                "positions": {k: asdict(p) for k, p in self.positions.items() if p.qty},
                "orders": [{**asdict(o), "side": o.side.value, "order_type": o.order_type.value,
                            "validity": o.validity.value, "activation": o.activation.value} for o in active],
                "log": self.log[-500:],
            }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # ---------- Konto ----------

    def position(self, tradable: Tradable) -> PaperPosition:
        return self.positions.get(tradable.key, PaperPosition())

    def equity(self) -> float:
        with self._lock:
            value = self.cash
            for key, p in self.positions.items():
                q = self.last_quotes.get(key)
                value += p.qty * (q.last if q else p.avg_price)
            return value

    def short_exposure(self, extra: float = 0.0) -> float:
        total = extra
        for key, p in self.positions.items():
            if p.qty < 0:
                q = self.last_quotes.get(key)
                total += -p.qty * (q.last if q else p.avg_price)
        return total

    def open_orders(self, tradable: Optional[Tradable] = None) -> list[PaperOrder]:
        return [
            o for o in self.orders.values()
            if o.state in ("WAITING", "ON_MARKET") and (tradable is None or o.tradable.key == tradable.key)
        ]

    def new_day_check(self) -> None:
        today = datetime.now().date().isoformat()
        if self.day != today:
            for o in self.open_orders():
                if o.validity == Validity.DAY:
                    o.state, o.note = "DELETED", "dagsorder utgången"
            self.day = today
            self.realized_today = 0.0
            self.start_of_day_equity = self.equity()

    # ---------- Order ----------

    def place(self, req: OrderRequest) -> PaperOrder:
        with self._lock:
            t = req.tradable
            self.tradables[t.key] = t
            order = PaperOrder(
                order_id=f"P-{next(self._ids)}", tradable=t, side=req.side, volume=req.volume, price=req.price,
                order_type=req.order_type, validity=req.validity, activation=req.activation,
                trigger_value=req.trigger_value, trigger_condition=req.trigger_condition,
                target_value=req.target_value, reference=req.reference,
            )
            error = self._validate(order)
            if error:
                order.state, order.note = "DELETED", error
                self._record("AVVISAD", order, 0, 0.0, error)
                return order
            if order.activation in (Activation.STOP_ACTPRICE, Activation.STOP_ACTPRICE_PERC, Activation.MANUAL):
                order.state = "WAITING"
            self.orders[order.order_id] = order
            self._record("LAGD", order, order.volume, order.price or 0.0, self._describe(order))
            q = self.last_quotes.get(t.key)
            if q:
                self._process(order, q, first=True)
            return order

    def cancel(self, order_id: str, why: str = "makulerad") -> None:
        with self._lock:
            o = self.orders.get(order_id)
            if o and o.state in ("WAITING", "ON_MARKET"):
                o.state, o.note = "DELETED", why
                self._record("MAKULERAD", o, o.volume, o.price or 0.0, why)

    def cancel_all(self, tradable: Optional[Tradable] = None, why: str = "makulerad") -> None:
        for o in self.open_orders(tradable):
            self.cancel(o.order_id, why)

    def _validate(self, o: PaperOrder) -> Optional[str]:
        if o.volume <= 0 or o.volume % max(o.tradable.lot_size, 1):
            return f"ogiltig volym {o.volume} (handelspost {o.tradable.lot_size})"
        needs_price = o.activation not in (Activation.STOP_ACTPRICE_PERC,)
        if needs_price and not o.price:
            return "pris saknas"
        if o.activation in (Activation.STOP_ACTPRICE, Activation.OCO_STOP_ACTPRICE) and (
            o.trigger_value is None or o.trigger_condition not in ("<=", ">=")
        ):
            return "stop-order kräver trigger_value och trigger_condition (<= eller >=)"
        if o.activation == Activation.STOP_ACTPRICE_PERC and not o.trigger_value:
            return "trailing stop kräver trigger_value (procent)"

        pos = self.position(o.tradable)
        q = self.last_quotes.get(o.tradable.key)
        ref_price = o.price or (q.last if q else pos.avg_price) or 0.0
        if o.side == Side.SELL and o.volume > max(pos.qty, 0) and o.activation == Activation.NONE:
            if not self.allow_short:
                return "blankning är avstängd"
            if self.shortable is not None and o.tradable.symbol not in self.shortable:
                return f"{o.tradable.symbol} får inte blankas"
            new_short = (o.volume - max(pos.qty, 0)) * ref_price
            if self.short_exposure(new_short) * self.costs.short_margin > self.equity():
                return "otillräcklig säkerhet för blankning"
        if o.side == Side.BUY and pos.qty >= 0 and o.activation == Activation.NONE:
            reserved = sum(
                (x.price or 0) * x.volume for x in self.open_orders()
                if x.side == Side.BUY and x.state == "ON_MARKET" and self.position(x.tradable).qty >= 0
            )
            cost = ref_price * o.volume
            if cost + self.costs.courtage(cost) + reserved > self.cash:
                return "otillräcklig köpkraft"
        return None

    # ---------- Simulering ----------

    def on_quote(self, tradable: Tradable, quote: Quote) -> None:
        with self._lock:
            self.last_quotes[tradable.key] = quote
            for o in self.open_orders(tradable):
                self._process(o, quote)

    def _crossed(self, last: float, cond: Optional[str], level: float) -> bool:
        return last <= level if cond == "<=" else last >= level

    def _process(self, o: PaperOrder, q: Quote, first: bool = False) -> None:
        last = q.last or q.mid
        if o.state == "WAITING":
            if o.activation == Activation.STOP_ACTPRICE:
                if self._crossed(last, o.trigger_condition, o.trigger_value):
                    o.state, o.note = "ON_MARKET", f"stop utlöst vid {last:.2f}"
                    self._record("AKTIVERAD", o, o.volume, last, o.note)
            elif o.activation == Activation.STOP_ACTPRICE_PERC:
                # Säljorder följer högsta kursen, köporder (täcka blankning) följer lägsta.
                if o.extreme is None:
                    o.extreme = last
                o.extreme = max(o.extreme, last) if o.side == Side.SELL else min(o.extreme, last)
                pct = o.trigger_value / 100
                trigger = o.extreme * (1 - pct) if o.side == Side.SELL else o.extreme * (1 + pct)
                if (o.side == Side.SELL and last <= trigger) or (o.side == Side.BUY and last >= trigger):
                    offset = (o.target_value or 0) / 100
                    o.price = trigger * (1 - offset) if o.side == Side.SELL else trigger * (1 + offset)
                    o.state, o.note = "ON_MARKET", f"trailing stop utlöst vid {last:.2f}"
                    self._record("AKTIVERAD", o, o.volume, last, o.note)
            if o.state == "WAITING":
                return
        elif o.activation == Activation.OCO_STOP_ACTPRICE and not o.note.startswith("stop"):
            if self._crossed(last, o.trigger_condition, o.trigger_value):
                o.price = o.target_value
                o.note = f"stop-del utlöst vid {last:.2f}, vinstmålet makulerat"
                self._record("AKTIVERAD", o, o.volume, last, o.note)

        # Är ordern säljbar/köpbar mot orderboken?
        if o.side == Side.BUY:
            book, book_vol = (q.ask or last), q.ask_volume
            marketable = o.price is not None and o.price >= book
        else:
            book, book_vol = (q.bid or last), q.bid_volume
            marketable = o.price is not None and o.price <= book

        if o.order_type in (OrderType.FAK, OrderType.FOK) and first:
            if not marketable or (o.order_type == OrderType.FOK and book_vol and book_vol < o.volume):
                o.state, o.note = "DELETED", f"{o.order_type.value}: kunde inte fyllas direkt"
                self._record("MAKULERAD", o, o.volume, book, o.note)
                return
            if o.order_type == OrderType.FAK and book_vol and book_vol < o.volume:
                lot = max(o.tradable.lot_size, 1)
                o.volume = int(book_vol // lot * lot) or o.volume
        if marketable:
            self._fill(o, book)

    def _fill(self, o: PaperOrder, price: float) -> None:
        key = o.tradable.key
        pos = self.positions.setdefault(key, PaperPosition())
        value = price * o.volume
        fee = self.costs.courtage(value)
        signed = o.volume if o.side == Side.BUY else -o.volume

        realized = 0.0
        if pos.qty and (pos.qty > 0) != (signed > 0):  # Minskar/stänger befintlig position
            closing = min(abs(pos.qty), abs(signed))
            direction = 1 if pos.qty > 0 else -1
            realized = (price - pos.avg_price) * closing * direction
        new_qty = pos.qty + signed
        if new_qty == 0:
            pos.avg_price = 0.0
        elif pos.qty == 0 or (pos.qty > 0) != (new_qty > 0):
            pos.avg_price = price  # Ny position eller vänt från lång till kort
        elif (pos.qty > 0) == (signed > 0):
            pos.avg_price = (pos.avg_price * abs(pos.qty) + value) / abs(new_qty)
        pos.qty = new_qty

        self.cash += -value - fee if o.side == Side.BUY else value - fee
        self.realized_today += realized - fee
        o.state = "DONE"
        self._record("AVSLUT", o, o.volume, price, f"courtage {fee:.2f}, realiserat {realized:+.2f}")

        # Stängd position -> makulera kvarvarande skyddsorder i samma papper.
        if pos.qty == 0:
            for other in self.open_orders(o.tradable):
                self.cancel(other.order_id, "positionen är stängd")

    # ---------- Loggning ----------

    def _describe(self, o: PaperOrder) -> str:
        parts = [o.order_type.value, o.validity.value]
        if o.activation != Activation.NONE:
            parts.append(f"{o.activation.value} trigger {o.trigger_condition or ''}{o.trigger_value} mål {o.target_value}")
        if o.reference:
            parts.append(o.reference)
        return ", ".join(parts)

    def _record(self, event: str, o: PaperOrder, volume: int, price: float, text: str) -> None:
        entry = {
            "tid": datetime.now().isoformat(timespec="seconds"),
            "händelse": event,
            "order": o.order_id,
            "papper": o.tradable.symbol,
            "sida": o.side.value,
            "antal": volume,
            "pris": round(price, 4),
            "info": text,
        }
        self.log.append(entry)
        print(f"  [{self.name}] {event} {o.order_id} {o.side.value} {volume} {o.tradable.symbol} @ {price:.2f} – {text}")

    def summary_text(self) -> str:
        with self._lock:
            lines = [
                f"Kontanter: {self.cash:,.2f}",
                f"Totalt värde (eget kapital): {self.equity():,.2f}",
                f"Realiserat idag (efter courtage): {self.realized_today:+,.2f}",
            ]
            for key, p in self.positions.items():
                if not p.qty:
                    continue
                t = self.tradables.get(key)
                q = self.last_quotes.get(key)
                unreal = (q.last - p.avg_price) * p.qty if q else 0.0
                kind = "LÅNG" if p.qty > 0 else "BLANKAD"
                lines.append(f"{kind} {t.symbol if t else key}: {p.qty} st, snitt {p.avg_price:.2f}, orealiserat {unreal:+.2f}")
            for o in self.open_orders():
                lines.append(f"Öppen order {o.order_id}: {o.side.value} {o.volume} {o.tradable.symbol} – {self._describe(o)} [{o.state}]")
            return "\n".join(lines)
