# Claude-daytrader – ett råd av AI-agenter

Fem Claude-agenter diskuterar varje aktie innan något händer:

| Agent | Roll |
|---|---|
| **Tekla** | Teknisk analytiker (RSI, glidande medelvärden, VWAP, volym) |
| **Moa** | Momentum- och trendstrateg |
| **Nils** | Nyhets- och sentimentanalytiker |
| **Rut** | Riskhanterare (positionsstorlek, stop-loss) |
| **Djävulens advokat** | Alltid negativ – säger emot och försöker övertala de andra |

## Så fungerar det

1. Kursdata (5-minutersstaplar) och rubriker hämtas från Yahoo Finance.
2. Agenterna diskuterar i `--rounds` rundor. I varje runda talar de fyra analytikerna,
   sedan går djävulens advokat till angrepp mot den åsikt som leder just då.
3. De fyra analytikerna röstar (BUY / SELL / HOLD). Djävulens advokat röstar inte –
   den kan bara påverka genom att övertala. Rösterna visar också om någon blev övertalad.
4. Minst `--min-agree` (standard 3 av 4) måste vara överens, annars blir det HOLD.
5. Stop-loss, take-profit, maxförlust per dag och stängning vid dagens slut sköts i
   koden och kan inte köras över av agenterna.

> ⚠️ **Programmet handlar bara på papper.** Inga riktiga order skickas, och resultatet
> sparas i `portfolio.json`. Det här är ett experiment, inte finansiell rådgivning.

## Kom igång

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # eller kör `ant auth login`

# En körning för en aktie
python main.py --tickers AAPL

# Svenska aktier var 15:e minut, stäng allt 17:20
python main.py --tickers VOLV-B.ST ERIC-B.ST --interval 15 --close-at 17:20
```

## Inställningar

| Flagga | Standard | Beskrivning |
|---|---|---|
| `--tickers` | `AAPL` | Yahoo-symboler (svenska aktier slutar på `.ST`) |
| `--rounds` | `2` | Diskussionsrundor per beslut |
| `--interval` | `0` | Minuter mellan körningar (0 = kör en gång) |
| `--min-agree` | `3` | Antal analytiker som måste vara överens |
| `--max-position-pct` | `20` | Max andel av kapitalet per affär |
| `--stop` / `--take` | `0.02` / `0.04` | Stop-loss och take-profit |
| `--max-daily-loss` | `0.03` | Ingen ny handel efter så stor dagsförlust |
| `--close-at` | – | Stäng alla positioner efter denna tid |
| `--cash` | `100000` | Startkapital |
| `--model` | `claude-opus-5-5` | Claude-modell |
| `--effort` | `low` | Hur mycket modellen tänker (`low` … `max`) |

## Kostnad

Varje beslut kräver `5 × rundor + 4` API-anrop per aktie (14 anrop med standardinställningar).
Med många aktier och kort intervall blir det många anrop – börja med en aktie och
`--interval 0`.
