"""
OpenAI-compatible AI client wrapper.

A deliberately thin transport boundary: it sends a system+user prompt and returns
raw text plus token usage. Prompt construction, JSON parsing, schema validation,
and guardrails all live in :mod:`report_generator`, so the model call is a single
seam the test-suite mocks (the suite never reaches the network — see
``config/settings/test.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


class AIClientError(Exception):
    """Raised on transport/timeout/empty-response failures talking to the model."""


@dataclass(frozen=True)
class AIResult:
    """Raw model output and accounting; parsing happens one layer up."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class AIClient:
    """Calls an OpenAI-compatible Chat Completions endpoint for JSON output."""

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or settings.PATCHPATH_AI
        self._client = None  # built lazily so importing never needs a key/network

    def _ensure_client(self):
        if self._client is None:
            # Imported lazily: keeps the dependency off the import path for code
            # (and tests) that never make a real call.
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._config.get("API_KEY") or "",
                base_url=self._config.get("BASE_URL") or None,
                timeout=self._config.get("TIMEOUT_SECONDS", 45),
                max_retries=0,  # retries are orchestrated in report_generator
            )
        return self._client

    def generate(self, system_prompt: str, user_prompt: str) -> AIResult:
        """Request a single JSON completion. Raises ``AIClientError`` on failure."""
        client = self._ensure_client()
        model = self._config.get("MODEL", "")
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=self._config.get("TEMPERATURE", 0.2),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001 — normalise any SDK/transport error
            raise AIClientError(str(exc)) from exc

        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise AIClientError("Model returned no choices.") from exc
        if not text.strip():
            raise AIClientError("Model returned an empty response.")

        usage = getattr(response, "usage", None)
        return AIResult(
            text=text,
            model=getattr(response, "model", model) or model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )
