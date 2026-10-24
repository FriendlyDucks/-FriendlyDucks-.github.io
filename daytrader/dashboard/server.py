"""Webbsida för inställningar, utveckling och realtidsinfo. Körs i samma process som boten."""

from __future__ import annotations

import hmac
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from settings import SCHEMA

INDEX = Path(__file__).with_name("index.html")


def make_handler(app, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        # ---------- Hjälpare ----------

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            given = self.headers.get("X-Token") or query.get("token", [""])[0]
            return hmac.compare_digest(given, token)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length))

        # ---------- GET ----------

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/favicon.ico":
                svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M1 13l4-5 3 3 6-8" stroke="#2f6fdf" stroke-width="2" fill="none"/></svg>'
                return self._send(200, svg, "image/svg+xml")
            if not self._authorized(query):
                return self._json({"fel": "fel lösenord"}, 401)
            try:
                if url.path == "/api/state":
                    return self._json(app.state())
                if url.path == "/api/history":
                    rows = app.history.last(20_000)
                    if query.get("range", ["all"])[0] == "day":
                        today = rows[-1]["t"][:10] if rows else ""
                        rows = [r for r in rows if r["t"].startswith(today)]
                    step = max(1, len(rows) // 600)  # Glesa ut långa serier
                    return self._json(rows[::step] + (rows[-1:] if rows and (len(rows) - 1) % step else []))
                if url.path == "/api/meetings":
                    limit = int(query.get("limit", ["30"])[0])
                    return self._json(app.meetings.last(limit)[::-1])
                if url.path.startswith("/api/meeting/"):
                    m = app.meetings.find("id", url.path.rsplit("/", 1)[1])
                    return self._json(m or {"fel": "hittades inte"}, 200 if m else 404)
                if url.path == "/api/settings":
                    schema = [{"key": k, "grupp": g, "etikett": l, "val": o, "hjälp": h, "omstart": r}
                              for k, (g, l, o, h, r) in SCHEMA.items()]
                    return self._json({"värden": app.store.as_dict(), "schema": schema})
                if url.path == "/api/events":
                    return self._stream()
            except Exception as e:
                return self._json({"fel": str(e)}, 500)
            self._json({"fel": "finns inte"}, 404)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = app.events.subscribe()
            try:
                for event in list(app.events.recent)[-150:]:
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.events.unsubscribe(q)
                self.close_connection = True

        # ---------- POST ----------

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if not self._authorized(parse_qs(url.query)):
                return self._json({"fel": "fel lösenord"}, 401)
            try:
                body = self._body()
                if url.path == "/api/settings":
                    restart = app.store.update(body)
                    app.events.publish({"typ": "status", "text": "Inställningar sparade"})
                    return self._json({"ok": True, "kräver_omstart": restart})
                if url.path == "/api/pause":
                    app.store.update({"paused": bool(body.get("paused", True))})
                    app.events.publish({"typ": "status", "text": "Pausad" if body.get("paused", True) else "Återupptagen"})
                    return self._json({"ok": True})
                if url.path == "/api/meeting-now":
                    app.meeting_now.set()
                    return self._json({"ok": True})
            except ValueError as e:
                return self._json({"fel": str(e)}, 400)
            except Exception as e:
                return self._json({"fel": str(e)}, 500)
            self._json({"fel": "finns inte"}, 404)

    return Handler


def start_server(app, host: str, port: int, token: str = "") -> str:
    if host not in ("127.0.0.1", "localhost") and not token:
        raise SystemExit("Webbsidan nås från andra enheter – ange ett lösenord med --token eller DASHBOARD_TOKEN.")
    server = ThreadingHTTPServer((host, port), make_handler(app, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    return f"http://{shown}:{port}/" + (f"?token={token}" if token else "")
