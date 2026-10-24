"""Klient för Nordnets officiella API v2 (https://www.nordnet.se/externalapi/docs/api).

Inloggning sker med en Ed25519-nyckel som du laddar upp under
Mina sidor -> Inställningar -> Min profil -> Säkerhet. Där får du en API-nyckel (UUID).
Flödet följer Nordnets officiella exempel (github.com/nordnet/next-api-v2-examples):

    POST /api/2/login/start   {"api_key": ...}               -> {"challenge": ...}
    POST /api/2/login/verify  {"service": "NEXTAPI", "api_key": ..., "signature": base64(ed25519(challenge))}
                                                              -> {"session_key", "expires_in", "public_feed", "private_feed"}

Därefter används HTTP Basic med session_key som både användarnamn och lösenord.
Sessionen hålls vid liv med PUT /api/2/login.
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import serialization

from orders import OrderRequest, Tradable

SERVICE_NAME = "NEXTAPI"


class NordnetError(RuntimeError):
    pass


class NordnetClient:
    def __init__(self, api_key: str, private_key_path: str | Path, country: str = "se", timeout: float = 15.0):
        self.api_key = api_key
        self.private_key_path = Path(private_key_path).expanduser()
        self.base = f"https://public.nordnet.{country}/api/2"
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"Accept": "application/json", "Accept-Language": "sv"})
        self.session_key: Optional[str] = None
        self.login_info: dict[str, Any] = {}
        self._keepalive: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------- Inloggning ----------

    def _sign(self, challenge: str) -> str:
        key = serialization.load_ssh_private_key(self.private_key_path.read_bytes(), password=None)
        return base64.b64encode(key.sign(challenge.encode("utf-8"))).decode()

    def login(self) -> dict[str, Any]:
        status = self._request("GET", "/", auth=False)
        if not status.get("system_running", True):
            raise NordnetError(f"Nordnets system är inte igång: {status.get('message')}")

        challenge = self._request("POST", "/login/start", json={"api_key": self.api_key}, auth=False)["challenge"]
        info = self._request(
            "POST",
            "/login/verify",
            json={"service": SERVICE_NAME, "api_key": self.api_key, "signature": self._sign(challenge)},
            auth=False,
        )
        self.session_key = info["session_key"]
        token = base64.b64encode(f"{self.session_key}:{self.session_key}".encode()).decode()
        self.http.headers["Authorization"] = f"Basic {token}"
        self.login_info = info
        self._start_keepalive(int(info.get("expires_in", 1800)))
        return info

    def _start_keepalive(self, expires_in: int) -> None:
        interval = max(60, expires_in // 3)

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self._request("PUT", "/login")
                except Exception as e:  # Logga och försök igen nästa varv
                    print(f"[nordnet] kunde inte förnya sessionen: {e}")

        self._keepalive = threading.Thread(target=loop, daemon=True)
        self._keepalive.start()

    def logout(self) -> None:
        self._stop.set()
        if self.session_key:
            try:
                self._request("DELETE", "/login")
            finally:
                self.session_key = None

    # ---------- HTTP ----------

    def _request(self, method: str, path: str, auth: bool = True, **kwargs) -> Any:
        if auth and not self.session_key:
            raise NordnetError("Inte inloggad – anropa login() först")
        for attempt in range(3):
            r = self.http.request(method, self.base + path, timeout=self.timeout, **kwargs)
            if r.status_code == 429:  # Nordnet begränsar antalet anrop per sekund
                time.sleep(1 + attempt)
                continue
            break
        if r.status_code == 204 or not r.content:
            return None
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise NordnetError(f"{method} {path} -> {r.status_code}: {body}")
        return body

    # ---------- Konton ----------

    def accounts(self) -> list[dict]:
        return self._request("GET", "/accounts")

    def account_info(self, accid: int) -> list[dict]:
        return self._request("GET", f"/accounts/{accid}/info")

    def positions(self, accid: int) -> list[dict]:
        return self._request("GET", f"/accounts/{accid}/positions")

    def orders(self, accid: int, deleted: bool = False) -> list[dict]:
        return self._request("GET", f"/accounts/{accid}/orders", params={"deleted": str(deleted).lower()})

    def trades(self, accid: int, days: int = 0) -> list[dict]:
        return self._request("GET", f"/accounts/{accid}/trades", params={"days": days})

    # ---------- Order ----------

    def place_order(self, accid: int, order: OrderRequest) -> dict:
        # Nordnets order-endpoint tar formulärparametrar (application/x-www-form-urlencoded).
        return self._request("POST", f"/accounts/{accid}/orders", data=order.to_nordnet_params())

    def modify_order(self, accid: int, order_id: int, **changes: Any) -> dict:
        return self._request("PUT", f"/accounts/{accid}/orders/{order_id}", data={k: str(v) for k, v in changes.items()})

    def cancel_order(self, accid: int, order_id: int) -> dict:
        return self._request("DELETE", f"/accounts/{accid}/orders/{order_id}")

    def activate_order(self, accid: int, order_id: int) -> dict:
        return self._request("PUT", f"/accounts/{accid}/orders/{order_id}/activate")

    # ---------- Marknadsdata ----------

    def tradable_info(self, tradable: Tradable | str) -> dict:
        key = tradable.key if isinstance(tradable, Tradable) else tradable
        return self._request("GET", f"/tradables/info/{key}")[0]

    def public_trades(self, tradable: Tradable) -> list[dict]:
        result = self._request("GET", f"/tradables/trades/{tradable.key}")
        return result[0].get("trades", []) if result else []

    def lookup_symbol(self, symbol: str) -> list[dict]:
        return self._request("GET", f"/instruments/lookup/symbol/{requests.utils.quote(symbol)}")

    def search(self, query: str, limit: int = 10) -> Any:
        return self._request("GET", "/main_search", params={"query": query, "limit": limit})

    def news(self, instrument_id: Optional[int] = None, limit: int = 5) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if instrument_id:
            params["instrument_id"] = instrument_id
        return self._request("GET", "/news", params=params)

    def tradable_from_symbol(self, symbol: str, market_id: int = 11, data_symbol: Optional[str] = None) -> Tradable:
        """Slå upp ett värdepapper, t.ex. 'VOLV B' på Stockholmsbörsen (market_id 11)."""
        for inst in self.lookup_symbol(symbol):
            for t in inst.get("tradables", []):
                if t.get("market_id") == market_id:
                    return Tradable(
                        symbol=inst.get("symbol", symbol),
                        market_id=market_id,
                        identifier=str(t["identifier"]),
                        currency=inst.get("currency", "SEK"),
                        lot_size=int(t.get("lot_size", 1)),
                        data_symbol=data_symbol,
                    )
        raise NordnetError(f"Hittade inte {symbol} på marknad {market_id}")
