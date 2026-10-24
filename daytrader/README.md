# Handelsrådet – AI-agenter som daytradar

Fem AI-agenter diskuterar varje aktie och fattar beslut som blir order. Standard är
**pappershandel**: riktiga kurser, låtsaspengar. Allt följs och styrs från en webbsida.

| Agent | Roll |
|---|---|
| **Tekla** | Teknisk analytiker (RSI, glidande medelvärden, VWAP, orderbok) |
| **Moa** | Momentum- och trendstrateg (uppåt och nedåt) |
| **Nils** | Nyhets- och sentimentanalytiker |
| **Rut** | Riskhanterare (storlek, stop-loss, vinstmål och avgifter) |
| **Djävulens advokat** | Alltid negativ – säger emot och försöker övertala de andra |

## Kom igång

```bash
cd daytrader
pip install -r requirements.txt
cp .env.example .env          # fyll i nycklar, se nedan
python check_setup.py         # testar AI-motor och datakälla
python main.py                # öppna sedan http://localhost:8000
```

## 1. Kursdata

| Källa | Stockholmsbörsen | Kostnad | Nyckel i `.env` |
|---|---|---|---|
| [Twelve Data](https://twelvedata.com) | Realtid på Pro-planen. Gratisnyckeln täcker bara testaktien INVE B | Gratis för test, Pro för riktig användning | `TWELVEDATA_API_KEY` |
| [EODHD](https://eodhd.com) | ~15 min fördröjning, plus nyhetsrubriker | Från ca 20 USD/mån ("All World Extended") | `EODHD_API_KEY` |
| Nordnet API | Realtid med orderbok | Tar inte in nya API-kunder just nu | `NORDNET_API_KEY` |

Med `data_source = auto` väljs Nordnet om det finns, annars Twelve Data, annars EODHD.
För att testa gratis: skaffa en gratisnyckel hos Twelve Data och bevaka `INVE B`.

Skriv aktier som på Nordnet (`VOLV B`). Programmet gör om namnet till källans format
(`VOLV.B` hos Twelve Data, `VOLV-B.ST` hos EODHD). Vill du ange symbolen själv skriver
du `VOLV B=VOLV-B.ST`.

## 2. AI-motor

| Motor | Kostnad | Så kommer du igång |
|---|---|---|
| `ollama` (standard) | Gratis, körs på din dator | Installera [Ollama](https://ollama.com), kör `ollama pull qwen2.5:14b` (eller `qwen2.5:7b` på svagare dator) |
| `claude-code` | Ingår i Claude Pro/Max | Installera Claude Code, kör `claude` en gång och logga in |
| `api` | API-krediter (~0,10 kr per möte med Haiku 5.5, ~5 kr med Opus 5.5) | Nyckel från platform.claude.com i `ANTHROPIC_API_KEY` |

Lokala modeller svarar ibland i fel format. Då frågas de igen, och annars räknas rösten
som HOLD. Prenumerationen i Claude Code har användningsgränser, så vid många möten kan
rådet pausas tills gränsen återställs.

## 3. Webbsidan

Öppna `http://localhost:8000` medan boten kör.

- **Översikt:** eget kapital, dagens och totalt resultat, avgifter, graf över utvecklingen,
  positioner (med "netto om stängd nu"), öppna order, kurser och **diskussionen live**
  medan agenterna talar.
- **Möten:** alla möten med röster, motiveringar, hela diskussionen och underlaget.
- **Affärer:** varje avslutad affär med brutto, avgifter och netto. Klicka på en affär
  för att se mötet som ledde till den. Statistiken visar vinstandel, snittvinst och
  snittförlust, och hur ofta djävulens advokat övertalade någon.
- **Avgifter:** vart pengarna gått (courtage, valuta, spread, låneavgift), hur mycket av
  vinsten avgifterna åt upp och hur rådet räknar just nu.
- **Inställningar:** aktier, mötesintervall, AI-motor, riskgränser, courtageklass med
  mera. Ändringarna gäller direkt, utom de som är märkta "kräver omstart".
- **Knappar:** *Möte nu* startar ett möte direkt. *Pausa* stoppar nya affärer och
  schemalagda möten, men skyddsorder och stängningen vid dagens slut fortsätter.

**Från mobilen** på samma wifi: starta med `python main.py --host 0.0.0.0 --token
ditt-lösenord` och öppna `http://<datorns-ip>:8000/?token=ditt-lösenord`. Utan
lösenord startar sidan bara på den egna datorn. Lägg aldrig ut sidan på internet,
eftersom den styr handeln.

## 4. Avgifter och hur de påverkar vinsten

Boten räknar med alla kostnader för att köpa och sälja:

| Kostnad | Hur |
|---|---|
| **Courtage** | Nordnets klasser: Mini 0,25 % (min 1 kr), Liten 0,15 % (min 39 kr), Mellan 0,069 % (min 69 kr), Fast 99 kr, eller Gratis (kampanj). Välj din klass under Inställningar. |
| **Spread** | Du köper på säljkursen och säljer på köpkursen. Kommer ur orderboken, eller uppskattas (standard 0,10 %) när källan saknar den. |
| **Valutaväxling** | 0,25 % per affär för aktier i annan valuta än SEK. |
| **Låneavgift** | Blankningar som ligger kvar över natten (standard 3 % per år). |
| **Skatt** | Med kontotyp AF räknas 30 % skatt på vinst i statistiken. ISK har ingen skatt per affär. |

Så används avgifterna:

1. **Agenterna får en avgiftskalkyl i varje möte**: hur mycket kursen måste röra sig bara
   för att gå jämnt ut, och om ordern är så liten att minimicourtaget blir dyrt.
2. **En hård regel stoppar olönsamma affärer.** Vinstmålet måste vara minst
   `min_edge_multiple` gånger avgifterna (standard 2×), och en stop-loss får inte vara
   snävare än avgifterna. Exempel med Mini på 10 000 kr: kursen måste upp 0,6 % för att gå
   jämnt ut, så ett vinstmål under 1,2 % stoppas även om rådet röstat för affären.
3. **Allt redovisas brutto och netto** på webbsidan, inklusive hur många affärer som var
   vinst före avgifter men förlust efter.

## Hur ett möte går till

1. Kurser hämtas varje minut, och pappersmäklaren prövar order mot köp- och säljkurs.
2. Var `interval` minut (standard 15) håller rådet möte för varje aktie i `rounds` rundor.
3. Analytikerna röstar: **BUY, SELL, SHORT, COVER eller HOLD**, med storlek, ordertyp,
   stop-loss, vinstmål och trailing stop. Minst 3 av 4 måste vara överens.
4. Avgiftsregeln prövas, och sedan läggs ordern. Skydd läggs direkt: **vinstmål + stop
   (OCO)**, **trailing stop** eller vanlig **stop-loss**.
5. Hårda gränser som agenterna inte kan ändra: max positionsstorlek, högsta stop-loss,
   max dagsförlust och att allt stängs vid `close_at`.

Allt sparas i mappen `data/`: `portfolio.json`, `settings.json`, `meetings.jsonl` och
`history.jsonl`.

## Live-handel

Koden för riktiga order via Nordnet finns (`--live` plus `NORDNET_LIVE_CONFIRM=JA`) men
kräver Nordnets API, som just nu är stängt för nya kunder.

> ⚠️ Daytrading innebär stor risk och de flesta förlorar pengar. Programmet är ett
> experiment, inte finansiell rådgivning.
