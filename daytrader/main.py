"""Daytrading med ett råd av AI-agenter, med webbsida för inställningar och realtidsinfo.

Standard är pappershandel. Starta och öppna http://localhost:8000:

    python main.py
    python main.py --tickers "VOLV B" "ERIC B" --backend claude-code
"""

from __future__ import annotations

import argparse
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

import config
from agents import Council
from events import EventBus, JsonlLog
from fees import FeeModel
from llm import Backend, make_backend
from market import EODHDMarketData, MarketData, NordnetMarketData, TwelveDataMarketData, market_is_open
from orders import Activation, OrderRequest, OrderType, Side, Tradable, TradePlan
from paper_broker import PaperBroker
from settings import Settings, SettingsStore


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI-agenter som daytradar (papper som standard). Inställningar görs på webbsidan.")
    p.add_argument("--port", type=int, default=8000, help="Webbsidans port")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 för att nå sidan från mobilen (kräver --token)")
    p.add_argument("--token", default=config.env("DASHBOARD_TOKEN"), help="Lösenord till webbsidan")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--data-dir", type=Path, default=Path("data"), help="Var portfölj, möten och historik sparas")
    p.add_argument("--once", action="store_true", help="Ett möte per aktie och sedan avsluta (för test)")
    p.add_argument("--ignore-hours", action="store_true", help="Kör även när börsen är stängd")
    p.add_argument("--live", action="store_true", help="RIKTIGA order via Nordnet (kräver NORDNET_LIVE_CONFIRM=JA)")
    p.add_argument("--max-order-value", type=float, default=20_000, help="Spärr per order i live-läge (kr)")
    # Snabbval som också sparas i inställningarna
    p.add_argument("--tickers", nargs="+")
    p.add_argument("--backend", choices=["ollama", "claude-code", "api"])
    p.add_argument("--data-source", choices=["auto", "nordnet", "twelvedata", "eodhd"])
    p.add_argument("--rounds", type=int)
    p.add_argument("--interval", type=int)
    return p.parse_args()


def fee_model(s: Settings) -> FeeModel:
    return FeeModel(s.courtage_class, s.spread_pct, s.fx_fee_pct, s.borrow_fee_pct_year, s.account_type, s.min_edge_multiple)


def limit_price(side: Side, mid: float, offset_pct: float) -> float:
    # Positiv offset = mer aggressivt pris för att få avslut.
    factor = 1 + offset_pct / 100 if side == Side.BUY else 1 - offset_pct / 100
    return round(mid * factor, 2)


def decide(votes: dict[str, dict], s: Settings) -> TradePlan:
    tally = Counter(v["action"] for v in votes.values())
    action, count = tally.most_common(1)[0]
    if action == "HOLD" or count < s.min_agree:
        return TradePlan("HOLD", reasons=[f"ingen majoritet ({dict(tally)})"])
    agree = [v for v in votes.values() if v["action"] == action]
    order_type = Counter(v["order_type"] for v in agree).most_common(1)[0][0]

    def avg(name: str) -> float:
        return mean(max(0.0, float(v[name])) for v in agree)

    stop = min(avg("stop_loss_pct") or s.default_stop, s.max_stop)
    trailing = avg("trailing_stop_pct")
    return TradePlan(
        action=action,
        position_pct=min(avg("position_pct"), s.max_position_pct),
        order_type=OrderType(order_type),
        limit_offset_pct=max(-1.0, min(mean(float(v["limit_offset_pct"]) for v in agree), 1.0)),
        stop_loss_pct=max(0.3, stop),
        take_profit_pct=avg("take_profit_pct") or None,
        trailing_stop_pct=min(trailing, s.max_stop) if trailing >= 0.3 else None,
        reasons=[f"{len(agree)} av {len(votes)} röstade {action}"],
    )


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        args.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SettingsStore(args.data_dir / "settings.json")
        overrides = {k: v for k, v in {
            "tickers": args.tickers, "backend": args.backend, "data_source": args.data_source,
            "rounds": args.rounds, "interval": args.interval,
        }.items() if v is not None}
        if overrides or not self.store.path.exists():
            self.store.update(overrides)
        self.events = EventBus()
        self.meetings = JsonlLog(args.data_dir / "meetings.jsonl", keep=500)
        self.history = JsonlLog(args.data_dir / "history.jsonl", keep=20_000)
        self.plans: dict[str, TradePlan] = {}
        self.meeting_now = threading.Event()
        self.next_meeting = 0.0
        self.status = "startar"
        self.last_history = 0.0
        self.backend: Optional[Backend] = None
        self.backend_key: tuple = ()
        self.council: Optional[Council] = None
        self.current_tickers: list[str] = []
        s = self.store.get()

        self.nordnet = None
        source = self._resolve_source(s.data_source)
        if source == "nordnet" or args.live:
            self.nordnet = config.make_nordnet_client()
            self.nordnet.login()
        self.market = self._make_market(source, s)

        if args.live:
            if config.env("NORDNET_LIVE_CONFIRM") != "JA":
                raise SystemExit("--live kräver NORDNET_LIVE_CONFIRM=JA i .env")
            from live_broker import NordnetLiveBroker

            accid = int(config.env("NORDNET_ACCID")) if config.env("NORDNET_ACCID") else None
            self.broker = NordnetLiveBroker(self.nordnet, accid, args.max_order_value)
        else:
            self.broker = PaperBroker(args.data_dir / "portfolio.json", s.cash, fee_model(s),
                                      allow_short=s.allow_short, shortable=set(s.shortable) or None)
        self.broker.listener = lambda e: self.events.publish(e)
        self.market.add_listener(self.broker.on_quote)
        self.tradables: list[Tradable] = []
        self._sync_tickers(s)

    # ---------- Uppstart / konfiguration ----------

    def _resolve_source(self, wanted: str) -> str:
        if wanted != "auto":
            return wanted
        if config.nordnet_configured():
            return "nordnet"
        if config.env("TWELVEDATA_API_KEY"):
            return "twelvedata"
        if config.env("EODHD_API_KEY"):
            return "eodhd"
        raise SystemExit("Ingen datakälla: ange TWELVEDATA_API_KEY eller EODHD_API_KEY i .env (se README).")

    def _make_market(self, source: str, s: Settings) -> MarketData:
        if source == "nordnet":
            return NordnetMarketData(self.nordnet)
        if source == "twelvedata":
            return TwelveDataMarketData(config.env("TWELVEDATA_API_KEY"), s.spread_pct)
        if source == "eodhd":
            return EODHDMarketData(config.env("EODHD_API_KEY"), s.spread_pct)
        raise SystemExit(f"Okänd datakälla: {source}")

    def _tradable(self, spec: str, s: Settings) -> Tradable:
        symbol, _, data_symbol = spec.partition("=")
        symbol, data_symbol = symbol.strip(), (data_symbol.strip() or None)
        if self.nordnet is not None:
            return self.nordnet.tradable_from_symbol(symbol, s.market_id, data_symbol)
        return Tradable(symbol=symbol, market_id=0, identifier=data_symbol or symbol, data_symbol=data_symbol)

    def _sync_tickers(self, s: Settings) -> None:
        if s.tickers == self.current_tickers:
            return
        if self.current_tickers and isinstance(self.market, NordnetMarketData):
            self.events.publish({"typ": "status", "text": "Byte av aktier med Nordnet-data kräver omstart."})
            return
        tradables = []
        for spec in s.tickers:
            try:
                tradables.append(self._tradable(spec, s))
            except Exception as e:
                self.events.publish({"typ": "fel", "text": f"Hittade inte {spec}: {e}"})
        self.tradables = tradables
        self.current_tickers = list(s.tickers)
        self.market.start(tradables)

    def _sync_backend(self, s: Settings) -> bool:
        key = (s.backend, s.model, s.effort, s.ollama_model, s.ollama_url)
        if key == self.backend_key and self.council:
            return True
        try:
            self.backend = make_backend(s.backend, s.model, s.effort, s.ollama_model, s.ollama_url)
            self.council = Council(self.backend)
            self.backend_key = key
            self.events.publish({"typ": "status", "text": f"AI-motor: {self.backend.name}"})
            return True
        except Exception as e:
            self.set_status(f"AI-motorn fungerar inte: {e}")
            return False

    def _apply_settings(self, s: Settings) -> None:
        if isinstance(self.broker, PaperBroker):
            self.broker.fees = fee_model(s)
            self.broker.allow_short = s.allow_short
            self.broker.shortable = set(s.shortable) or None
        if hasattr(self.market, "spread_pct"):
            self.market.spread_pct = s.spread_pct
        self._sync_tickers(s)

    def set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.events.publish({"typ": "status", "text": text})

    # ---------- Handel ----------

    def close_position(self, t: Tradable, order_type: OrderType, offset_pct: float, why: str) -> None:
        pos = self.broker.position(t).qty
        quote = self.market.quote(t)
        if not pos or quote is None:
            return
        self.broker.cancel_all(t, "ersätts av stängningsorder")
        side = Side.SELL if pos > 0 else Side.BUY
        self.broker.place(OrderRequest(
            tradable=t, side=side, volume=abs(pos), price=limit_price(side, quote.mid, max(offset_pct, 0.1)),
            order_type=order_type, reference=why[:32],
        ))

    def ensure_protection(self, t: Tradable, s: Settings) -> None:
        """Lägg stop-loss / trailing / OCO så fort en position finns och saknar skydd."""
        pos = self.broker.position(t)
        if not pos.qty or self.broker.open_orders(t):
            return
        plan = self.plans.get(t.key) or TradePlan("BUY", stop_loss_pct=s.default_stop)
        long = pos.qty > 0
        side = Side.SELL if long else Side.BUY
        q = self.market.quote(t)
        entry = pos.avg_price or (q.last if q else 0)
        if not entry:
            return
        sign = -1 if long else 1  # Stoppen ligger under ingång för lång, över för blankning
        slip = 0.005
        if plan.trailing_stop_pct:
            req = OrderRequest(t, side, abs(pos.qty), activation=Activation.STOP_ACTPRICE_PERC,
                               trigger_value=round(plan.trailing_stop_pct, 2), target_value=slip * 100, reference="trailing")
        else:
            stop = round(entry * (1 + sign * plan.stop_loss_pct / 100), 2)
            stop_limit = round(stop * (1 + sign * slip), 2)
            cond = "<=" if long else ">="
            if plan.take_profit_pct:
                target = round(entry * (1 - sign * plan.take_profit_pct / 100), 2)
                req = OrderRequest(t, side, abs(pos.qty), price=target, activation=Activation.OCO_STOP_ACTPRICE,
                                   trigger_value=stop, trigger_condition=cond, target_value=stop_limit, reference="oco")
            else:
                req = OrderRequest(t, side, abs(pos.qty), price=stop_limit, activation=Activation.STOP_ACTPRICE,
                                   trigger_value=stop, trigger_condition=cond, reference="stop-loss")
        self.broker.place(req)

    def fee_check(self, plan: TradePlan, value: float, t: Tradable, s: Settings) -> Optional[str]:
        """Stoppar affärer där avgifterna äter upp den förväntade vinsten."""
        fees = fee_model(s)
        q = self.market.quote(t)
        spread = (q.ask - q.bid) / q.mid * 100 if q and q.bid and q.ask else None
        rt = fees.round_trip_pct(value, t.currency, spread)
        needed = rt * fees.min_edge_multiple
        if plan.take_profit_pct and plan.take_profit_pct < needed:
            return (f"vinstmålet {plan.take_profit_pct:.2f} % täcker inte avgifterna: kursen måste röra sig "
                    f"{rt:.2f} % bara för att gå jämnt ut, regeln kräver {needed:.2f} %")
        if not plan.take_profit_pct and plan.stop_loss_pct and plan.stop_loss_pct < rt:
            return f"stop-lossen {plan.stop_loss_pct:.2f} % är snävare än avgifterna ({rt:.2f} %)"
        return None

    def execute(self, plan: TradePlan, t: Tradable, s: Settings, may_open: bool, meeting_id: str) -> str:
        quote = self.market.quote(t)
        if quote is None or plan.action == "HOLD":
            return "ingen åtgärd"
        pos = self.broker.position(t).qty
        mid = quote.mid
        lot = max(t.lot_size, 1)

        if plan.action in ("SELL", "COVER"):
            if (plan.action == "SELL" and pos <= 0) or (plan.action == "COVER" and pos >= 0):
                return f"ingen {'lång' if plan.action == 'SELL' else 'blankad'} position att stänga"
            self.close_position(t, plan.order_type, plan.limit_offset_pct, f"rådet: {plan.action}")
            return "stängningsorder lagd"

        if not may_open:
            return "nya positioner är spärrade (paus, dagsförlust eller stängningstid)"
        side = Side.BUY if plan.action == "BUY" else Side.SELL
        if (side == Side.BUY and pos < 0) or (side == Side.SELL and pos > 0):
            self.close_position(t, OrderType.LIMIT, 0.2, "vänder position")
            return "stänger motsatt position först"
        budget = self.broker.equity() * plan.position_pct / 100
        volume = int(budget // (mid * lot)) * lot
        if volume <= 0:
            return "för liten budget för en handelspost"
        blocked = self.fee_check(plan, volume * mid, t, s)
        if blocked:
            return f"stoppad av avgiftsregeln: {blocked}"
        self.plans[t.key] = plan
        self.broker.place(OrderRequest(
            tradable=t, side=side, volume=volume, price=limit_price(side, mid, plan.limit_offset_pct),
            order_type=plan.order_type, reference=meeting_id,
        ))
        return f"order lagd: {side.value} {volume} st"

    def hold_council(self, t: Tradable, s: Settings, may_open: bool) -> None:
        meeting_id = "m" + re.sub(r"[^A-Za-z0-9]", "", f"{datetime.now():%m%d%H%M}{t.symbol}")[:30]
        self.set_status(f"Möte pågår: {t.symbol}")
        try:
            snap = self.market.snapshot(t)
        except Exception as e:
            self.events.publish({"typ": "fel", "text": f"Ingen data för {t.symbol}: {e}"})
            return
        fees = fee_model(s)
        pos = self.broker.position(t)
        budget = self.broker.equity() * s.max_position_pct / 100
        q = snap.quote
        spread = (q.ask - q.bid) / q.mid * 100 if q.bid and q.ask else None
        fees_text = fees.describe(budget, t.currency, spread)
        pf = self.broker.summary_text() + f"\nNuvarande position i {t.symbol}: {pos.qty} st"
        if not may_open:
            pf += "\nOBS: Nya positioner är spärrade – endast HOLD, SELL eller COVER är möjligt."
        self.events.publish({"typ": "möte_start", "möte": meeting_id, "papper": t.symbol, "data": snap.to_text()})

        def on_message(event: dict) -> None:
            self.events.publish({**event, "möte": meeting_id, "papper": t.symbol})

        transcript = self.council.debate(snap.to_text(), pf, s.rounds, fees_text, on_message)
        votes = self.council.vote(snap.to_text(), pf, transcript, fees_text)
        plan = decide(votes, s)
        outcome = self.execute(plan, t, s, may_open, meeting_id)
        print(f"\nBeslut för {t.symbol}: {plan.action} – {'; '.join(plan.reasons)} – {outcome}")
        record = {
            "id": meeting_id, "tid": datetime.now().isoformat(timespec="seconds"), "papper": t.symbol,
            "data": snap.to_text(), "avgifter": fees_text,
            "inlägg": [{"agent": who, "text": msg} for who, msg in transcript],
            "röster": votes, "beslut": plan.action, "skäl": plan.reasons, "utfall": outcome,
            "plan": {"position_pct": plan.position_pct, "stop_loss_pct": plan.stop_loss_pct,
                     "take_profit_pct": plan.take_profit_pct, "trailing_stop_pct": plan.trailing_stop_pct,
                     "order_type": plan.order_type.value},
        }
        self.meetings.append(record)
        self.events.publish({"typ": "beslut", "möte": meeting_id, "papper": t.symbol, "beslut": plan.action,
                             "skäl": plan.reasons, "utfall": outcome, "röster": votes})
        self.ensure_protection(t, s)

    # ---------- Data till webbsidan ----------

    def record_history(self, force: bool = False) -> None:
        if not force and time.time() - self.last_history < 60:
            return
        self.last_history = time.time()
        try:
            st = self.broker.state_dict()
        except Exception:
            return
        self.history.append({"t": datetime.now().isoformat(timespec="seconds"),
                             "eget_kapital": st["eget_kapital"], "kontanter": st.get("kontanter")})

    def state(self) -> dict:
        s = self.store.get()
        quotes = {}
        for t in self.tradables:
            q = self.market.quote(t)
            if q:
                quotes[t.symbol] = {"senast": q.last, "köp": q.bid, "sälj": q.ask}
        all_votes = [v for m in self.meetings.last(500) for v in m.get("röster", {}).values()]
        swayed = sum(1 for v in all_votes if v.get("changed_mind_because_of_devil"))
        return {
            "status": self.status,
            "pausad": s.paused,
            "live": self.args.live,
            "börsen_öppen": market_is_open(datetime.now(), s.open, s.close_at),
            "nästa_möte": datetime.fromtimestamp(self.next_meeting).strftime("%H:%M") if self.next_meeting else None,
            "datakälla": self.market.name,
            "datakälla_fel": getattr(self.market, "last_error", ""),
            "anrop_idag": getattr(self.market, "requests_today", None),
            "ai_motor": self.backend.name if self.backend else s.backend,
            "kurser": quotes,
            "djävulen": {"röster": len(all_votes), "övertalade": swayed,
                         "andel": round(swayed / len(all_votes) * 100, 1) if all_votes else None},
            "avgiftsexempel": fee_model(s).describe(self.broker.equity() * s.max_position_pct / 100),
            **self.broker.state_dict(),
        }

    # ---------- Huvudloop ----------

    def run(self) -> None:
        print("PAPPERSHANDEL – inga riktiga order skickas." if not self.args.live else "!!! LIVE-HANDEL !!!")
        print(f"Kursdata: {self.market.name}")
        while True:
            s = self.store.get()
            self._apply_settings(s)
            self.market.poll()
            self.broker.new_day_check()
            self.record_history()
            now = datetime.now()
            open_now = self.args.ignore_hours or market_is_open(now, s.open, s.close_at)
            closing = not self.args.ignore_hours and now.strftime("%H:%M") >= s.close_at
            if not open_now:
                self.meeting_now.clear()  # "Möte nu" när börsen är stängd ignoreras

            for t in self.tradables:
                if closing and self.broker.position(t).qty:
                    self.close_position(t, OrderType.LIMIT, 0.3, "dagens slut")
                elif self.broker.position(t).qty:
                    self.ensure_protection(t, s)

            # Pausad: inga schemalagda möten (sparar AI-anrop), men "Möte nu" fungerar.
            due = (time.time() >= self.next_meeting and not s.paused) or self.meeting_now.is_set()
            if open_now and due and not self._sync_backend(s):
                self.meeting_now.clear()
                self.next_meeting = time.time() + 60  # Försök igen om en minut
                due = False
                if self.args.once:
                    return
            if open_now and due:
                self.meeting_now.clear()
                day_change = (self.broker.equity() / self.broker.start_of_day_equity - 1) * 100
                may_open = day_change > -s.max_daily_loss and not closing and not s.paused
                for t in self.tradables:
                    try:
                        self.hold_council(t, s, may_open)
                    except Exception as e:
                        self.events.publish({"typ": "fel", "text": f"Mötet om {t.symbol} misslyckades: {e}"})
                        print(f"Mötet om {t.symbol} misslyckades: {e}")
                self.broker.save()
                self.record_history(force=True)
                self.set_status("Pausad – inga nya affärer" if s.paused else "Väntar på nästa möte")
                if self.args.once:
                    return
                self.next_meeting = time.time() + s.interval * 60
            elif s.paused:
                self.set_status("Pausad – inga nya affärer")
            elif not open_now:
                self.meeting_now.clear()
                if self.args.once:
                    print("Börsen är stängd. Använd --ignore-hours för att testa ändå.")
                    return
                self.set_status("Börsen är stängd")
            self.broker.save()
            # Vänta, men vakna direkt om någon trycker "Möte nu" på webbsidan.
            self.meeting_now.wait(s.poll if open_now else 60)


def main() -> None:
    config.load_env()
    args = parse_args()
    app = App(args)
    if not args.no_dashboard:
        from dashboard.server import start_server

        url = start_server(app, args.host, args.port, args.token)
        print(f"Webbsidan: {url}")
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nAvslutar...")
    finally:
        app.broker.save()
        if app.nordnet:
            app.nordnet.logout()


if __name__ == "__main__":
    main()
