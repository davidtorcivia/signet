"""OpenRouter's model and provider catalogue.

Used to populate the settings pickers, so choosing a model is a list rather than a remembered
string, and provider routing is a set of real providers rather than hand-written JSON.

Both endpoints are public, so the pickers work before an API key is set. Results are cached in
the settings table because the list is 300-odd models and the settings page should not wait on
a network call, nor break when the box is offline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

import httpx

from . import db

logger = logging.getLogger("signet.openrouter")

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_KEY = "cache:openrouter_models"
CACHE_AT_KEY = "cache:openrouter_models_at"
PROVIDER_CACHE_PREFIX = "cache:openrouter_providers:"
TTL_SECONDS = 24 * 60 * 60


@dataclass
class Model:
    id: str
    name: str
    context_length: int
    prompt_price: float
    completion_price: float
    structured: bool

    @property
    def label(self) -> str:
        """What the picker shows: name, price per million tokens, context window."""
        context = f"{self.context_length // 1000}k" if self.context_length else "unknown context"
        return (
            f"{self.name} - ${self.prompt_price * 1_000_000:.2f}/"
            f"${self.completion_price * 1_000_000:.2f} per M, {context}"
        )


def _price(value: str | float | None) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_models(payload: dict) -> list[Model]:
    models = []
    for item in payload.get("data", []):
        pricing = item.get("pricing") or {}
        supported = item.get("supported_parameters") or []
        models.append(
            Model(
                id=item.get("id", ""),
                name=item.get("name") or item.get("id", ""),
                context_length=int(item.get("context_length") or 0),
                prompt_price=_price(pricing.get("prompt")),
                completion_price=_price(pricing.get("completion")),
                # signet's router and scheduler need json_schema output. A model without it
                # still answers questions, so these are sorted first rather than hidden.
                structured="structured_outputs" in supported,
            )
        )
    return [m for m in models if m.id]


async def fetch_models(conn: sqlite3.Connection, *, force: bool = False) -> list[Model]:
    cached = db.get_setting(conn, CACHE_KEY, "")
    fetched_at = db.get_setting(conn, CACHE_AT_KEY, "0")
    try:
        fresh = cached and (time.time() - float(fetched_at)) < TTL_SECONDS
    except ValueError:
        fresh = False

    if cached and fresh and not force:
        return parse_models(json.loads(cached))

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(MODELS_URL)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("could not refresh the model list: %s", exc)
        # Stale beats empty: a settings page with last week's list still works.
        return parse_models(json.loads(cached)) if cached else []

    db.set_setting(conn, CACHE_KEY, json.dumps(payload))
    db.set_setting(conn, CACHE_AT_KEY, str(time.time()))
    return parse_models(payload)


def _normalise(entries) -> list[dict]:
    """Tolerate the older cache format, which was a plain list of provider names."""
    out = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append({"name": entry, "structured": True})
        elif isinstance(entry, dict) and entry.get("name"):
            out.append({"name": entry["name"], "structured": bool(entry.get("structured"))})
    return out


async def fetch_providers(
    conn: sqlite3.Connection, model_id: str, *, force: bool = False
) -> list[dict]:
    """Providers that serve this model, and whether each can do structured output.

    That flag is load-bearing. signet's router and scheduler ask for a JSON schema, and a
    provider that cannot honour it is simply not an eligible endpoint. Pin only such a
    provider with fallbacks off and OpenRouter answers "No endpoints found", so questions
    still work while scheduling silently falls back to the journal.
    """
    if not model_id:
        return []
    key = PROVIDER_CACHE_PREFIX + model_id
    cached = db.get_setting(conn, key, "")
    if cached and not force:
        try:
            return _normalise(json.loads(cached))
        except json.JSONDecodeError:
            pass

    url = f"{MODELS_URL}/{model_id}/endpoints"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("could not list providers for %s: %s", model_id, exc)
        return _normalise(json.loads(cached)) if cached else []

    found: list[dict] = []
    seen: set[str] = set()
    for endpoint in (payload.get("data") or {}).get("endpoints", []):
        name = endpoint.get("provider_name")
        if not name or name in seen:
            continue
        seen.add(name)
        supported = endpoint.get("supported_parameters") or []
        found.append({"name": name, "structured": "structured_outputs" in supported})

    db.set_setting(conn, key, json.dumps(found))
    return found


def build_provider_config(order: list[str], allow_fallbacks: bool) -> dict:
    """Turn the picker's state into OpenRouter's provider routing block.

    An empty order means no preference at all, which is a different thing from an order with
    fallbacks disabled, so it returns an empty dict rather than a block that pins nothing.
    """
    config: dict = {}
    if order:
        config["order"] = order
    if not allow_fallbacks:
        config["allow_fallbacks"] = False
    return config


def read_provider_config(raw: str | None) -> tuple[list[str], bool]:
    """Inverse of build_provider_config, for rendering the form."""
    if not raw:
        return [], True
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [], True
    if not isinstance(parsed, dict):
        return [], True
    order = [str(x) for x in parsed.get("order", []) if x]
    return order, bool(parsed.get("allow_fallbacks", True))
