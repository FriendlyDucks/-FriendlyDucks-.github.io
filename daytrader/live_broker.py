"""Riktig handel via Nordnets API. Samma gränssnitt som PaperBroker.

Används bara med flaggan --live OCH miljövariabeln NORDNET_LIVE_CONFIRM=JA.
Stop-loss, trailing stop och OCO läggs som riktiga Nordnet-order, så de
skyddar positionen även om programmet kraschar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from nordnet.client import NordnetClient
from orders import OrderRequest, Quote, Tradable
from paper_broker import PaperPosition


def _num(value: Any) -> float:
    """Nordnet returnerar ibland belopp som {"value": x, "currency": "SEK"}."""
    if isinstance(value, dict):
        value = value.get("value", value.get("price", 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class LiveOrder:
    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.order_id = str(raw.get("order_id"))
        self.state = raw.get("order_state", "")
        self.side = raw.get("side")
        self.volume = raw.get("volume")
        t = raw.get("tradable", {})
        self.key = f"{t.get('market_id')}:{t.get('identifier')}"


class NordnetLiveBroker:
    name = "Nordnet LIVE"

    def __init__(self, client: NordnetClient, accid: Optional[int] = None, max_order_value: float = 20_000) -> None:
        self.client = client
        self.max_order_value = max_order_value
        if accid is None:
            accounts = client.accounts()
            default = next((a for a in accounts if a.get("default") or a.get("is_default")), accounts[0])
            accid = int(default["accid"])
        self.accid = accid
        self.last_quotes: dict[str, Quote] = {}
        self.day = ""
        self.start_of_day_equity = self.equity()
        self.realized_today = 0.0

    def on_quote(self, tradable: Tradable, quote: Quote) -> None:
        self.last_quotes[tradable.key] = quote

    def new_day_check(self) -> None:
        today = datetime.now().date().isoformat()
        if self.day != today:
            self.day = today
            self.start_of_day_equity = self.equity()

    def equity(self) -> float:
        info = self.client.account_info(self.accid)[0]
        return _num(info.get("own_capital")) or _num(info.get("account_sum"))

    def position(self, tradable: Tradable) -> PaperPosition:
        for p in self.client.positions(self.accid):
            inst = p.get("instrument", {})
            if f"{p.get('market_id', inst.get('market_id'))}:{inst.get('identifier', p.get('identifier'))}" == tradable.key \
                    or inst.get("symbol") == tradable.symbol:
                return PaperPosition(qty=int(_num(p.get("qty"))), avg_price=_num(p.get("acq_price")))
        return PaperPosition()

    def open_orders(self, tradable: Optional[Tradable] = None) -> list[LiveOrder]:
        orders = [LiveOrder(o) for o in self.client.orders(self.accid)]
        active = [o for o in orders if o.state in ("ON_MARKET", "LOCAL", "WAITING")]
        return [o for o in active if tradable is None or o.key == tradable.key]

    def place(self, req: OrderRequest):
        q = self.last_quotes.get(req.tradable.key)
        ref = req.price or (q.last if q else 0)
        if ref * req.volume > self.max_order_value:
            print(f"  [LIVE] STOPPAD: ordervärde {ref * req.volume:,.0f} över gränsen {self.max_order_value:,.0f}")
            return None
        reply = self.client.place_order(self.accid, req)
        print(f"  [LIVE] {req.side.value} {req.volume} {req.tradable.symbol} -> {reply}")
        return reply

    def cancel(self, order_id: str, why: str = "") -> None:
        self.client.cancel_order(self.accid, int(order_id))

    def cancel_all(self, tradable: Optional[Tradable] = None, why: str = "") -> None:
        for o in self.open_orders(tradable):
            self.cancel(o.order_id, why)

    def save(self) -> None:
        pass  # Nordnet är källan till sanningen

    def summary_text(self) -> str:
        lines = [f"Konto {self.accid}, eget kapital: {self.equity():,.2f}"]
        for p in self.client.positions(self.accid):
            inst = p.get("instrument", {})
            lines.append(f"Innehav {inst.get('symbol', inst)}: {_num(p.get('qty')):.0f} st, anskaffning {_num(p.get('acq_price')):.2f}")
        for o in self.open_orders():
            lines.append(f"Öppen order {o.order_id}: {o.side} {o.volume} ({o.key}) [{o.state}]")
        return "\n".join(lines)
