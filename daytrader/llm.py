"""AI-motorer för agentrådet.

    api          Anthropic API (kostar krediter)
    claude-code  Lokala `claude`-kommandot – räknas mot din Claude Pro/Max-prenumeration
    ollama       Lokal gratismodell via Ollama (https://ollama.com), inget nätverk behövs
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional

import requests

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """Plockar ut ett JSON-objekt även om modellen skrivit text eller ```-block runt det."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"Inget JSON-objekt i svaret: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def schema_instruction(schema: dict) -> str:
    return (
        "\n\nSvara ENDAST med ett JSON-objekt (ingen annan text) som följer detta JSON-schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )


class Backend:
    name = "?"

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        """Returnerar text, eller en JSON-sträng när schema anges."""
        raise NotImplementedError


class AnthropicBackend(Backend):
    name = "Anthropic API"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, effort: str = "low") -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        output_config: dict = {"effort": self.effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
            # Om modellen nekar körs samma förfrågan om på en reservmodell.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMError("modellen avböjde att svara")
        return "".join(b.text for b in response.content if b.type == "text").strip()


class ClaudeCodeBackend(Backend):
    """Anropar `claude -p` lokalt. Kräver Claude Code installerat och inloggat med din prenumeration."""

    name = "Claude Code (prenumeration)"

    def __init__(self, model: Optional[str] = None, timeout: int = 300) -> None:
        self.exe = shutil.which("claude")
        if not self.exe:
            raise LLMError("Hittar inte kommandot `claude`. Installera Claude Code och kör `claude` en gång för att logga in.")
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        cmd = [self.exe, "-p", "--output-format", "json", "--system-prompt", system,
               "--tools", "", "--no-session-persistence"]
        if self.model:
            cmd += ["--model", self.model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        # En satt API-nyckel skulle få claude att debitera krediter i stället för prenumerationen.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout, env=env)
        if proc.returncode != 0:
            raise LLMError(f"claude avslutades med fel {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
        result = json.loads(proc.stdout)
        if result.get("is_error"):
            raise LLMError(f"claude: {result.get('result')}")
        structured = result.get("structured_output")
        if schema and isinstance(structured, dict):
            return json.dumps(structured, ensure_ascii=False)
        if schema and isinstance(structured, str) and structured.strip():
            return structured
        return str(result.get("result", "")).strip()


class OllamaBackend(Backend):
    """Lokal modell via Ollama. Gratis, men svagare än Claude."""

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, url: str = "http://localhost:11434", timeout: int = 600) -> None:
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.name = f"Ollama ({model})"
        try:
            tags = requests.get(f"{self.url}/api/tags", timeout=5).json()
        except requests.RequestException:
            raise LLMError(f"Når inte Ollama på {self.url}. Starta Ollama-appen eller kör `ollama serve`.")
        installed = {m.get("name") for m in tags.get("models", [])}
        if model not in installed and f"{model}:latest" not in installed:
            raise LLMError(f"Modellen {model} är inte nedladdad. Kör: ollama pull {model}")

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        body: dict = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt + (schema_instruction(schema) if schema else "")},
            ],
            "options": {"temperature": 0.7},
        }
        if schema:
            body["format"] = schema  # Ollama begränsar svaret till schemat
        r = requests.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise LLMError(f"Ollama svarade {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"].strip()


def make_backend(kind: str, claude_model: str, effort: str, ollama_model: str, ollama_url: str) -> Backend:
    if kind == "api":
        return AnthropicBackend(claude_model, effort)
    if kind == "claude-code":
        return ClaudeCodeBackend(claude_model)
    if kind == "ollama":
        return OllamaBackend(ollama_model, ollama_url)
    raise ValueError(f"Okänd motor: {kind}")
