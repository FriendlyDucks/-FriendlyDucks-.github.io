# Claude-daytrader för Nordnet

Fem Claude-agenter diskuterar varje aktie och fattar beslut som skickas som
Nordnet-order. Standard är **pappershandel med riktiga kurser från Nordnet**.

| Agent | Roll |
|---|---|
| **Tekla** | Teknisk analytiker (RSI, glidande medelvärden, VWAP, orderbok) |
| **Moa** | Momentum- och trendstrateg (uppåt och nedåt) |
| **Nils** | Nyhets- och sentimentanalytiker |
| **Rut** | Riskhanterare (storlek, stop-loss, trailing, vinstmål) |
| **Djävulens advokat** | Alltid negativ – säger emot och försöker övertala de andra |

## Steg 1: Claude (Anthropic API)

1. Skapa konto på <https://platform.claude.com>.
2. Lägg in betalkort och köp krediter under **Billing** (API:t är förbetalt och
   separat från en Claude Pro/Max-prenumeration).
3. Skapa en nyckel under **API Keys** och lägg den i `.env` som `ANTHROPIC_API_KEY`.

## Steg 2: Nordnet API

Nordnet har ett officiellt API för privatkunder ([dokumentation](https://www.nordnet.se/externalapi/docs/getting_started)).
Det används här i stället för att avlyssna webbplatsens interna anrop. Det är tillåtet,
stabilt och ger realtidskurser.

1. **Ansök om API-åtkomst** hos Nordnet Trading Support (tradingsupport@nordnet.se,
   010-583 31 99). Enligt Nordnets dokumentation tar de för närvarande inte alltid in nya
   API-kunder, så fråga om status. Det kan också tillkomma en avgift. API:t kopplas till
   det konto du anger, och det kontot måste vara förvalt i din profil.
2. Skapa en nyckel:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/nordnet_ed25519 -N ""
   ```
3. Ladda upp `~/.ssh/nordnet_ed25519.pub` på nordnet.se: **Mina sidor → Inställningar →
   Min profil → Säkerhet**. Kopiera API-nyckeln (UUID) som visas.
4. Fyll i `NORDNET_API_KEY` och `NORDNET_PRIVATE_KEY` i `.env`.

Inloggningen sker sedan automatiskt med nyckeln, utan BankID varje gång. Sessionen
förnyas av programmet.

**Utan Nordnet-API** fungerar allt ändå, men med fördröjda kurser från Yahoo Finance
(ange då Yahoo-symbol: `"VOLV B=VOLV-B.ST"`).

## Steg 3: Kör

```bash
pip install -r requirements.txt
cp .env.example .env        # fyll i nycklarna
python check_setup.py       # testar Claude och Nordnet

python main.py --tickers "VOLV B=VOLV-B.ST" "ERIC B=ERIC-B.ST"
python main.py --tickers "VOLV B" --interval 0 --ignore-hours   # ett möte, för test
```

## Så fungerar det

1. Kurser strömmar in (Nordnets realtidsflöde eller Yahoo). Pappersmäklaren
   kontrollerar order mot riktiga köp- och säljkurser hela tiden.
2. Var `--interval` minut håller rådet möte för varje aktie: analytikerna talar, djävulens
   advokat angriper, i `--rounds` rundor.
3. Analytikerna röstar: **BUY, SELL, SHORT, COVER eller HOLD**, med ordertyp,
   limitpris, stop-loss, vinstmål och trailing stop. Minst `--min-agree` (3 av 4)
   måste vara överens.
4. När en position finns läggs skydd direkt som Nordnet-order:
   - **OCO** (vinstmål + stop-loss) om rådet satt ett vinstmål
   - **Trailing stop** (`STOP_ACTPRICE_PERC`) om rådet valt det
   - Annars en vanlig **stop-loss** (`STOP_ACTPRICE`)
5. Hårda regler i koden: max positionsstorlek, högsta stop-loss, maxförlust per dag och
   att allt stängs vid `--close-at`.

### Nordnet-funktioner som stöds

| Nordnet | Pappershandel | Live |
|---|---|---|
| Ordertyper LIMIT, NORMAL, FAK, FOK | ✅ simuleras mot bid/ask och volym | ✅ |
| Stop-loss (`STOP_ACTPRICE`) | ✅ | ✅ |
| Trailing stop (`STOP_ACTPRICE_PERC`) | ✅ | ✅ |
| OCO, vinstmål + stop (`OCO_STOP_ACTPRICE`) | ✅ | ✅ |
| Vilande order (`MANUAL`) | ✅ | ✅ |
| Blankning | ✅ med säkerhetskrav (`--shortable` begränsar) | ✅ om kontot har blankningsavtal |
| Courtage | ✅ ställ in din klass med `--courtage-pct` och `--courtage-min` | Nordnet drar |
| Dagsorder upphör | ✅ | ✅ |

## Live-handel (senare)

```bash
# i .env: NORDNET_LIVE_CONFIRM=JA
python main.py --tickers "VOLV B" --live --max-order-value 5000
```

Två spärrar krävs: flaggan `--live` och `NORDNET_LIVE_CONFIRM=JA`. `--max-order-value`
stoppar för stora order. Kontrollera innan du går live:

- **Tickstorlek:** limitpriser avrundas till 2 decimaler, men Nordnet kräver att priset
  följer papperets tickstorlek, annars avvisas ordern.
- **Trailing stop:** att `target_value` är en procentavvikelse för limitpriset är ett
  antagande. Kontrollera det mot Nordnets dokumentation.
- **Blankning:** att ditt konto (vanligen en aktie- och fonddepå, inte ISK/KF) och
  aktien tillåter blankning.
- **Prova först:** kör en vecka på papper med Nordnet-data.

> ⚠️ Daytrading innebär stor risk och de flesta förlorar pengar. Programmet är ett
> experiment, inte finansiell rådgivning.

## Kostnad för Claude

Ett möte kostar `5 × rundor + 4` API-anrop per aktie (14 med standardinställningar).
Med Claude Opus 5.5 ($4 per miljon tokens in, $20 per miljon ut) kostar det ungefär
3–5 kr per möte och aktie. Mötestiden varierar. Ett möte var 15:e minut under en
handelsdag blir ungefär 33 möten, alltså cirka 100–170 kr per aktie och dag. Sänk
kostnaden med `--interval 30`, `--rounds 1` eller `CLAUDE_MODEL=claude-sonnet-5-5`
(halva priset).
