"""Capability discovery and the one place enforcement happens.

Handlers do the work. Everything that must never be forgotten (scopes, rate limits, the kill
switch, approval for destructive things, audit rows) lives here instead, so forgetting it in a
capability is not possible.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import sqlite3
import time
from collections import defaultdict, deque

from pydantic import ValidationError

from . import coreschema, db
from .auth import Principal
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

    def unregister(self, name: str) -> None:
        self._capabilities.pop(name, None)

    def drop_upstreams(self) -> list[str]:
        """Remove everything mounted from a remote server, leaving built-ins alone."""
        names = [c.name for c in self.all() if c.metadata.get("upstream")]
        for name in names:
            self.unregister(name)
        return names

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
        *,
        approved: bool = False,
    ) -> Outcome:
        """`approved` is set only when running a job the user has just tapped to allow. Every
        other check still applies, so approving cannot widen what the original request could
        do."""
        started = time.monotonic()
        capability = self.get(name)

        if capability is None:
            return _fail(f"signet has no capability called {name}.", recoverable=False)

        # The kill switch is deliberately checked before scopes: when it is on, the only thing
        # that still works is writing to the journal (and todos), so a capture is never lost.
        if db.kill_switch_on(conn) and not name.startswith(("journal.", "todos.")):
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

        if capability.destructive and not approved:
            # The ring cannot answer "are you sure": no session, no elicitation, and it is
            # usually in a pocket. So this parks the call with the arguments already
            # validated, and it runs on one deliberate tap afterwards.
            title = describe_call(capability, args)
            job_id = db.queue_approval(
                conn,
                capability=name,
                args=args.model_dump(mode="json"),
                title=title,
                request_id=request.id or None,
                scopes=sorted(request.scopes),
            )
            logger.info("queued %s for approval as %s", name, job_id)
            return Outcome(
                output=f"Waiting for your approval: {title}",
                semantic=coreschema.action_logged(
                    "signet", f"Needs your approval: {title}", success=False
                ),
                data={"job_id": job_id, "awaiting_approval": True},
            )

        try:
            outcome = await capability.handler(request, args)
        except Exception:
            logger.exception("capability %s failed", name)
            return _fail("That did not work.", recoverable=False)

        # Set here rather than trusting the handler to remember. A capability declared as
        # pulling in outside text always produces an untrusted outcome.
        if capability.returns_untrusted:
            outcome.untrusted = True

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("%s ok in %dms", name, elapsed_ms)
        return outcome


def describe_call(capability: Capability, args) -> str:
    """A short human sentence for the approval prompt.

    What the user is being asked to allow has to be legible at a glance. "home.unlock" is not
    a decision anyone can make; "Unlock the front door" is.
    """
    fields = args.model_dump(mode="json")
    interesting = [
        str(value)
        for key, value in fields.items()
        if isinstance(value, str | int | float) and str(value).strip() and key != "kind"
    ]
    subject = ", ".join(interesting[:2])
    action = capability.description.rstrip(".").split(".")[0]
    return f"{action}: {subject}" if subject else action


def _fail(message: str, *, recoverable: bool) -> Outcome:
    return Outcome(
        output=message,
        semantic=coreschema.generic_failure(message, llm_recoverable=recoverable),
        is_error=True,
    )


_registry: Registry | None = None


def get_registry() -> Registry:
    """The one registry, shared by the MCP surface and the portal.

    Two of them was a real bug rather than a tidiness point: approving a queued upstream job
    in the portal looked up a capability that only the server's copy had mounted, so the tap
    failed with "no such capability".
    """
    global _registry
    if _registry is None:
        registry = Registry()
        registry.discover()
        _registry = registry
    return _registry


def reload_upstreams(conn: sqlite3.Connection) -> list[str]:
    """Remount every server's tools. Called after any change, so editing what a tool is
    allowed to do takes effect on the next request rather than the next restart."""
    from . import upstream

    registry = get_registry()
    registry.drop_upstreams()
    return upstream.mount(registry, conn)


async def run_approved(
    registry: Registry, conn: sqlite3.Connection, job_id: str, by: str = "portal"
) -> Outcome:
    """Carry out a job the user has approved.

    The scopes stored with the job are used rather than the approver's, so a tap authorises
    exactly the request that was made and nothing wider. Decided first and once, so a double
    tap or a replayed link cannot run it twice.
    """
    job = db.get_job(conn, job_id)
    if job is None:
        return _fail("That approval no longer exists.", recoverable=False)
    if job["status"] != db.AWAITING:
        return _fail("That was already dealt with.", recoverable=False)
    if job["expires_at"] and job["expires_at"] <= db.now_iso():
        db.decide_job(conn, job_id, "expired", by)
        return _fail("That approval expired. Ask again if you still want it.", recoverable=False)

    if not db.decide_job(conn, job_id, "running", by):
        return _fail("That was already dealt with.", recoverable=False)

    payload = json.loads(job["payload_json"] or "{}")
    request = Request(
        text=job["title"] or "",
        source="approval",
        client=Principal(client_id=by, scopes=frozenset(payload.get("scopes") or [])),
    )
    outcome = await registry.invoke(
        conn, request, job["capability"], payload.get("args") or {}, approved=True
    )
    db.finish_job(
        conn,
        job_id,
        "failed" if outcome.is_error else "done",
        {"output": outcome.output, "semantic": outcome.semantic},
    )
    return outcome
