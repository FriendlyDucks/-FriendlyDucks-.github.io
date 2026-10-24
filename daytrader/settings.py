"""Inställningar som kan ändras medan boten kör (från webbsidan eller settings.json)."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import config
from fees import COURTAGE_CLASSES


@dataclass
class Settings:
    # Handel
    tickers: list[str] = field(default_factory=lambda: ["VOLV B", "ERIC B"])
    data_source: str = "auto"
    market_id: int = 11
    paused: bool = False
    # Rådet
    rounds: int = 2
    interval: int = 15
    min_agree: int = 3
    # AI
    backend: str = "ollama"
    model: str = "claude-opus-5-5"
    effort: str = "low"
    ollama_model: str = "qwen2.5:14b"
    ollama_url: str = "http://localhost:11434"
    # Risk
    max_position_pct: float = 20.0
    default_stop: float = 1.5
    max_stop: float = 5.0
    max_daily_loss: float = 3.0
    allow_short: bool = True
    shortable: list[str] = field(default_factory=list)
    # Avgifter
    courtage_class: str = "MINI"
    spread_pct: float = 0.10
    fx_fee_pct: float = 0.25
    borrow_fee_pct_year: float = 3.0
    account_type: str = "ISK"
    min_edge_multiple: float = 2.0
    # Tider
    open: str = "09:00"
    close_at: str = "17:20"
    poll: int = 20
    cash: float = 100_000.0


# Beskrivning för webbsidan: nyckel -> (grupp, etikett, val eller None, hjälptext, kräver omstart)
SCHEMA: dict[str, tuple[str, str, list | None, str, bool]] = {
    "tickers": ("Handel", "Aktier att bevaka", None, "Kommaseparerat, Nordnet-namn, t.ex. VOLV B, ERIC B. Lägg till =SYMBOL för datakällans symbol.", False),
    "data_source": ("Handel", "Datakälla", ["auto", "nordnet", "twelvedata", "eodhd"], "auto = Nordnet om API finns, annars Twelve Data, annars EODHD", True),
    "market_id": ("Handel", "Nordnet marknads-id", None, "11 = Stockholmsbörsen", True),
    "paused": ("Handel", "Pausad", None, "Inga nya affärer. Skyddsorder och stängning vid dagens slut fortsätter.", False),
    "rounds": ("Rådet", "Diskussionsrundor", None, "Fler rundor = grundligare men långsammare", False),
    "interval": ("Rådet", "Minuter mellan möten", None, "", False),
    "min_agree": ("Rådet", "Antal överens (av 4)", None, "Så många analytiker måste rösta likadant", False),
    "backend": ("AI", "AI-motor", ["ollama", "claude-code", "api"], "ollama = gratis lokalt, claude-code = prenumeration, api = krediter", False),
    "model": ("AI", "Claude-modell", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "Gäller claude-code och api", False),
    "effort": ("AI", "Tankenivå (api)", ["low", "medium", "high"], "", False),
    "ollama_model": ("AI", "Ollama-modell", None, "T.ex. qwen2.5:14b eller qwen2.5:7b", False),
    "ollama_url": ("AI", "Ollama-adress", None, "", False),
    "max_position_pct": ("Risk", "Max % av kapitalet per affär", None, "", False),
    "default_stop": ("Risk", "Standard stop-loss %", None, "Används om rådet inte anger något", False),
    "max_stop": ("Risk", "Högsta stop-loss %", None, "", False),
    "max_daily_loss": ("Risk", "Max dagsförlust %", None, "Inga nya affärer efter så stor förlust", False),
    "allow_short": ("Risk", "Tillåt blankning", None, "", False),
    "shortable": ("Risk", "Får blankas (tomt = alla)", None, "Kommaseparerat", False),
    "courtage_class": ("Avgifter", "Courtageklass", list(COURTAGE_CLASSES), "Nordnet: Mini 0,25 % (min 1 kr), Liten 0,15 % (min 39 kr), Mellan 0,069 % (min 69 kr), Fast 99 kr", False),
    "spread_pct": ("Avgifter", "Uppskattad spread %", None, "Används när datakällan saknar köp-/säljkurs", False),
    "fx_fee_pct": ("Avgifter", "Valutaväxling %", None, "För aktier i annan valuta än SEK", False),
    "borrow_fee_pct_year": ("Avgifter", "Låneavgift blankning % per år", None, "", False),
    "account_type": ("Avgifter", "Kontotyp", ["ISK", "AF"], "AF: 30 % skatt på vinst räknas in i statistiken", False),
    "min_edge_multiple": ("Avgifter", "Vinstmål minst × avgifterna", None, "Affärer där vinstmålet inte täcker avgifterna så många gånger stoppas", False),
    "open": ("Tider", "Börsen öppnar", None, "", False),
    "close_at": ("Tider", "Stäng allt efter", None, "", False),
    "poll": ("Tider", "Sekunder mellan kurskontroller", None, "", False),
    "cash": ("Tider", "Startkapital (papper)", None, "Gäller bara en ny portfölj", True),
}


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.version = 0
        self.s = Settings(
            backend=config.env("LLM_BACKEND", "ollama"),
            model=config.env("CLAUDE_MODEL", "claude-opus-5-5"),
            ollama_model=config.env("OLLAMA_MODEL", "qwen2.5:14b"),
            ollama_url=config.env("OLLAMA_URL", "http://localhost:11434"),
        )
        if path.exists():
            self.update(json.loads(path.read_text()), save=False)

    def get(self) -> Settings:
        with self.lock:
            return Settings(**asdict(self.s))

    def as_dict(self) -> dict:
        with self.lock:
            return asdict(self.s)

    def update(self, changes: dict[str, Any], save: bool = True) -> list[str]:
        """Uppdaterar och typkonverterar. Returnerar namnen på fält som krävde omstart."""
        types = {f.name: f.type for f in fields(Settings)}
        restart = []
        with self.lock:
            for key, value in changes.items():
                if key not in types:
                    continue
                current = getattr(self.s, key)
                if isinstance(current, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "ja", "on")
                elif isinstance(current, list):
                    if isinstance(value, str):
                        value = [v.strip() for v in value.split(",") if v.strip()]
                    value = [str(v) for v in value]
                elif isinstance(current, int):
                    value = int(float(value))
                elif isinstance(current, float):
                    value = float(value)
                else:
                    value = str(value).strip()
                options = SCHEMA.get(key, (None, None, None))[2]
                if options and value not in options:
                    raise ValueError(f"{key}: ogiltigt värde {value!r}")
                if value != current:
                    setattr(self.s, key, value)
                    if SCHEMA.get(key, ("", "", None, "", False))[4]:
                        restart.append(key)
            self.version += 1
            if save:
                self.path.write_text(json.dumps(asdict(self.s), indent=2, ensure_ascii=False))
        return restart
