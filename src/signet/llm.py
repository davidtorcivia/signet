"""OpenRouter client.

Small on purpose. signet needs one thing from a model: turn some text plus context into either
a short answer or a structured decision. That is one HTTP call.

Cost is tracked per call and enforced as a daily cap. Not for economics, since a day of heavy
use is a few cents, but as a runaway-loop breaker: a router that decides to call itself should
run out of budget, not run out of money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("signet.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# deepseek/deepseek-v4-flash-0731, per docs/02-architecture.md. Prices are per million tokens.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
PRICE_IN_PER_M = 0.09
PRICE_OUT_PER_M = 0.18

# Room for a reasoning model to think and still answer. Reasoning is billed as output, so the
# whole ceiling is worth about $0.0004 and buys correctness on date arithmetic.
SCHEMA_TOKEN_BUDGET = 2000

# Providers go busy. Pinned with fallbacks off there is nowhere else to go, so one quick retry
# turns a transient 503 into a slower answer instead of a lost request.
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


def _looks_like_unsupported(message: str) -> bool:
    """Whether the failure is the endpoint refusing response_format rather than a real fault.

    Pinning a provider that cannot do structured output removes every eligible endpoint, and
    OpenRouter reports that as "No endpoints found" with a 404, which reads like the model
    does not exist.
    """
    lowered = message.lower()
    return any(
        hint in lowered
        for hint in ("no endpoints found", "response_format", "json_schema", "structured")
    )


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


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def loads_lenient(text: str) -> dict[str, Any] | None:
    """Get an object out of whatever the model actually said.

    Structured output is a provider capability, not a model one, and pinning a provider that
    lacks it makes OpenRouter answer "No endpoints found" rather than degrading. Since signet
    only ever wants a small flat object, parsing tolerantly is more robust than demanding a
    guarantee the endpoint may not offer: fenced code blocks, a preamble before the brace, or
    trailing commentary all still yield the object.
    """
    if not text:
        return None

    candidates = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    # The outermost balanced object, so nested braces survive.
    start = text.find("{")
    if start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def describe(schema: dict[str, Any]) -> str:
    """A plain-words version of the schema, for models asked without response_format."""
    fields = []
    for name, spec in (schema.get("properties") or {}).items():
        kind = spec.get("type", "string")
        kind = "/".join(kind) if isinstance(kind, list) else kind
        note = spec.get("description")
        fields.append(f'  "{name}": {kind}{" - " + note if note else ""}')
    body = "\n".join(fields)
    return f"Reply with one JSON object and nothing else. No prose, no code fence.\nFields:\n{body}"


class LLM:
    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 45.0,
        provider: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        plain_json: bool = False,
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
        # Set after an endpoint rejects response_format, and settable directly for a provider
        # known not to support it. Asking in words costs a little accuracy and works
        # everywhere.
        self.plain_json = plain_json

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
        """One call. With `schema`, `Completion.data` carries the parsed object.

        Tries real structured output first and falls back to asking in words if the endpoint
        cannot do it, so pinning a provider without that capability degrades quality slightly
        instead of failing outright.
        """
        if schema is not None and not self.plain_json:
            try:
                return await self._request(system, user, schema, max_tokens, timeout)
            except LLMUnavailable as exc:
                if not _looks_like_unsupported(str(exc)):
                    raise
                logger.info("endpoint cannot do structured output, asking in words instead")
                self.plain_json = True
        return await self._request(system, user, schema, max_tokens, timeout)

    async def _request(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        max_tokens: int,
        timeout: float | None,
    ) -> Completion:
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
        if schema is not None:
            # Reasoning tokens come out of the same budget as the answer, and a 300 token
            # ceiling meant the model spent all 300 thinking and returned content of None with
            # finish_reason "length". The fix is headroom, not less thinking: resolving "the
            # 21st, 22nd and 24th" against today's date is exactly the arithmetic that
            # benefits from it, and reasoning bills as output, so this ceiling is worth about
            # four hundredths of a cent. Set `reasoning` in model params to control it.
            body["max_tokens"] = max(body["max_tokens"], SCHEMA_TOKEN_BUDGET)

        if self.provider:
            body["provider"] = self.provider
        if self.params:
            # Merged under the fields signet controls, so a stray "messages" or "model" in
            # the params blob cannot break the call.
            body = {**self.params, **body}
        if schema is not None and not self.plain_json:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "plan", "strict": True, "schema": schema},
            }
        elif schema is not None:
            # No response_format at all: the endpoint may not support it, so the schema is
            # described in words and the reply parsed tolerantly.
            body["messages"][0]["content"] += "\n\n" + describe(schema)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution. Harmless, and it keeps the request
            # identifiable in the dashboard.
            "X-Title": "signet",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.post(OPENROUTER_URL, json=body, headers=headers)
                if response.status_code in RETRY_STATUSES:
                    logger.info("openrouter %s, retrying once", response.status_code)
                    await asyncio.sleep(1.5)
                    response = await client.post(OPENROUTER_URL, json=body, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            # The body carries the actual reason, and it is often actionable: pinning a
            # provider that cannot do structured output produces "No endpoints found", which
            # says nothing useful without this.
            raise LLMUnavailable(
                f"openrouter returned {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"openrouter request failed: {exc}") from exc

        message = payload["choices"][0]["message"]
        choice = message.get("content") or ""
        if not choice and schema is not None:
            # Some endpoints put everything in `reasoning` when the content budget runs out.
            # The object is often in there, so it is worth a look before giving up.
            choice = message.get("reasoning") or ""
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))

        data = None
        if schema is not None:
            data = loads_lenient(choice)
            if data is None:
                logger.warning("could not parse an object from the reply: %s", choice[:200])

        return Completion(
            text=choice.strip(),
            data=data,
            model=payload.get("model", self.model),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(tokens_in, tokens_out),
        )
