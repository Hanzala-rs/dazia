"""Local model adapter (Ollama).

Kept separate from orchestration so the model/provider can change without touching assistant.py.
"""
import json
import logging
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 3.0
MAX_ATTEMPTS = 2  # first try + at most one retry on invalid JSON


class LLMError(Exception):
    """Model returned something unusable (bad JSON, timeout, etc.)."""


class LLMUnavailable(LLMError):
    """Ollama is not reachable or the model cannot be loaded."""


class LLMClient(Protocol):
    def complete_json(self, messages: list[dict], schema: dict) -> dict:
        """Chat completion constrained to the JSON `schema`; returns the parsed object."""
        ...


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ):
        """`transport` is optional and only meant for tests (e.g. httpx.MockTransport)."""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        # One reusable client (connection pooling); created once, not per request.
        self._http = httpx.Client(base_url=self.base_url, transport=transport)

    def _unavailable(self, reason: str) -> LLMUnavailable:
        return LLMUnavailable(
            f"{reason} Make sure Ollama is running (start the Ollama app or run `ollama serve`) "
            f"and the model is installed (`ollama pull {self.model}`)."
        )

    def complete_json(self, messages: list[dict], schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "think": False,  # thinking models (e.g. qwen3.5) are far slower with reasoning on
            "options": {"temperature": 0, "num_ctx": 2048},
        }
        last_error = "no attempt made"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            content = self._chat(payload)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON from model: {exc.msg}"
                log.warning("LLM attempt %d returned invalid JSON", attempt)
                continue
            if not isinstance(parsed, dict):
                last_error = "model returned JSON that is not an object"
                log.warning("LLM attempt %d returned non-object JSON", attempt)
                continue
            return parsed
        raise LLMError(f"The model gave an unusable answer ({last_error}).")

    def _chat(self, payload: dict) -> str:
        """POST /api/chat and return message.content. Maps transport/HTTP problems to LLM errors."""
        try:
            resp = self._http.post("/api/chat", json=payload, timeout=self.timeout_s)
        except httpx.ConnectError as exc:
            raise self._unavailable(f"Cannot connect to Ollama at {self.base_url}.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"The model did not answer within {self.timeout_s:g} seconds.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Error talking to Ollama: {exc.__class__.__name__}.") from exc

        if resp.status_code != 200:
            detail = _error_text(resp)
            if resp.status_code == 404 or "not found" in detail.lower():
                raise self._unavailable(f"Model '{self.model}' is not available in Ollama.")
            raise LLMError(f"Ollama returned HTTP {resp.status_code}: {detail[:200]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMError("Ollama returned a response that is not JSON.") from exc
        content = (body.get("message") or {}).get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise LLMError("Ollama response is missing message.content.")
        return content

    def health(self) -> tuple[bool, str]:
        """(ok, detail) — reachable and the configured model is installed. Never raises."""
        try:
            resp = self._http.get("/api/tags", timeout=HEALTH_TIMEOUT_S)
            resp.raise_for_status()
            models = resp.json().get("models", [])
            names = {m.get("name") for m in models} | {m.get("model") for m in models}
        except Exception as exc:  # health checks must never raise
            return False, f"Ollama not reachable at {self.base_url} ({exc.__class__.__name__}). Start Ollama."
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"Ollama is up and model '{self.model}' is installed."
        return False, f"Ollama is up but model '{self.model}' is missing. Run `ollama pull {self.model}`."


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "error" in body:
            return str(body["error"])
    except ValueError:
        pass
    return resp.text
