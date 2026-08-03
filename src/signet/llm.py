"""OpenRouter client.

Small on purpose. signet needs one thing from a model: turn some text plus context into either
a short answer or a structured decision. That is one HTTP call.

Cost is tracked per call and enforced as a daily cap. Not for economics, since a day of heavy
use is a few cents, but as a runaway-loop breaker: a router that decides to call itself should
run out of budget, not run out of money.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("signet.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# deepseek/deepseek-v4-flash-0731, per docs/02-architecture.md. Prices are per million tokens.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
PRICE_IN_PER_M = 0.09
PRICE_OUT_PER_M = 0.18


class LLMUnavailable(RuntimeError):
    """No API key, the daily cap is spent, or the provider is unreachable.

    Callers must degrade rather than fail: signet without a model is still a recorder and a
    search engine, which is most of its value.
    """


@dataclass
class Completion:
    text: str
    data: dict[str, Any] | None
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


def estimate_cost(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * PRICE_IN_PER_M + tokens_out * PRICE_OUT_PER_M) / 1_000_000


class LLM:
    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 45.0,
        provider: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # OpenRouter's provider routing block: pin providers, set an order, allow or forbid
        # fallbacks. https://openrouter.ai/docs/features/provider-routing
        self.provider = provider or {}
        # Anything else that belongs in the request body, such as temperature or a
        # reasoning-effort block. Kept opaque so a new upstream knob needs no code change.
        self.params = params or {}

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 600,
        timeout: float | None = None,
    ) -> Completion:
        """One call. With `schema`, the model is forced into structured output and
        `Completion.data` carries the parsed object."""
        if not self.api_key:
            raise LLMUnavailable("no OPENROUTER_API_KEY set")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if self.provider:
            body["provider"] = self.provider
        if self.params:
            # Merged under the fields signet controls, so a stray "messages" or "model" in
            # the params blob cannot break the call.
            body = {**self.params, **body}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "plan", "strict": True, "schema": schema},
            }

        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.post(
                    OPENROUTER_URL,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        # OpenRouter uses these for attribution. Harmless, and it keeps the
                        # request identifiable in the dashboard.
                        "X-Title": "signet",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"openrouter request failed: {exc}") from exc

        choice = payload["choices"][0]["message"]["content"] or ""
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))

        data = None
        if schema is not None:
            try:
                data = json.loads(choice)
            except json.JSONDecodeError:
                logger.warning("structured output was not valid JSON: %s", choice[:200])

        return Completion(
            text=choice.strip(),
            data=data,
            model=payload.get("model", self.model),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(tokens_in, tokens_out),
        )
