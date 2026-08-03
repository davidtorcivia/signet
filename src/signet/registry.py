"""Capability discovery and the one place enforcement happens.

Handlers do the work. Everything that must never be forgotten (scopes, rate limits, the kill
switch, approval for destructive things, audit rows) lives here instead, so forgetting it in a
capability is not possible.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sqlite3
import time
from collections import defaultdict, deque

from pydantic import ValidationError

from . import coreschema, db
from .capability import Capability
from .envelope import Outcome, Request

logger = logging.getLogger("signet.registry")

DEFAULT_PACKAGE = "signet.capabilities"


class Registry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._calls: dict[int | None, deque[float]] = defaultdict(deque)

    # --- registration ---------------------------------------------------------------

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"duplicate capability: {capability.name}")
        self._capabilities[capability.name] = capability

    def discover(self, package: str = DEFAULT_PACKAGE) -> list[str]:
        """Import every submodule of `package` and collect its CAPABILITIES list.

        This is the seam. Dropping a directory in `capabilities/` is the entire integration
        step; nothing here needs to learn the new name.
        """
        module = importlib.import_module(package)
        found: list[str] = []
        for info in pkgutil.iter_modules(module.__path__):
            submodule = importlib.import_module(f"{package}.{info.name}")
            for capability in getattr(submodule, "CAPABILITIES", []):
                self.register(capability)
                found.append(capability.name)
        return found

    # --- lookup ---------------------------------------------------------------------

    def all(self) -> list[Capability]:
        return sorted(self._capabilities.values(), key=lambda c: c.name)

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def visible_to(self, scopes: frozenset[str]) -> list[Capability]:
        """What this caller may see in tools/list."""
        return [c for c in self.all() if c.mcp_visible and c.permitted_for(scopes)]

    # --- enforcement ----------------------------------------------------------------

    def _rate_limited(self, request: Request) -> bool:
        limit = request.client.rate_limit_per_min if request.client else 0
        if limit <= 0:
            return False
        window = self._calls[request.client_id]
        cutoff = time.monotonic() - 60
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(time.monotonic())
        return False

    async def invoke(
        self,
        conn: sqlite3.Connection,
        request: Request,
        name: str,
        arguments: dict,
    ) -> Outcome:
        started = time.monotonic()
        capability = self.get(name)

        if capability is None:
            return _fail(f"signet has no capability called {name}.", recoverable=False)

        # The kill switch is deliberately checked before scopes: when it is on, the only thing
        # that still works is writing to the journal, so a capture is never lost.
        if db.kill_switch_on(conn) and not name.startswith("journal."):
            logger.warning("kill switch blocked %s", name)
            return _fail("signet is paused. Only capture is working.", recoverable=False)

        if not capability.permitted_for(request.scopes):
            missing = sorted(set(capability.scopes) - request.scopes)
            logger.warning("scope denied %s missing=%s", name, missing)
            return _fail("Not allowed.", recoverable=False)

        if self._rate_limited(request):
            logger.warning("rate limited client=%s", request.client_id)
            return _fail("Too many requests, try again in a minute.", recoverable=True)

        try:
            args = capability.schema.model_validate(arguments)
        except ValidationError as exc:
            logger.info("bad arguments for %s: %s", name, exc)
            # Recoverable: the on-device model can reasonably try again with better arguments.
            return _fail("I did not understand that request.", recoverable=True)

        if capability.destructive:
            # P2 turns this into a real approval queue with an ntfy action button. Until then
            # refusing is the honest behaviour: the ring cannot answer a confirmation prompt,
            # so silently running it would be the dangerous option.
            logger.warning("destructive capability %s refused (approval queue is P2)", name)
            return _fail("That needs approval, which is not built yet.", recoverable=False)

        try:
            outcome = await capability.handler(request, args)
        except Exception:
            logger.exception("capability %s failed", name)
            return _fail("That did not work.", recoverable=False)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("%s ok in %dms", name, elapsed_ms)
        return outcome


def _fail(message: str, *, recoverable: bool) -> Outcome:
    return Outcome(
        output=message,
        semantic=coreschema.generic_failure(message, llm_recoverable=recoverable),
        is_error=True,
    )


_registry: Registry | None = None


def get_registry() -> Registry:
    """Process-wide registry, built once."""
    global _registry
    if _registry is None:
        registry = Registry()
        registry.discover()
        _registry = registry
    return _registry
