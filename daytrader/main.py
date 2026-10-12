"""Daytrading med ett råd av Claude-agenter mot Nordnet.

Standard är pappershandel. Med Nordnet-API konfigurerat används riktiga
realtidskurser från Nordnet, annars fördröjda kurser från Yahoo Finance.

Exempel:
    python main.py --tickers "VOLV B=VOLV-B.ST" "ERIC B=ERIC-B.ST"
    python main.py --tickers "VOLV B" --interval 15 --close-at 17:20
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

import config
from agents import MODEL, Council
from llm import DEFAULT_OLLAMA_MODEL, make_backend
from market import MarketData, NordnetMarketData, YahooMarketData, market_is_open
from orders import Activation, OrderRequest, OrderType, TradePlan, Side, Tradable
from paper_broker import CostModel, PaperBroker


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Claude-agenter som daytradar via Nordnet (papper som standard).")
    p.add_argument("--tickers", nargs="+", default=["VOLV B=VOLV-B.ST"],
                   help='Nordnet-symbol, ev. med Yahoo-symbol: "VOLV B=VOLV-B.ST"')
    p.add_argument("--market-id", type=int, default=11, help="Nordnets marknads-id (11 = Stockholmsbörsen)")
    p.add_argument("--source", choices=["auto", "nordnet", "yahoo"], default="auto")
    p.add_argument("--rounds", type=int, default=2, help="Diskussionsrundor per beslut")
    p.add_argument("--interval", type=int, default=15, help="Minuter mellan rådets möten (0 = ett möte och sedan avsluta)")
    p.add_argument("--poll", type=int, default=20, help="Sekunder mellan kurs-/orderkontroller")
    p.add_argument("--min-agree", type=int, default=3, help="Antal av 4 analytiker som måste vara överens")
    p.add_argument("--max-position-pct", type=float, default=20.0)
    p.add_argument("--default-stop", type=float, default=1.5, help="Stop-loss i %% om agenterna inte anger något")
    p.add_argument("--max-stop", type=float, default=5.0, help="Högsta tillåtna stop-loss i %%")
    p.add_argument("--max-daily-loss", type=float, default=3.0, help="Inga nya affärer efter så stor dagsförlust (%%)")
    p.add_argument("--open", default="09:00", help="Börsen öppnar (lokal tid)")
    p.add_argument("--close-at", default="17:20", help="Stäng alla positioner efter denna tid")
    p.add_argument("--ignore-hours", action="store_true", help="Kör även när börsen är stängd (för test)")
    p.add_argument("--no-short", action="store_true", help="Stäng av blankning")
    p.add_argument("--shortable", nargs="*", default=None, help="Endast dessa symboler får blankas")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--courtage-pct", type=float, default=0.25, help="Courtage i %% (ställ in din courtageklass)")
    p.add_argument("--courtage-min", type=float, default=1.0, help="Minsta courtage i kr")
    p.add_argument("--portfolio", type=Path, default=Path("portfolio.json"))
    p.add_argument("--backend", choices=["ollama", "claude-code", "api"], default=config.env("LLM_BACKEND", "ollama"),
                   help="ollama = lokal gratismodell, claude-code = din Claude-prenumeration, api = API-krediter")
    p.add_argument("--model", default=config.env("CLAUDE_MODEL", MODEL), help="Claude-modell (claude-code/api)")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"], help="Endast api")
    p.add_argument("--ollama-model", default=config.env("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    p.add_argument("--ollama-url", default=config.env("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument("--live", action="store_true", help="RIKTIGA order via Nordnet (kräver NORDNET_LIVE_CONFIRM=JA)")
    p.add_argument("--max-order-value", type=float, default=20_000, help="Spärr per order i live-läge (kr)")
    return p.parse_args()


# ---------- Uppstart ----------

def build_tradables(args: argparse.Namespace, nordnet) -> list[Tradable]:
    result = []
    for spec in args.tickers:
        symbol, _, yahoo = spec.partition("=")
        symbol, yahoo = symbol.strip(), (yahoo.strip() or None)
        if nordnet is not None:
            result.append(nordnet.tradable_from_symbol(symbol, args.market_id, yahoo))
        else:
            result.append(Tradable(symbol=symbol, market_id=0, identifier=yahoo or symbol, yahoo=yahoo or symbol))
    return result


# ---------- Beslut ----------

def decide(votes: dict[str, dict], args: argparse.Namespace) -> TradePlan:
    tally = Counter(v["action"] for v in votes.values())
    action, count = tally.most_common(1)[0]
    if action == "HOLD" or count < args.min_agree:
        return TradePlan("HOLD", reasons=[f"ingen majoritet ({dict(tally)})"])
    agree = [v for v in votes.values() if v["action"] == action]
    order_type = Counter(v["order_type"] for v in agree).most_common(1)[0][0]

    def avg(field: str) -> float:
        return mean(max(0.0, float(v[field])) for v in agree)

    stop = min(avg("stop_loss_pct") or args.default_stop, args.max_stop)
    trailing = avg("trailing_stop_pct")
    return TradePlan(
        action=action,
        position_pct=min(avg("position_pct"), args.max_position_pct),
        order_type=OrderType(order_type),
        limit_offset_pct=max(-1.0, min(mean(float(v["limit_offset_pct"]) for v in agree), 1.0)),
        stop_loss_pct=max(0.3, stop),
        take_profit_pct=avg("take_profit_pct") or None,
        trailing_stop_pct=min(trailing, args.max_stop) if trailing >= 0.3 else None,
        reasons=[f"{len(agree)} av {len(votes)} röstade {action}"],
    )


def limit_price(side: Side, mid: float, offset_pct: float) -> float:
    # Positiv offset = mer aggressivt pris för att få avslut.
    factor = 1 + offset_pct / 100 if side == Side.BUY else 1 - offset_pct / 100
    return round(mid * factor, 2)


def execute(plan: TradePlan, t: Tradable, broker, market: MarketData, args, plans: dict[str, TradePlan], may_open: bool) -> None:
    quote = market.quote(t)
    if quote is None or plan.action == "HOLD":
        return
    pos = broker.position(t).qty
    mid = quote.mid
    lot = max(t.lot_size, 1)

    if plan.action in ("SELL", "COVER"):
        if (plan.action == "SELL" and pos <= 0) or (plan.action == "COVER" and pos >= 0):
            print(f"  Ingen {'lång' if plan.action == 'SELL' else 'blankad'} position att stänga.")
            return
        close_position(t, broker, market, plan.order_type, plan.limit_offset_pct, f"rådet: {plan.action}")
        return

    if not may_open:
        print("  Nya positioner är spärrade just nu (dagsförlust eller stängningstid).")
        return
    side = Side.BUY if plan.action == "BUY" else Side.SELL
    if (side == Side.BUY and pos < 0) or (side == Side.SELL and pos > 0):
        print("  Rådet vill vända positionen – stänger först, nästa möte får besluta om ny.")
        close_position(t, broker, market, OrderType.LIMIT, 0.2, "vänder position")
        return
    budget = broker.equity() * plan.position_pct / 100
    volume = int(budget // (mid * lot)) * lot
    if volume <= 0:
        print("  För liten budget för en handelspost.")
        return
    plans[t.key] = plan
    broker.place(OrderRequest(
        tradable=t, side=side, volume=volume, price=limit_price(side, mid, plan.limit_offset_pct),
        order_type=plan.order_type, reference=f"rad-{plan.action.lower()}",
    ))


def close_position(t: Tradable, broker, market: MarketData, order_type: OrderType, offset_pct: float, why: str) -> None:
    pos = broker.position(t).qty
    quote = market.quote(t)
    if not pos or quote is None:
        return
    broker.cancel_all(t, "ersätts av stängningsorder")
    side = Side.SELL if pos > 0 else Side.BUY
    broker.place(OrderRequest(
        tradable=t, side=side, volume=abs(pos), price=limit_price(side, quote.mid, max(offset_pct, 0.1)),
        order_type=order_type, reference=why[:32],
    ))


def ensure_protection(t: Tradable, broker, market: MarketData, plans: dict[str, TradePlan], args) -> None:
    """Lägg stop-loss / trailing / OCO så fort en position finns och saknar skydd."""
    pos = broker.position(t)
    if not pos.qty or broker.open_orders(t):
        return
    plan = plans.get(t.key) or TradePlan("BUY", stop_loss_pct=args.default_stop)
    long = pos.qty > 0
    side = Side.SELL if long else Side.BUY
    entry = pos.avg_price or market.quote(t).last
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
    broker.place(req)


# ---------- Huvudloop ----------

def hold_council(t: Tradable, council: Council, broker, market: MarketData, args, plans, may_open: bool) -> None:
    print(f"\n{'=' * 70}\nRådet diskuterar {t.symbol} ({datetime.now():%H:%M})\n{'=' * 70}")
    try:
        snap = market.snapshot(t)
    except Exception as e:
        print(f"Ingen data för {t.symbol}: {e}")
        return
    pos = broker.position(t)
    pf = broker.summary_text() + f"\nNuvarande position i {t.symbol}: {pos.qty} st"
    if not may_open:
        pf += "\nOBS: Nya positioner är spärrade – endast HOLD, SELL eller COVER är möjligt."
    transcript = council.debate(snap.to_text(), pf, args.rounds)
    votes = council.vote(snap.to_text(), pf, transcript)

    print("\n--- Röster ---")
    for name, v in votes.items():
        swayed = " (övertalad av djävulens advokat)" if v["changed_mind_because_of_devil"] else ""
        print(f"{name}: {v['action']} {v['order_type']} ({v['confidence']} %), {v['position_pct']:.0f} % kapital, "
              f"stop {v['stop_loss_pct']} %, mål {v['take_profit_pct']} %, trailing {v['trailing_stop_pct']} %{swayed}")
        print(f"   {v['reasoning']}")
    plan = decide(votes, args)
    print(f"\nBeslut: {plan.action} – {'; '.join(plan.reasons)}")
    execute(plan, t, broker, market, args, plans, may_open)


def main() -> None:
    config.load_env()
    args = parse_args()

    nordnet = None
    use_nordnet = args.source == "nordnet" or (args.source == "auto" and config.nordnet_configured())
    if use_nordnet or args.live:
        nordnet = config.make_nordnet_client()
        nordnet.login()
        print("Inloggad hos Nordnet.")

    tradables = build_tradables(args, nordnet)
    market: MarketData = NordnetMarketData(nordnet) if nordnet else YahooMarketData()

    if args.live:
        if config.env("NORDNET_LIVE_CONFIRM") != "JA":
            raise SystemExit("--live kräver NORDNET_LIVE_CONFIRM=JA i .env")
        from live_broker import NordnetLiveBroker

        accid = int(config.env("NORDNET_ACCID")) if config.env("NORDNET_ACCID") else None
        broker = NordnetLiveBroker(nordnet, accid, args.max_order_value)
        print(f"!!! LIVE-HANDEL på Nordnet-konto {broker.accid} – riktiga pengar !!!")
    else:
        costs = CostModel(courtage_pct=args.courtage_pct / 100, courtage_min=args.courtage_min)
        shortable = set(args.shortable) if args.shortable else None
        broker = PaperBroker(args.portfolio, args.cash, costs, allow_short=not args.no_short, shortable=shortable)
        print("PAPPERSHANDEL – inga riktiga order skickas.")
    backend = make_backend(args.backend, args.model, args.effort, args.ollama_model, args.ollama_url)
    print(f"Kursdata: {market.name}. AI-motor: {backend.name}.")

    market.add_listener(broker.on_quote)
    market.start(tradables)
    council = Council(backend)
    plans: dict[str, TradePlan] = {}
    next_meeting = 0.0

    try:
        while True:
            market.poll()
            broker.new_day_check()
            now = datetime.now()
            open_now = args.ignore_hours or market_is_open(now, args.open, args.close_at)
            closing = not args.ignore_hours and now.strftime("%H:%M") >= args.close_at

            for t in tradables:
                if closing and broker.position(t).qty:
                    close_position(t, broker, market, OrderType.LIMIT, 0.3, "dagens slut")
                elif broker.position(t).qty:
                    ensure_protection(t, broker, market, plans, args)

            if open_now and time.time() >= next_meeting:
                day_change = (broker.equity() / broker.start_of_day_equity - 1) * 100
                may_open = day_change > -args.max_daily_loss and not closing
                if not may_open:
                    print(f"Dagsresultat {day_change:+.2f} % – nya positioner spärrade.")
                for t in tradables:
                    hold_council(t, council, broker, market, args, plans, may_open)
                    ensure_protection(t, broker, market, plans, args)
                print(f"\n{broker.summary_text()}")
                broker.save()
                if args.interval <= 0:
                    break
                next_meeting = time.time() + args.interval * 60
            elif not open_now and args.interval > 0:
                print(f"{now:%H:%M} Börsen är stängd, väntar...")
                time.sleep(300)
                continue
            elif not open_now:
                print("Börsen är stängd. Använd --ignore-hours för att testa ändå.")
                break
            broker.save()
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nAvslutar...")
    finally:
        broker.save()
        if nordnet:
            nordnet.logout()


if __name__ == "__main__":
    main()
