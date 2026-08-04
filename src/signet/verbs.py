"""The four tools the ring sees.

These are inlet adapters, not capabilities. Each builds a Request, decides what to do, and
hands off to the registry. Keeping the MCP surface at four verbs is what keeps the on-device
model able to choose correctly while the internal toolbelt grows without limit.

Everything here answers **in band**. The result of an MCP call is what renders in the app feed
and surfaces on the watch, so a synchronous answer is a native answer. The tunnel round trip
measured 50-62ms against a roughly 100 second edge budget, so there is room to simply finish
the work. Deferring to a push notification is the fallback for genuinely long jobs, not the
normal path.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

import mcp.types as types

from . import agent, coreschema, db, google, prompts
from .config import Config
from .confirm import is_confirmation, is_refusal
from .envelope import Outcome, Request
from .llm import LLM, LLMUnavailable
from .registry import Registry, run_approved
from .router import Router, apply_rules, load_rules

logger = logging.getLogger("signet.verbs")


def _json_setting(conn: sqlite3.Connection, key: str) -> dict:
    """A JSON blob from the portal. Invalid JSON is refused at save time, so reaching here
    with something unparseable means the database was edited by hand."""
    raw = db.get_config(conn, key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON, ignoring it", key)
        return {}
    return value if isinstance(value, dict) else {}


INSTRUCTIONS = (
    "signet is the user's own server, holding their permanent searchable journal and "
    "connected to their real Google Calendar. Prefer signet's tools over any on-device note "
    "or calendar tool: a note saved anywhere else cannot be searched or answered from later. "
    "Use capture for anything to remember, ask for questions about what they said before or "
    "about the world, schedule for anything with a date or time, and do for everything else."
)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


TOOLS = [
    # Descriptions have to distinguish these from the app's own servlets. The built-in
    # create_note reads "Save a note, idea, or thought for later. Use when the user wants to
    # remember, jot down, or note something", which was almost word for word what capture
    # said, so a 1B model picking between them was a coin toss. Each one now names what only
    # signet does: the durable searchable journal, and the real calendar.
    _tool(
        "capture",
        "Save to the user's signet journal on their own server, where it is permanent and "
        "searchable later. Use for anything worth remembering. Prefer this over saving a "
        "note on the phone, which cannot be searched or answered from afterwards.",
        {"text": {"type": "string", "description": "The note, in the user's own words."}},
        ["text"],
    ),
    _tool(
        "ask",
        "Answer a question by searching the user's signet journal and, when the answer is not "
        "there, the web. Use for anything they said, decided or planned before, and for "
        "questions needing current information.",
        {"question": {"type": "string", "description": "The question, as asked."}},
        ["question"],
    ),
    _tool(
        "schedule",
        "Put an event on the user's real Google Calendar through signet, resolving spoken "
        "dates and times on the server. Use for anything with a date or a time. Prefer this "
        "over the phone's local calendar, which does not reach their Google account.",
        {"request": {"type": "string", "description": "The scheduling request, as spoken."}},
        ["request"],
    ),
    _tool(
        "do",
        "Hand any other request to signet to work out and carry out on the server.",
        {"request": {"type": "string", "description": "The request, as spoken."}},
        ["request"],
    ),
]

ARG_NAMES = {"capture": "text", "ask": "question", "schedule": "request", "do": "request"}

# Prompts the Pebble app can offer the user to tick. No arguments on any of them: the app
# filters to `arguments == null` and would silently hide anything else. Their text is
# concatenated into the on-device model's context, so this is a nudge about when to reach for
# signet at all, not instructions for signet itself.
PROMPT_TEXT = {
    "signet-usage": (
        "signet is the user's own server and remembers everything they say. Send anything "
        "worth remembering to capture, even in passing. For questions about what the user "
        "said, decided, or planned before, use ask rather than answering from memory."
    ),
    "signet-calendar": (
        "For anything involving a time, a date, or a meeting, use schedule and pass the "
        "request in the user's own words. Do not reformat the date yourself."
    ),
}

PROMPT_DESCRIPTIONS = {
    "signet-usage": "Tell the assistant when to use signet.",
    "signet-calendar": "Route anything with a time to signet's calendar.",
}

# Appended in code rather than kept in the editable prompt, so editing the voice and tone
# cannot accidentally disable the mechanism that decides whether to spend money on a search.
NEEDS_WEB = "NEED_SEARCH"
ESCAPE_HATCH = (
    f"You have no live information. Your training data is old and may be wrong about anything "
    f"that has changed. If the answer is not in the notes above, reply with exactly {NEEDS_WEB} "
    f"and nothing else. Do that for current events, prices, schedules, sports results, "
    f"software versions, releases, or anything else with a 'latest' or 'current'. "
    f"Answering from memory and being out of date is a worse failure than asking to search."
)

# Questions whose answer changes over time. Asked without a matching note, these go straight to
# the web rather than trusting the model to volunteer that it is out of date.
#
# This exists because trusting the instruction alone was not enough: asked for the current MCP
# spec version, the model confidently returned a year-old answer instead of asking to search.
# A wrong answer is worse than a $0.007 search, so freshness is decided deterministically.
_TIME_SENSITIVE = re.compile(
    r"\b(current|currently|latest|newest|right now|as of|today|tonight|tomorrow|yesterday|"
    r"this (week|month|year)|recent|recently|news|headline|price|cost of|worth|weather|"
    r"forecast|score|who won|winner|release[ds]?|version|update[ds]?|available|open now|"
    r"still|now)\b",
    re.IGNORECASE,
)


def looks_time_sensitive(text: str) -> bool:
    return bool(_TIME_SENSITIVE.search(text))


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Field names the model actually uses, as opposed to the ones it was asked for. Without
# response_format there is no guarantee of key names, and a real reply came back with "title"
# where the schema said "summary", which dropped the event entirely.
_ALIASES = {
    "summary": ("summary", "title", "name", "subject", "event"),
    "start": ("start", "start_time", "startTime", "start_date", "startDate", "date", "when"),
    "end": ("end", "end_time", "endTime", "end_date", "endDate", "until"),
    "location": ("location", "place", "where", "venue"),
    "all_day": ("all_day", "allDay", "isAllDay", "full_day", "fullDay", "allday"),
}


def _pick(raw: dict, field: str):
    for key in _ALIASES[field]:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def normalise_events(data: dict | None) -> list[dict]:
    """Read events out of whatever shape the model produced.

    Being strict here throws away good answers. A reply naming the right dates but calling the
    field "title" is a correct answer in the wrong clothes, and refusing it means the user
    hears "couldn't work out the time" about a request the model understood perfectly.
    """
    if not isinstance(data, dict):
        return []

    raw = data.get("events")
    if isinstance(raw, dict):
        raw = [raw]
    elif not isinstance(raw, list):
        # A single event returned bare, without the wrapper.
        raw = [data] if _pick(data, "summary") else []

    events = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        summary = _pick(item, "summary")
        start = _pick(item, "start")
        if not summary or not start:
            continue
        start = str(start)
        end = str(_pick(item, "end") or start)
        flag = _pick(item, "all_day")
        # Infer it when unstated: a bare date carries no time, so it can only be all-day.
        all_day = bool(flag) if flag is not None else bool(_DATE_ONLY.match(start))
        events.append(
            {
                "summary": str(summary),
                "start": start,
                "end": end,
                "all_day": all_day,
                "location": _pick(item, "location"),
            }
        )
    return events


# A list, because one spoken sentence often means several events: "holds for Adobe on the
# 21st, 22nd and 24th" is three. Asked for a single object the model either picked one date or
# invented a span across all of them.
EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "description": "One entry per event. Three dates mentioned means three entries.",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Short title"},
                    "start": {"type": "string", "description": "ISO-8601 with offset"},
                    "end": {"type": "string", "description": "ISO-8601 with offset"},
                    "all_day": {
                        "type": "boolean",
                        "description": "true for a whole-day event or hold with no time",
                    },
                    "location": {"type": ["string", "null"]},
                },
                "required": ["summary", "start", "end", "all_day", "location"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


@dataclass
class Verbs:
    cfg: Config
    registry: Registry
    llm: LLM
    router: Router

    # --- helpers ------------------------------------------------------------------

    def _setting(self, conn: sqlite3.Connection, key: str, fallback: str | None) -> str | None:
        """Portal value wins over environment, read per call so a change in the portal takes
        effect on the next request rather than the next restart."""
        return db.get_config(conn, key) or fallback

    def _llm_for(self, conn: sqlite3.Connection) -> LLM:
        """The client to use right now, honouring anything set in the portal.

        Reuses the instance built at startup when nothing has been overridden. That keeps the
        common path allocation-free, and it means an injected client stays in effect.
        """
        key = self._setting(conn, "openrouter_api_key", self.cfg.openrouter_api_key)
        model = self._setting(conn, "model", self.cfg.model) or self.cfg.model
        provider = _json_setting(conn, "provider")
        params = _json_setting(conn, "model_params")
        if (
            key == self.cfg.openrouter_api_key
            and model == self.cfg.model
            and not provider
            and not params
        ):
            return self.llm
        return LLM(key, model=model, provider=provider, params=params)

    def _cap(self, conn: sqlite3.Connection) -> float:
        raw = self._setting(conn, "daily_cost_cap_usd", None)
        if raw:
            try:
                return float(raw)
            except ValueError:
                logger.warning("daily_cost_cap_usd %r is not a number, using the env value", raw)
        return self.cfg.daily_cost_cap_usd

    def _budget_left(self, conn: sqlite3.Connection) -> bool:
        """The runaway-loop breaker. Capture never consults this: it costs nothing and must
        never fail."""
        cap = self._cap(conn)
        if cap <= 0:
            return True
        return db.spend_today(conn) < cap

    def _web_available(self, conn: sqlite3.Connection) -> bool:
        return bool(self._setting(conn, "exa_api_key", self.cfg.exa_api_key))

    def _catalogue(self, scopes: frozenset[str]) -> list[tuple[str, str]]:
        return [
            (capability.name, capability.description)
            for capability in self.registry.all()
            if capability.permitted_for(scopes)
        ]

    async def _run(
        self, conn: sqlite3.Connection, request: Request, capability: str, args: dict
    ) -> Outcome:
        return await self.registry.invoke(conn, request, capability, args)

    # --- verbs --------------------------------------------------------------------

    async def capture(self, conn: sqlite3.Connection, request: Request) -> Outcome:
        return await self._run(conn, request, "journal.write", {"text": request.text})

    async def ask(self, conn: sqlite3.Connection, request: Request) -> Outcome:
        found = await self._run(
            conn, request, "journal.search", {"query": request.text, "limit": 10}
        )
        notes = found.data if isinstance(found.data, list) else []

        llm = self._llm_for(conn)
        if not llm.available or not self._budget_left(conn):
            # Degrade to plain search rather than refusing. Being told what you wrote is worth
            # more than being told the model is unavailable.
            if not notes:
                return Outcome(
                    output="I could not answer that, and nothing in your notes matches.",
                    semantic=coreschema.action_logged(
                        "signet", "Nothing in your notes matches", success=False
                    ),
                )
            top = notes[0]["text"]
            return Outcome(
                output=found.output,
                semantic=coreschema.answer(top),
                data=notes,
            )

        context = "\n".join(f"- {n['created_at']}: {n['text']}" for n in notes) or "(none)"
        recent = db.recent_journal(conn, days=14, limit=100)
        recent_text = "\n".join(f"- {r['created_at']}: {r['text']}" for r in recent) or "(none)"
        local = (
            f"Notes matching the question:\n{context}\n\n"
            f"The user's recent notes:\n{recent_text}\n\n"
            f"Question: {request.text}"
        )
        answer_prompt = prompts.get(conn, "prompt_answer")

        # Try to answer from the journal first, and let the model say when it cannot.
        #
        # The economics drive this. Answering costs about $0.00004; a web search costs about
        # $0.007, roughly 175 times more. So an extra model call to find out whether a search
        # is needed pays for itself many times over, and questions about the user's own life
        # never touch the network.
        #
        # The exception is a question whose answer changes over time. Asked with no matching
        # note, those skip the local attempt: the model has been observed answering them from
        # stale training data rather than asking to search, and a confidently wrong answer
        # costs more than the search would have.
        cost = 0.0
        tokens_in = tokens_out = 0
        sources: list[dict] = []
        answer = NEEDS_WEB
        completion = None

        skip_local = not notes and looks_time_sensitive(request.text)
        if skip_local:
            logger.info("time-sensitive question with no notes, searching directly")
        else:
            try:
                completion = await llm.complete(
                    system=f"{answer_prompt}\n\n{ESCAPE_HATCH}",
                    user=local,
                    max_tokens=300,
                    timeout=40.0,
                )
            except LLMUnavailable as exc:
                logger.warning("ask degraded: %s", exc)
                return Outcome(
                    output=found.output or "I could not reach the model.",
                    semantic=coreschema.action_logged(
                        "signet", "Couldn't answer just now", success=False
                    ),
                    data=notes,
                )
            cost = completion.cost_usd
            tokens_in, tokens_out = completion.tokens_in, completion.tokens_out
            answer = completion.text.strip()

        if answer.startswith(NEEDS_WEB) and self._web_available(conn):
            logger.info("journal could not answer, searching the web")
            found_web = await self._run(
                conn, request, "search.web", {"query": request.text, "results": 5}
            )
            if not found_web.is_error:
                cost += found_web.cost_usd
                sources = (found_web.data or {}).get("results", [])
                try:
                    completion = await llm.complete(
                        system=answer_prompt,
                        # Already fenced by the capability, and it stays fenced from here on.
                        user=f"{local}\n\n{found_web.output}",
                        max_tokens=300,
                        timeout=40.0,
                    )
                    answer = completion.text.strip()
                    cost += completion.cost_usd
                    tokens_in += completion.tokens_in
                    tokens_out += completion.tokens_out
                except LLMUnavailable:
                    answer = "I could not answer that."
        elif answer.startswith(NEEDS_WEB):
            # No search configured, so say so rather than leaking the marker to the watch.
            answer = "I don't know, and web search isn't set up."

        return Outcome(
            output=answer or "I do not know.",
            # The answer has to be the headline. Response renders as the word "Replied" and
            # hides the text behind a tap, which is how asking a question produced nothing
            # readable on the watch.
            semantic=coreschema.answer(answer or "I do not know."),
            data={"notes": notes, "sources": sources},
            cost_usd=cost,
            model=completion.model if completion else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def schedule(self, conn: sqlite3.Connection, request: Request) -> Outcome:
        """Speech to a real calendar event.

        Falls back to the journal at every step where it cannot finish, because a spoken
        commitment that vanishes is worse than one filed in the wrong place.
        """
        llm = self._llm_for(conn)
        if not google.connected(conn) or not llm.available or not self._budget_left(conn):
            await self._run(conn, request, "journal.write", {"text": request.text, "kind": "todo"})
            message = (
                "Calendar isn't connected, saved to your journal"
                if not google.connected(conn)
                else "Couldn't work out the time, saved to your journal"
            )
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=False),
            )

        now = datetime.now().astimezone()
        try:
            completion = await llm.complete(
                system=prompts.get(conn, "prompt_schedule"),
                user="\n".join(
                    [
                        f"Now: {now.isoformat()}",
                        f"Timezone offset: {now.strftime('%z')}",
                        "",
                        f"Request: {request.text}",
                    ]
                ),
                schema=EVENT_SCHEMA,
                max_tokens=300,
                timeout=30.0,
            )
        except LLMUnavailable:
            await self._run(conn, request, "journal.write", {"text": request.text, "kind": "todo"})
            message = "Couldn't reach the model, saved to your journal"
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=False),
            )

        events = normalise_events(completion.data)
        if not events:
            await self._run(conn, request, "journal.write", {"text": request.text, "kind": "todo"})
            message = "Couldn't work out the time, saved to your journal"
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=False),
            )

        created: list[Outcome] = []
        failed = 0
        for event in events:
            outcome = await self._run(
                conn,
                request,
                "calendar.create_event",
                event,
            )
            if outcome.is_error:
                failed += 1
            else:
                created.append(outcome)

        if not created:
            # Nothing landed, so keep the words rather than lose them.
            await self._run(conn, request, "journal.write", {"text": request.text, "kind": "todo"})
            message = "Couldn't add that to your calendar, saved to your journal"
            # Deliberately not is_error: the request was handled, just not the way asked.
            # Flagging it would invite the on-device model to retry, and a retry duplicates
            # the journal note. The reason is recorded for the feed instead.
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=False),
                error="calendar rejected every event",
                cost_usd=completion.cost_usd,
                model=completion.model,
            )

        if len(created) == 1 and not failed:
            # One event keeps the rich variant, which renders as a real calendar item.
            result = created[0]
            result.cost_usd += completion.cost_usd
            result.model = completion.model
            result.tokens_in = completion.tokens_in
            result.tokens_out = completion.tokens_out
            return result

        titles = ", ".join(str((c.data or {}).get("summary", "")) for c in created)
        note = f"Added {len(created)} events: {titles}"
        if failed:
            note += f" ({failed} failed)"
        return Outcome(
            output=note,
            semantic=coreschema.action_logged("signet", note, success=not failed),
            data={"created": len(created), "failed": failed},
            cost_usd=completion.cost_usd,
            model=completion.model,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )

    async def do(self, conn: sqlite3.Connection, request: Request) -> Outcome:
        """Rules first, then the agent loop.

        A matched rule is free and predictable, so "remember the fixer is low" never pays for
        a model. What falls through goes to the loop, which can chain steps: "move my 3pm and
        tell Sarah" is three calls, and the single-capability router could only ever do one.
        """
        llm = self._llm_for(conn)
        rules = load_rules(self.router.rules_path)
        planned = apply_rules(request.text, rules)
        if planned is not None:
            logger.info("do -> %s (%s)", planned.capability, planned.reason)
            outcome = await self._run(conn, request, planned.capability, planned.args)
            outcome.data = {"plan": planned.as_dict(), "result": outcome.data}
            return outcome

        if not llm.available or not self._budget_left(conn):
            # No model and no rule. Keeping the words beats guessing.
            return await self.capture(conn, request)

        outcome, trace = await agent.run(conn, self.registry, llm, request)
        logger.info(
            "do ran %d step(s), stopped because %s", len(trace.steps), trace.stopped_because
        )
        return outcome

    async def _decide(
        self, conn: sqlite3.Connection, request: Request, pending: list
    ) -> Outcome | None:
        """Answer a waiting approval by voice.

        Only when exactly one thing is waiting. With two, "yes" does not say which, and
        guessing at something marked destructive is the one place guessing is unacceptable.
        """
        if len(pending) != 1:
            titles = " or ".join(job["title"] for job in pending[:2])
            message = f"More than one thing is waiting. Approve in the app: {titles}"
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=False),
            )

        job = pending[0]
        if is_refusal(request.text):
            db.decide_job(conn, job["id"], "denied", "voice")
            message = f"Cancelled: {job['title']}"
            logger.info("denied %s by voice", job["id"])
            return Outcome(
                output=message,
                semantic=coreschema.action_logged("signet", message, success=True),
            )

        logger.info("approving %s by voice", job["id"])
        outcome = await run_approved(self.registry, conn, job["id"], by="voice")
        return outcome

    # --- dispatch -----------------------------------------------------------------

    async def call(self, conn: sqlite3.Connection, verb: str, request: Request) -> Outcome:
        """`request.text` is the single source of truth for what was said. The inlet is
        responsible for pulling it out of whatever argument name its verb uses."""
        handler = {
            "capture": self.capture,
            "ask": self.ask,
            "schedule": self.schedule,
            "do": self.do,
        }.get(verb)
        if handler is None:
            return Outcome(
                output=f"signet has no tool called {verb}.",
                semantic=coreschema.generic_failure(
                    f"No tool called {verb}.", llm_recoverable=False
                ),
                is_error=True,
            )

        # A bare "yes" is almost never a note. Checked before dispatch so it works whichever
        # verb the on-device model happened to pick.
        pending = db.pending_approvals(conn, limit=2)
        if pending and (is_confirmation(request.text) or is_refusal(request.text)):
            decided = await self._decide(conn, request, pending)
            if decided is not None:
                return decided

        started = time.monotonic()
        request_id = db.start_request(
            conn,
            text=request.text,
            source=request.source,
            verb=verb,
            client_id=request.client_id,
        )
        request.id = request_id
        try:
            outcome = await handler(conn, request)
        except Exception:
            logger.exception("verb %s failed", verb)
            db.finish_request(conn, request_id, status="error", error="unhandled")
            return Outcome(
                output="That did not work.",
                semantic=coreschema.generic_failure("That did not work."),
                is_error=True,
            )

        db.finish_request(
            conn,
            request_id,
            status="error" if outcome.is_error else "ok",
            result={"output": outcome.output, "semantic": outcome.semantic},
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd=outcome.cost_usd,
            model=outcome.model,
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
        )
        return outcome
