"""Agentrådet: fyra analytiker som argumenterar för sina idéer och en
djävulens advokat som alltid säger emot och försöker övertala de andra."""

from __future__ import annotations

from dataclasses import dataclass

from llm import DEFAULT_CLAUDE_MODEL as MODEL, Backend, LLMError, extract_json

COMMON_RULES = """Du är med i ett handelsråd med fem agenter som diskuterar en daytrade-affär.
Rådet handlar via Nordnet, just nu på papper med riktiga kurser. Svara alltid på svenska.
Möjliga beslut: BUY (köpa/öka lång position), SELL (stänga lång position), SHORT (blanka),
COVER (köpa tillbaka blankade aktier) eller HOLD (göra ingenting).
Ordertyper: LIMIT (ligger kvar i orderboken), FAK (fyll det som går direkt), FOK (allt eller inget direkt).
Skydd som läggs direkt efter köp/blankning: stop-loss, trailing stop eller vinstmål + stop (OCO).
Tänk på spread, courtage och att positioner ska vara stängda innan börsen stänger.
Håll varje inlägg under 120 ord. Tilltala de andra agenterna vid namn när du bemöter dem.
Hitta inte på siffror – använd bara marknadsdatan och det som sagts i diskussionen."""


@dataclass
class Agent:
    name: str
    role: str
    persona: str
    votes: bool = True

    @property
    def system(self) -> str:
        return f"{COMMON_RULES}\n\nDitt namn är {self.name}. Din roll: {self.role}.\n{self.persona}"


ANALYSTS = [
    Agent(
        name="Tekla",
        role="Teknisk analytiker",
        persona=(
            "Du läser diagram och orderbok: RSI, glidande medelvärden, VWAP, dagens högsta/lägsta, volym, "
            "bid/ask och spread. Lyft fram de tekniska fördelarna och föreslå konkreta in- och utgångsnivåer."
        ),
    ),
    Agent(
        name="Moa",
        role="Momentum- och trendstrateg",
        persona=(
            "Du letar efter starka intradagsrörelser och volymutbrott som går att rida på – uppåt (köp) "
            "eller nedåt (blankning). Argumentera för hur rådet kan tjäna pengar på dagens momentum."
        ),
    ),
    Agent(
        name="Nils",
        role="Nyhets- och sentimentanalytiker",
        persona=(
            "Du tolkar rubriker och marknadsstämning. Förklara vilka katalysatorer som kan driva kursen "
            "och om sentimentet ger en fördel för köp eller sälj."
        ),
    ),
    Agent(
        name="Rut",
        role="Riskhanterare",
        persona=(
            "Du fokuserar på positionsstorlek, stop-loss, trailing stop, vinstmål och risk/reward. Du är positiv "
            "till affärer med bra riskprofil och föreslår storlek, ordertyp och skyddsnivåer."
        ),
    ),
]

DEVIL = Agent(
    name="Djävulens advokat",
    role="Ständig motståndare",
    persona=(
        "Du är ALLTID negativ. Ditt enda jobb är att säga emot den åsikt som just nu leder i rådet, "
        "hitta svagheterna i varje argument och försöka övertala de andra att ändra sig. "
        "Om de vill köpa förklarar du varför det är en fälla; om de vill sälja eller avvakta "
        "argumenterar du för att de missar något. Håll med ingen. Var skarp och övertygande, "
        "angrip konkreta påståenden och ställ obekväma frågor direkt till agenterna vid namn."
    ),
    votes=False,
)

VOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "SHORT", "COVER", "HOLD"]},
        "confidence": {"type": "integer", "description": "0–100"},
        "position_pct": {
            "type": "number",
            "description": "Andel av kapitalet (0–100) vid BUY/SHORT. 0 annars.",
        },
        "order_type": {"type": "string", "enum": ["LIMIT", "FAK", "FOK"]},
        "limit_offset_pct": {
            "type": "number",
            "description": "Limitpris i procent från mittkursen. Positivt = mer aggressivt (betala mer vid köp, ta lägre vid sälj).",
        },
        "stop_loss_pct": {"type": "number", "description": "Stop-loss i procent från ingångspriset. 0 = ingen."},
        "take_profit_pct": {"type": "number", "description": "Vinstmål i procent. 0 = inget (då används ren stop-loss)."},
        "trailing_stop_pct": {"type": "number", "description": "Trailing stop i procent. 0 = ingen. Ersätter stop-loss/vinstmål."},
        "changed_mind_because_of_devil": {"type": "boolean"},
        "reasoning": {"type": "string", "description": "En till två meningar."},
    },
    "required": [
        "action", "confidence", "position_pct", "order_type", "limit_offset_pct", "stop_loss_pct",
        "take_profit_pct", "trailing_stop_pct", "changed_mind_because_of_devil", "reasoning",
    ],
    "additionalProperties": False,
}


def normalize_vote(raw: dict) -> dict:
    """Säkerställer en giltig röst även om en lokal modell svarar slarvigt."""

    def num(key: str) -> float:
        try:
            return max(0.0, float(raw.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    action = str(raw.get("action", "HOLD")).upper()
    order_type = str(raw.get("order_type", "LIMIT")).upper()
    try:
        offset = max(-1.0, min(float(raw.get("limit_offset_pct", 0) or 0), 1.0))
    except (TypeError, ValueError):
        offset = 0.0
    return {
        "action": action if action in ("BUY", "SELL", "SHORT", "COVER", "HOLD") else "HOLD",
        "confidence": int(min(num("confidence"), 100)),
        "position_pct": min(num("position_pct"), 100.0),
        "order_type": order_type if order_type in ("LIMIT", "FAK", "FOK") else "LIMIT",
        "limit_offset_pct": offset,
        "stop_loss_pct": num("stop_loss_pct"),
        "take_profit_pct": num("take_profit_pct"),
        "trailing_stop_pct": num("trailing_stop_pct"),
        "changed_mind_because_of_devil": bool(raw.get("changed_mind_because_of_devil", False)),
        "reasoning": str(raw.get("reasoning", ""))[:500],
    }


class Council:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def _ask(self, agent: Agent, prompt: str, schema: dict | None = None) -> str:
        return self.backend.complete(agent.system, prompt, schema)

    def debate(self, snapshot_text: str, portfolio_text: str, rounds: int) -> list[tuple[str, str]]:
        transcript: list[tuple[str, str]] = []

        def render() -> str:
            if not transcript:
                return "(Diskussionen har inte börjat än.)"
            return "\n\n".join(f"[{who}]: {msg}" for who, msg in transcript)

        for rnd in range(1, rounds + 1):
            print(f"\n--- Runda {rnd}/{rounds} ---")
            for agent in [*ANALYSTS, DEVIL]:
                if agent is DEVIL:
                    task = (
                        "Säg emot rådets ledande uppfattning så övertygande du kan och försök "
                        "få minst en av analytikerna att byta sida."
                    )
                elif rnd == 1:
                    task = "Ge din första bedömning: köpa, blanka, stänga eller avvakta, och vilka fördelar du ser."
                else:
                    task = (
                        "Bemöt de andra – särskilt Djävulens advokat. Står du fast vid din bedömning "
                        "eller har du blivit övertalad? Motivera."
                    )
                prompt = (
                    f"MARKNADSDATA\n{snapshot_text}\n\nPORTFÖLJ\n{portfolio_text}\n\n"
                    f"DISKUSSION HITTILLS\n{render()}\n\nDIN UPPGIFT\n{task}"
                )
                message = self._ask(agent, prompt)
                transcript.append((agent.name, message))
                print(f"\n[{agent.name} – {agent.role}]\n{message}")
        return transcript

    def vote(self, snapshot_text: str, portfolio_text: str, transcript: list[tuple[str, str]]) -> dict[str, dict]:
        discussion = "\n\n".join(f"[{who}]: {msg}" for who, msg in transcript)
        prompt = (
            f"MARKNADSDATA\n{snapshot_text}\n\nPORTFÖLJ\n{portfolio_text}\n\n"
            f"HELA DISKUSSIONEN\n{discussion}\n\n"
            "Diskussionen är slut. Lägg din slutliga röst med ordertyp och skyddsnivåer."
        )
        votes = {}
        for agent in ANALYSTS:
            for attempt in range(2):
                try:
                    votes[agent.name] = normalize_vote(extract_json(self._ask(agent, prompt, schema=VOTE_SCHEMA)))
                    break
                except (LLMError, ValueError) as e:
                    print(f"  Ogiltig röst från {agent.name} ({e}){' – försöker igen' if attempt == 0 else ''}")
            else:
                votes[agent.name] = normalize_vote({"action": "HOLD", "reasoning": "kunde inte tolka rösten"})
        return votes
