"""Avgifter vid köp och sälj och hur de påverkar vinsten.

Courtageklasserna följer Nordnets prislista för nordiska aktier (hösten 2026).
Kontrollera din egen klass på nordnet.se – priserna ändras ibland.
"""

from __future__ import annotations

from dataclasses import dataclass


def _kr(v: float, d: int = 0) -> str:
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def _pct(v: float, d: int = 3) -> str:
    return f"{v:.{d}f}".replace(".", ",")

# klass: (procent av ordervärdet, minsta avgift i kr, fast avgift i kr eller None)
COURTAGE_CLASSES: dict[str, tuple[float, float, float | None]] = {
    "MINI": (0.25, 1.0, None),
    "LITEN": (0.15, 39.0, None),
    "MELLAN": (0.069, 69.0, None),
    "FAST": (0.0, 0.0, 99.0),
    "GRATIS": (0.0, 0.0, 0.0),  # T.ex. Nordnets kampanj med courtagefri handel för nya kunder
}


@dataclass
class FeeModel:
    courtage_class: str = "MINI"
    spread_pct: float = 0.10  # Uppskattad spread när datakällan saknar köp-/säljkurs
    fx_fee_pct: float = 0.25  # Valutaväxling vid handel i utländsk valuta (per växling)
    borrow_fee_pct_year: float = 3.0  # Låneavgift för blankade aktier, per år, dras per natt
    account_type: str = "ISK"  # ISK = schablonskatt, AF = 30 % skatt på vinst
    min_edge_multiple: float = 2.0  # Vinstmålet måste vara minst så här många gånger avgifterna

    def courtage(self, value: float) -> float:
        pct, minimum, flat = COURTAGE_CLASSES.get(self.courtage_class.upper(), COURTAGE_CLASSES["MINI"])
        if flat is not None:
            return flat
        return max(minimum, value * pct / 100)

    def fx_fee(self, value: float, currency: str) -> float:
        return value * self.fx_fee_pct / 100 if currency.upper() != "SEK" else 0.0

    def trade_fees(self, value: float, currency: str = "SEK") -> float:
        """Avgifter för en enskild affär (köp eller sälj)."""
        return self.courtage(value) + self.fx_fee(value, currency)

    def round_trip_pct(self, value: float, currency: str = "SEK", spread_pct: float | None = None) -> float:
        """Hur många procent kursen måste röra sig bara för att gå jämnt ut (in + ut)."""
        if value <= 0:
            return 0.0
        fees = 2 * self.trade_fees(value, currency)
        spread = self.spread_pct if spread_pct is None else spread_pct
        return fees / value * 100 + spread

    def borrow_fee_per_night(self, short_value: float) -> float:
        return short_value * self.borrow_fee_pct_year / 100 / 365

    def efficient_order_value(self) -> float:
        """Ordervärde där minimiavgiften slutar dominera (gäller klasser med minsta avgift)."""
        pct, minimum, flat = COURTAGE_CLASSES.get(self.courtage_class.upper(), COURTAGE_CLASSES["MINI"])
        if flat:
            return flat / 0.001  # Fast avgift: under 0,1 % först vid ~99 000 kr
        return minimum / (pct / 100) if pct else 0.0

    def tax_on(self, net_profit: float) -> float:
        """Uppskattad skatt på realiserat resultat (förenklat)."""
        if self.account_type.upper() != "AF":
            return 0.0  # ISK/KF: schablonskatt på kapitalet, inte på varje affär
        return net_profit * 0.30 if net_profit > 0 else net_profit * 0.30 * 0.70

    def describe(self, order_value: float, currency: str = "SEK", spread_pct: float | None = None) -> str:
        c = self.courtage(order_value)
        rt = self.round_trip_pct(order_value, currency, spread_pct)
        lines = [
            f"Courtageklass: {self.courtage_class} – {_kr(c, 2)} kr per affär vid ordervärde {_kr(order_value)} kr "
            f"({_pct(c / order_value * 100 if order_value else 0)} %)",
            f"Spread (kostnad att gå in och ut direkt): {_pct(self.spread_pct if spread_pct is None else spread_pct)} %",
        ]
        if currency.upper() != "SEK":
            lines.append(f"Valutaväxling: {_pct(self.fx_fee_pct, 2)} % per affär ({currency})")
        lines += [
            f"Kursen måste röra sig minst {_pct(rt)} % åt rätt håll för att gå jämnt ut efter avgifter.",
            f"Rådets regel: vinstmålet måste vara minst {_pct(self.min_edge_multiple, 1)} × detta = {_pct(rt * self.min_edge_multiple)} %.",
            f"Blankning kostar {_pct(self.borrow_fee_pct_year, 1)} % per år i låneavgift för positioner över natten.",
        ]
        eff = self.efficient_order_value()
        if eff:
            lines.append(f"Under cirka {_kr(eff)} kr per order blir courtaget procentuellt dyrare.")
        if self.account_type.upper() == "AF":
            lines.append("Kontotyp AF: 30 % skatt på vinst – en vinst på 100 kr blir 70 kr netto.")
        else:
            lines.append("Kontotyp ISK: ingen skatt per affär (schablonskatt på kapitalet).")
        return "\n".join(lines)
