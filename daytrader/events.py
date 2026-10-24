"""Händelser i realtid (agentinlägg, röster, order) till webbsidan via Server-Sent Events."""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self.recent: deque[dict] = deque(maxlen=history)
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.ids = itertools.count(1)

    def publish(self, event: dict) -> None:
        event = {"id": next(self.ids), "tid": time.strftime("%H:%M:%S"), **event}
        with self.lock:
            self.recent.append(event)
            for q in list(self.subscribers):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


class JsonlLog:
    """Enkel append-logg på disk med de senaste raderna i minnet."""

    def __init__(self, path: Path, keep: int = 2000) -> None:
        self.path = path
        self.items: deque[dict] = deque(maxlen=keep)
        self.lock = threading.Lock()
        if path.exists():
            for line in path.read_text().splitlines()[-keep:]:
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    pass

    def append(self, item: dict) -> None:
        with self.lock:
            self.items.append(item)
            with self.path.open("a") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def last(self, n: int = 100) -> list[dict]:
        with self.lock:
            return list(self.items)[-n:]

    def find(self, key: str, value: str) -> Optional[dict]:
        with self.lock:
            return next((i for i in reversed(self.items) if i.get(key) == value), None)
