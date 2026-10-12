"""Läser inställningar från miljövariabler och en lokal .env-fil."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    """Minimal .env-läsare: NYCKEL=värde per rad. Befintliga miljövariabler vinner."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def nordnet_configured() -> bool:
    return bool(env("NORDNET_API_KEY") and env("NORDNET_PRIVATE_KEY"))


def make_nordnet_client():
    from nordnet.client import NordnetClient

    return NordnetClient(
        api_key=env("NORDNET_API_KEY"),
        private_key_path=env("NORDNET_PRIVATE_KEY"),
        country=env("NORDNET_COUNTRY", "se"),
    )
