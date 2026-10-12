"""Daytrading med ett råd av Claude-agenter (endast pappershandel).

Exempel:
    python main.py --tickers AAPL NVDA --rounds 2
    python main.py --tickers VOLV-B.ST --interval 15 --close-at 17:20
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import anthropic

from agents import MODEL, Council
from broker import PaperBroker
from market import get_snapshot


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Claude-agenter som daytradar på papper.")
    p.add_argument("--tickers", nargs="+", default=["AAPL"], help="Aktier att bevaka (Yahoo-symboler)")
    p.add_argument("--rounds", type=int, default=2, help="Antal diskussionsrundor per beslut")
    p.add_argument("--interval", type=int, default=0, help="Minuter mellan körningar (0 = kör en gång)")
    p.add_argument("--min-agree", type=int, default=3, help="Hur många av 4 analytiker som måste vara överens")
    p.add_argument("--max-position-pct", type=float, default=20.0, help="Max andel av kapitalet per affär")
    p.add_argument("--stop", type=float, default=0.02, help="Stop-loss, t.ex. 0.02 = 2 %%")
    p.add_argument("--take", type=float, default=0.04, help="Take-profit, t.ex. 0.04 = 4 %%")
    p.add_argument("--max-daily-loss", type=float, default=0.03, help="Sluta handla om dagen tappat så mycket")
    p.add_argument("--close-at", default=None, help="Stäng alla positioner efter denna tid (HH:MM)")
    p.add_argument("--cash", type=float, default=100_000.0, help="Startkapital för pappersportföljen")
    p.add_argument("--portfolio", type=Path, default=Path("portfolio.json"))
    p.add_argument("--model", default=MODEL)
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    return p.parse_args()


def portfolio_text(broker: PaperBroker, prices: dict[str, float]) -> str:
    lines = [f"Kontanter: {broker.cash:.2f}", f"Totalt värde: {broker.equity(prices):.2f}"]
    for t, pos in broker.positions.items():
        lines.append(
            f"Innehav {t}: {pos.shares} st, inköp {pos.entry_price:.2f}, "
            f"stop {pos.stop_loss:.2f}, mål {pos.take_profit:.2f}"
        )
    return "\n".join(lines)


def decide(votes: dict[str, dict], min_agree: int, max_pct: float) -> tuple[str, float]:
    tally = Counter(v["action"] for v in votes.values())
    action, count = tally.most_common(1)[0]
    if action == "HOLD" or count < min_agree:
        return "HOLD", 0.0
    agreeing = [v for v in votes.values() if v["action"] == action]
    pct = sum(v["position_pct"] for v in agreeing) / len(agreeing)
    return action, max(0.0, min(pct, max_pct))


def past_close_time(close_at: str | None) -> bool:
    if not close_at:
        return False
    hh, mm = map(int, close_at.split(":"))
    now = datetime.now()
    return (now.hour, now.minute) >= (hh, mm)


def run_once(args: argparse.Namespace, council: Council, broker: PaperBroker) -> None:
    snapshots = {}
    for t in args.tickers:
        try:
            snapshots[t] = get_snapshot(t)
        except Exception as e:
            print(f"Kunde inte hämta {t}: {e}")
    prices = {t: s.price for t, s in snapshots.items()}
    broker.new_day_check(prices)

    # Automatiska regler går före agenternas diskussion.
    for t, pos in list(broker.positions.items()):
        price = prices.get(t)
        if price is None:
            continue
        if price <= pos.stop_loss:
            broker.sell_all(t, price, "stop-loss utlöst")
        elif price >= pos.take_profit:
            broker.sell_all(t, price, "take-profit nådd")

    if past_close_time(args.close_at):
        for t in list(broker.positions):
            if t in prices:
                broker.sell_all(t, prices[t], "dagens slut – stänger position")
        print("Efter stängningstid, ingen ny handel.")
        return

    day_pnl = broker.equity(prices) / broker.start_of_day_equity - 1
    if day_pnl <= -args.max_daily_loss:
        print(f"Dagens förlust {day_pnl:.2%} har nått gränsen – ingen ny handel idag.")
        return

    for t, snap in snapshots.items():
        print(f"\n{'=' * 70}\nRådet diskuterar {t}\n{'=' * 70}")
        snap_text = snap.to_text()
        pf_text = portfolio_text(broker, prices)
        transcript = council.debate(snap_text, pf_text, args.rounds)
        votes = council.vote(snap_text, pf_text, transcript)

        print("\n--- Röster ---")
        for name, v in votes.items():
            swayed = " (övertalad av djävulens advokat)" if v["changed_mind_because_of_devil"] else ""
            print(f"{name}: {v['action']} ({v['confidence']} %), {v['position_pct']:.0f} % av kapitalet{swayed}")
            print(f"   {v['reasoning']}")

        action, pct = decide(votes, args.min_agree, args.max_position_pct)
        print(f"\nBeslut: {action}" + (f" med {pct:.1f} % av kapitalet" if action == "BUY" else ""))

        if action == "BUY":
            budget = broker.equity(prices) * pct / 100
            shares = int(min(budget, broker.cash) // snap.price)
            broker.buy(t, shares, snap.price, args.stop, args.take, f"rådet röstade köp ({pct:.1f} %)")
        elif action == "SELL" and t in broker.positions:
            broker.sell_all(t, snap.price, "rådet röstade sälj")

    print(f"\n{portfolio_text(broker, prices)}")
    broker.save()


def main() -> None:
    args = parse_args()
    print("OBS: Detta är PAPPERSHANDEL med simulerade pengar. Inga riktiga order skickas.\n")
    council = Council(anthropic.Anthropic(), model=args.model, effort=args.effort)
    broker = PaperBroker.load(args.portfolio, args.cash)

    while True:
        run_once(args, council, broker)
        if args.interval <= 0:
            break
        print(f"\nVäntar {args.interval} min till nästa körning (Ctrl+C för att avsluta)...")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
