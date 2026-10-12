"""Nordnets realtidsflöden (public/private feed).

Flödet är en TLS-socket där varje meddelande är en JSON-rad:
    -> {"cmd": "login", "args": {"session_key": ..., "service": "NEXTAPI"}}
    -> {"cmd": "subscribe", "args": {"t": "price", "m": 11, "i": "101"}}
    <- {"type": "price", "data": {"i": "101", "m": 11, "bid": ..., "ask": ..., "last": ...}}
Publikt flöde: price, depth, trade, indicator, news. Privat flöde: order, trade.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from typing import Callable, Optional

from nordnet.client import SERVICE_NAME

Handler = Callable[[str, dict], None]


class Feed:
    def __init__(self, hostname: str, port: int, session_key: str, on_message: Handler, encrypted: bool = True):
        self.hostname = hostname
        self.port = port
        self.session_key = session_key
        self.on_message = on_message
        self.encrypted = encrypted
        self.sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._subscriptions: list[dict] = []
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._connect()
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _connect(self) -> None:
        raw = socket.create_connection((self.hostname, self.port), timeout=30)
        if self.encrypted:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.hostname)
        raw.settimeout(None)
        self.sock = raw
        self._send({"cmd": "login", "args": {"session_key": self.session_key, "service": SERVICE_NAME}})
        for sub in self._subscriptions:
            self._send({"cmd": "subscribe", "args": sub})

    def _send(self, cmd: dict) -> None:
        with self._lock:
            assert self.sock is not None
            self.sock.sendall((json.dumps(cmd) + "\n").encode())

    def subscribe(self, kind: str, market_id: int, identifier: str) -> None:
        args = {"t": kind, "m": market_id, "i": identifier}
        self._subscriptions.append(args)
        if self.sock:
            self._send({"cmd": "subscribe", "args": args})

    def _read_loop(self) -> None:
        buffer = ""
        while self._running:
            try:
                assert self.sock is not None
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("flödet stängdes")
                buffer += chunk.decode("utf-8")
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    if line.strip():
                        msg = json.loads(line)
                        self.on_message(msg.get("type", ""), msg.get("data", {}))
            except Exception as e:
                if not self._running:
                    return
                print(f"[feed {self.hostname}] {e} – kopplar upp igen")
                try:
                    self._connect()
                    buffer = ""
                except Exception as e2:
                    print(f"[feed {self.hostname}] återanslutning misslyckades: {e2}")
                    threading.Event().wait(5)

    def stop(self) -> None:
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
