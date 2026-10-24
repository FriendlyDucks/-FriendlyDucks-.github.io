"""Ordermodell som speglar Nordnets API v2 (POST /accounts/{accid}/orders).

Samma objekt används av pappershandeln och av den riktiga Nordnet-mäklaren,
så att en strategi som fungerar på papper skickar exakt samma order live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    # Nordnets ordertyper. Vilka som är tillåtna beror på värdepappret
    # (se GET /tradables/info/{market_id}:{identifier}).
    LIMIT = "LIMIT"
    NORMAL = "NORMAL"
    FAK = "FAK"  # Fill and kill: fyll det som går direkt, resten makuleras
    FOK = "FOK"  # Fill or kill: allt eller inget, direkt


class Activation(str, Enum):
    NONE = "NONE"
    STOP_ACTPRICE = "STOP_ACTPRICE"  # Stop-loss: aktiveras vid ett triggerpris
    STOP_ACTPRICE_PERC = "STOP_ACTPRICE_PERC"  # Trailing stop i procent
    OCO_STOP_ACTPRICE = "OCO_STOP_ACTPRICE"  # Limitorder + stop-loss, en makulerar den andra
    MANUAL = "MANUAL"  # Vilande order som aktiveras manuellt


class Validity(str, Enum):
    DAY = "DAY"
    UNTIL_DATE = "UNTIL_DATE"


@dataclass(frozen=True)
class Tradable:
    """Ett handelsbart värdepapper hos Nordnet."""

    symbol: str  # T.ex. "VOLV B"
    market_id: int  # 11 = Stockholmsbörsen
    identifier: str  # Nordnets id för värdepappret på marknaden
    currency: str = "SEK"
    lot_size: int = 1
    data_symbol: Optional[str] = None  # Symbol hos datakällan om den skiljer sig, t.ex. "VOLV-B.ST"

    @property
    def key(self) -> str:
        return f"{self.market_id}:{self.identifier}"


@dataclass
class OrderRequest:
    tradable: Tradable
    side: Side
    volume: int
    price: Optional[float] = None
    order_type: OrderType = OrderType.LIMIT
    validity: Validity = Validity.DAY
    valid_until: Optional[str] = None
    open_volume: Optional[int] = None  # Iceberg: visad volym
    activation: Activation = Activation.NONE
    trigger_value: Optional[float] = None
    trigger_condition: Optional[str] = None  # "<=" eller ">="
    target_value: Optional[float] = None
    reference: Optional[str] = None

    def to_nordnet_params(self) -> dict[str, str]:
        """Parametrar enligt Nordnets order-endpoint."""
        params: dict[str, object] = {
            "market_id": self.tradable.market_id,
            "identifier": self.tradable.identifier,
            "side": self.side.value,
            "volume": self.volume,
            "currency": self.tradable.currency,
            "order_type": self.order_type.value,
        }
        if self.price is not None and self.activation != Activation.STOP_ACTPRICE_PERC:
            params["price"] = self.price
        if self.validity == Validity.UNTIL_DATE and self.valid_until:
            params["valid_until"] = self.valid_until
        if self.open_volume:
            params["open_volume"] = self.open_volume
        if self.activation != Activation.NONE:
            params["activation_condition"] = self.activation.value
            for name in ("trigger_value", "trigger_condition", "target_value"):
                value = getattr(self, name)
                if value is not None:
                    params[name] = value
        if self.reference:
            params["reference"] = self.reference[:32]
        return {k: str(v) for k, v in params.items()}


@dataclass
class Quote:
    bid: float
    ask: float
    last: float
    bid_volume: float = 0
    ask_volume: float = 0
    timestamp: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass
class TradePlan:
    """Rådets beslut, översatt till en entry-order plus skydd."""

    action: str  # BUY, SELL, SHORT, COVER, HOLD
    position_pct: float = 0.0
    order_type: OrderType = OrderType.LIMIT
    limit_offset_pct: float = 0.0  # Hur långt från mittkursen limitpriset läggs
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    reasons: list[str] = field(default_factory=list)
