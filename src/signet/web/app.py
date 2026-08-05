"""The admin portal.

Mobile first, because the realistic use is standing somewhere with a phone having just pressed
the ring, wanting to know what signet did with it. Single column, tap targets, nothing that
scrolls sideways. Desktop is the enhancement, not the baseline.

Server-rendered Jinja with htmx for the feed refresh. No SPA, no build step, no npm.

**Auth.** signet is reached directly by the Cloudflare Tunnel with no reverse proxy in front,
so the portal cannot lean on forward auth. It therefore fails closed: with no
`SIGNET_ADMIN_PASSWORD` set, the portal is not mounted at all. Put Cloudflare Access in front
as well if you want a second lock; a browser can follow the redirect even though the ring
cannot.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from .. import db, google, openrouter, prompts, upstream
from ..config import Config
from ..registry import get_registry, reload_upstreams, run_approved

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Bundled rather than pulled from a CDN: signet is meant to work on a home server with no
# outbound dependency, and a dashboard that breaks when a CDN is unreachable is a bad dashboard.
HTMX = (
    (Path(__file__).parent / "static" / "htmx.min.js").read_text(encoding="utf-8")
    if (Path(__file__).parent / "static" / "htmx.min.js").exists()
    else ""
)


def _ago(timestamp: str) -> str:
    try:
        seconds = time.time() - float(timestamp)
    except ValueError:
        return "never"
    if seconds < 0 or seconds > 10**9:
        return "never"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile.

    Truncating `len * fraction` returns the *minimum* for small samples, which showed up as a
    p95 lower than the p50 on a verb with two calls. With ceil, a single sample is its own p95
    and n=2 gives the slower of the two, which is what the number is supposed to mean.
    """
    if not values:
        return 0
    ranked = sorted(values)
    index = math.ceil(fraction * len(ranked)) - 1
    return int(ranked[max(0, min(index, len(ranked) - 1))])


def _authed(request: Request) -> bool:
    return bool(request.session.get("admin"))


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    conn = _conn(request)
    try:
        context.setdefault("kill_switch", db.kill_switch_on(conn))
        # Shown on every page: something waiting for you is not a thing to go looking for.
        context.setdefault("awaiting", len(db.pending_approvals(conn)))
    finally:
        conn.close()
    context.setdefault("favicon_version", FAVICON_VERSION)
    return TEMPLATES.TemplateResponse(request, template, context)


def _conn(request: Request):
    cfg: Config = request.app.state.cfg
    return db.connect(cfg.db_path)


async def login_form(request: Request) -> Response:
    if _authed(request):
        return RedirectResponse("/app/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": None, "favicon_version": FAVICON_VERSION}
    )


async def login(request: Request) -> Response:
    form = await request.form()
    cfg: Config = request.app.state.cfg
    supplied = str(form.get("password", ""))
    # Constant time: the password is short enough that a timing signal is worth avoiding.
    if cfg.admin_password and secrets.compare_digest(supplied, cfg.admin_password):
        request.session["admin"] = True
        return RedirectResponse("/app/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"error": "Wrong password.", "favicon_version": FAVICON_VERSION},
        status_code=401,
    )


async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/app/login", status_code=303)


async def dashboard(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    cfg: Config = request.app.state.cfg
    conn = _conn(request)
    try:
        today = "strftime('%Y-%m-%dT00:00:00Z', 'now')"
        captures_today = conn.execute(
            f"SELECT COUNT(*) c FROM journal WHERE created_at >= {today}"
        ).fetchone()["c"]
        requests_today = conn.execute(
            f"SELECT COUNT(*) c FROM requests WHERE received_at >= {today}"
        ).fetchone()["c"]
        errors_today = conn.execute(
            f"SELECT COUNT(*) c FROM requests WHERE received_at >= {today} AND status = 'error'"
        ).fetchone()["c"]
        journal_total = conn.execute("SELECT COUNT(*) c FROM journal").fetchone()["c"]
        journal_week = conn.execute(
            "SELECT COUNT(*) c FROM journal WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()["c"]

        by_verb: dict[str, list[int]] = {}
        for row in conn.execute(
            "SELECT verb, latency_ms FROM requests "
            "WHERE latency_ms IS NOT NULL AND verb IS NOT NULL"
        ):
            by_verb.setdefault(row["verb"], []).append(row["latency_ms"])

        # Captures per day for the chart. Filled from a dict so quiet days are zero bars
        # rather than gaps, which would misread as a narrower window.
        counted = {
            row["day"]: row["c"]
            for row in conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c FROM journal "
                "WHERE created_at >= datetime('now', '-14 days') GROUP BY day"
            )
        }
        today = datetime.now(UTC).date()
        days = [today - timedelta(days=offset) for offset in range(13, -1, -1)]
        busiest = max([counted.get(d.isoformat(), 0) for d in days] or [0])
        activity = [
            {
                "date": d.strftime("%d %b"),
                "count": counted.get(d.isoformat(), 0),
                "pct": int(counted.get(d.isoformat(), 0) / busiest * 100) if busiest else 0,
            }
            for d in days
        ]

        latency = [
            {
                "verb": verb,
                "n": len(values),
                "p50": int(statistics.median(values)),
                "p95": _percentile(values, 0.95),
            }
            for verb, values in sorted(by_verb.items())
        ]
        slowest = max([row["p95"] for row in latency] or [0])
        for row in latency:
            # Relative bar, so the slowest verb is obvious without needing an axis.
            row["pct"] = int(row["p95"] / slowest * 100) if slowest else 0

        stats = {
            "captures_today": captures_today,
            "requests_today": requests_today,
            "errors_today": errors_today,
            "spend_today": db.spend_today(conn),
            "cap": cfg.daily_cost_cap_usd,
            "journal_total": journal_total,
            "journal_week": journal_week,
            "activity": activity,
            "busiest": busiest,
            "latency": latency,
        }
        try:
            todo_counts = db.count_todos(conn)
            todo_overdue = len(db.overdue_todos(conn))
        except Exception:
            todo_counts = {"open": 0, "done": 0, "deleted": 0, "total": 0}
            todo_overdue = 0
        stats["todos_open"] = todo_counts["open"]
        stats["todos_done"] = todo_counts["done"]
        stats["todos_overdue"] = todo_overdue
        stats["todos_total"] = todo_counts["total"]
        tokens = list(conn.execute("SELECT * FROM tokens ORDER BY created_at"))
    finally:
        conn.close()
    return _render(request, "dashboard.html", page="dashboard", stats=stats, tokens=tokens)


def _feed_rows(request: Request, limit: int = 50) -> list[dict]:
    conn = _conn(request)
    try:
        rows = []
        for row in conn.execute(
            "SELECT * FROM requests ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)
        ):
            answer = None
            if row["result_json"]:
                try:
                    answer = (json.loads(row["result_json"]) or {}).get("output")
                except json.JSONDecodeError:
                    answer = None
            rows.append(
                {
                    "verb": row["verb"],
                    "status": row["status"],
                    "received_at": row["received_at"],
                    "text": row["text"],
                    "answer": answer,
                    "latency_ms": row["latency_ms"],
                    "cost_usd": row["cost_usd"],
                    "model": row["model"],
                    "error": row["error"],
                }
            )
        return rows
    finally:
        conn.close()


async def feed(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    return _render(request, "feed.html", page="feed", requests=_feed_rows(request))


async def feed_rows(request: Request) -> Response:
    """htmx polls this. Returns the rows only, so the page does not flash."""
    if not _authed(request):
        return PlainTextResponse("", status_code=401)
    return TEMPLATES.TemplateResponse(request, "_rows.html", {"requests": _feed_rows(request)})


async def journal(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    query = (request.query_params.get("q") or "").strip()
    showing_deleted = request.query_params.get("show") == "deleted"
    conn = _conn(request)
    try:
        if showing_deleted:
            entries = db.list_journal(conn, deleted=True)
        elif query:
            entries = db.search_journal(conn, query, limit=100)
        else:
            entries = db.list_journal(conn)
        deleted_count = len(db.list_journal(conn, deleted=True, limit=1000))
    finally:
        conn.close()
    return _render(
        request,
        "journal.html",
        page="journal",
        entries=entries,
        q=query,
        showing_deleted=showing_deleted,
        deleted_count=deleted_count,
        undo_id=request.session.pop("undo_id", None),
    )


def _back_to_journal(request: Request) -> str:
    """Return to whichever view the action came from, so editing an entry found by search
    does not dump you back at the top of an unfiltered list."""
    return request.headers.get("referer") or "/app/journal"


async def edit_journal(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    conn = _conn(request)
    try:
        db.update_journal(conn, request.path_params["entry_id"], str(form.get("text", "")))
    finally:
        conn.close()
    return RedirectResponse(_back_to_journal(request), status_code=303)


async def delete_journal(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    entry_id = request.path_params["entry_id"]
    conn = _conn(request)
    try:
        if db.delete_journal(conn, entry_id):
            # Offer undo on the next render. A mis-tap on a phone should cost one tap back.
            request.session["undo_id"] = entry_id
    finally:
        conn.close()
    return RedirectResponse(_back_to_journal(request), status_code=303)


async def restore_journal(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.restore_journal(conn, request.path_params["entry_id"])
    finally:
        conn.close()
    return RedirectResponse(_back_to_journal(request), status_code=303)


async def purge_journal(request: Request) -> Response:
    """Permanent. Only reachable from the deleted view."""
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.purge_journal(conn, request.path_params["entry_id"])
    finally:
        conn.close()
    return RedirectResponse("/app/journal?show=deleted", status_code=303)


# --- todos --------------------------------------------------------------------


def _parse_due_input(raw: str) -> str | None:
    """Parse HTML date/datetime-local into ISO Z. Empty means clear."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # datetime-local gives 2026-08-06T14:30 and date gives 2026-08-06. A space separator turns
    # up when the value arrived from an MCP client rather than the form, so accept that too and
    # normalise here — this is the only place a due date is parsed.
    candidate = raw.replace(" ", "T", 1) if "T" not in raw and " " in raw else raw
    try:
        if "T" in candidate:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            # Treat naive as local time -> UTC (store as UTC Z)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        # Date only -> due at 09:00 UTC (avoids midnight edge)
        dt = datetime.fromisoformat(candidate + "T09:00:00").replace(tzinfo=UTC)
        return dt.isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _todo_due_label(due_at: str | None) -> tuple[str, str]:
    """Return (label, tone) for template: tone is ''|'overdue'|'today'|'soon'."""
    if not due_at:
        return "", ""
    try:
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return due_at[:10], ""
    days = (dt.date() - datetime.now(UTC).date()).days
    # 09:00 is what a todo with no stated time is given, so read it back as no time at all.
    timed = (dt.hour, dt.minute) != (9, 0)
    if days < 0:
        # Only a past date is overdue. Something due earlier today is still today's work.
        return dt.strftime("%b %d"), "overdue"
    if days == 0:
        return (dt.strftime("%H:%M today") if timed else "today"), "today"
    if days == 1:
        return (f"tomorrow {dt.strftime('%H:%M')}" if timed else "tomorrow"), "soon"
    if days <= 7:
        return dt.strftime("%a %b %d"), "soon"
    return dt.strftime("%b %d, %Y"), ""


def _back_to_todos(request: Request) -> str:
    params = []
    q = request.query_params.get("q")
    f = request.query_params.get("filter")
    show = request.query_params.get("show")
    if q:
        params.append(f"q={q}")
    if f:
        params.append(f"filter={f}")
    if show:
        params.append(f"show={show}")
    qs = ("?" + "&".join(params)) if params else ""
    return request.headers.get("referer") or f"/app/todos{qs}"


async def todos_page(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    q = (request.query_params.get("q") or "").strip()
    filt = (request.query_params.get("filter") or "open").strip()
    show = request.query_params.get("show")  # deleted
    conn = _conn(request)
    try:
        if show == "deleted":
            # Show soft-deleted
            rows = list(
                conn.execute(
                    "SELECT * FROM todos WHERE deleted_at IS NOT NULL "
                    "ORDER BY deleted_at DESC LIMIT 100"
                )
            )
            counts = db.count_todos(conn)
            overdue = []
        else:
            # Normal views
            if q:
                # Search respects filter unless filter is all
                status_filter = filt if filt in ("open", "done") else None
                rows = db.list_todos(conn, status=status_filter, query=q, limit=100)
            else:
                if filt == "done":
                    rows = db.list_todos(conn, status="done", limit=100)
                elif filt == "all":
                    rows = db.list_todos(conn, status=None, limit=100)
                elif filt == "overdue":
                    rows = db.overdue_todos(conn)
                else:  # open default
                    rows = db.list_todos(conn, status="open", limit=100)
                    filt = "open"
            try:
                counts = db.count_todos(conn)
            except Exception:
                counts = {"open": 0, "done": 0, "deleted": 0, "total": 0}
            try:
                overdue = db.overdue_todos(conn)
            except Exception:
                overdue = []
            # Enrich due labels for template performance (avoid per-row parsing in Jinja)
            # Attach _due_label and _tone as extra keys via dict copy; rows are sqlite3.Row
    finally:
        conn.close()

    # Convert rows to dicts with enriched fields for template
    enriched = []
    for r in rows:
        d = dict(r)
        label, tone = _todo_due_label(d.get("due_at"))
        d["_due_label"] = label
        d["_due_tone"] = tone
        # priority label
        pri = d.get("priority", 0)
        d["_pri_label"] = {0: "", 1: "high", 2: "urgent"}.get(pri, "")
        d["_pri_tone"] = {0: "", 1: "high", 2: "urgent"}.get(pri, "")
        enriched.append(d)

    return _render(
        request,
        "todos.html",
        page="todos",
        todos=enriched,
        q=q,
        filter=filt,
        show_deleted=(show == "deleted"),
        counts=counts,
        overdue_count=len(overdue),
        undo_id=request.session.pop("undo_todo_id", None),
    )


async def create_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    text = str(form.get("text") or "").strip()
    if not text:
        return RedirectResponse("/app/todos", status_code=303)
    due_raw = str(form.get("due_at") or "")
    # An unparseable date is dropped rather than rejected: the todo itself still gets saved.
    due_at = _parse_due_input(due_raw)
    try:
        priority = int(str(form.get("priority") or "0"))
    except ValueError:
        priority = 0
    priority = max(0, min(2, priority))
    recurrence = str(form.get("recurrence") or "none").strip().lower()
    if recurrence not in db.VALID_RECURRENCE:
        recurrence = "none"
    conn = _conn(request)
    try:
        db.add_todo(conn, text, due_at=due_at, priority=priority, recurrence=recurrence)
    except ValueError:
        # Invalid priority/recurrence already clamped; text empty already handled
        pass
    finally:
        conn.close()
    # Preserve filter/q if coming from filtered view
    return RedirectResponse(_back_to_todos(request), status_code=303)


async def toggle_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.toggle_todo(conn, request.path_params["todo_id"])
    finally:
        conn.close()
    return RedirectResponse(_back_to_todos(request), status_code=303)


async def edit_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    todo_id = request.path_params["todo_id"]
    text = form.get("text")
    due_raw = form.get("due_at")
    priority_raw = form.get("priority")
    recurrence_raw = form.get("recurrence")
    # Normalise
    due_at = None
    due_provided = False
    if due_raw is not None:
        due_provided = True
        raw = str(due_raw).strip()
        if raw == "":
            due_at = ""  # clear
        else:
            parsed = _parse_due_input(raw)
            # If raw looks like ISO and parse failed, try raw directly
            if parsed is None:
                try:
                    datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    parsed = raw
                except ValueError:
                    parsed = None
                    due_provided = False  # ignore invalid date rather than clearing
            due_at = parsed if parsed is not None else ""
            if due_at is None:
                due_provided = False
    priority = None
    if priority_raw is not None and str(priority_raw).strip() != "":
        try:
            priority = max(0, min(2, int(str(priority_raw))))
        except ValueError:
            priority = None
    recurrence = None
    if recurrence_raw is not None:
        r = str(recurrence_raw).strip().lower()
        if r in db.VALID_RECURRENCE:
            recurrence = r
    conn = _conn(request)
    try:
        db.update_todo(
            conn,
            todo_id,
            text=str(text) if text is not None else None,
            due_at=due_at if due_provided else None,  # type: ignore[arg-type]
            priority=priority,
            recurrence=recurrence,
        )
    finally:
        conn.close()
    return RedirectResponse(_back_to_todos(request), status_code=303)


async def delete_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        if db.delete_todo(conn, request.path_params["todo_id"]):
            request.session["undo_todo_id"] = request.path_params["todo_id"]
    finally:
        conn.close()
    return RedirectResponse(_back_to_todos(request), status_code=303)


async def restore_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.restore_todo(conn, request.path_params["todo_id"])
    finally:
        conn.close()
    return RedirectResponse(_back_to_todos(request), status_code=303)


async def purge_todo(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.purge_todo(conn, request.path_params["todo_id"])
    finally:
        conn.close()
    return RedirectResponse("/app/todos?show=deleted", status_code=303)


async def tokens(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        rows = list(conn.execute("SELECT * FROM tokens ORDER BY created_at"))
    finally:
        conn.close()
    new_token = request.session.pop("new_token", None)
    conn = _conn(request)
    try:
        scopes = upstream.known_scopes(conn)
    finally:
        conn.close()
    return _render(
        request,
        "tokens.html",
        page="tokens",
        tokens=rows,
        all_scopes=scopes,
        default_scopes=set(db.DEFAULT_RING_SCOPES),
        new_token=new_token,
        # Built the same way as the OAuth redirect, so it is right behind the tunnel.
        mcp_url=_public_base(request) + "/mcp",
    )


async def create_token(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    name = str(form.get("name") or "").strip() or "unnamed"
    conn = _conn(request)
    try:
        chosen = [s for s in form.getlist("scopes") if s in upstream.known_scopes(conn)]
        _, plaintext = db.create_token(conn, name, chosen or list(db.DEFAULT_RING_SCOPES))
    finally:
        conn.close()
    # Shown once on the next render, then gone.
    request.session["new_token"] = plaintext
    return RedirectResponse("/app/tokens", status_code=303)


async def revoke_token(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.revoke_token(conn, int(request.path_params["token_id"]))
    finally:
        conn.close()
    return RedirectResponse("/app/tokens", status_code=303)


SETTINGS_FIELDS = [
    {
        "key": "openrouter_api_key",
        "label": "OpenRouter API key",
        "kind": "secret",
        "help": "Without it, ask falls back to plain search of your journal.",
        "env": "openrouter_api_key",
    },
    {
        "key": "exa_api_key",
        "label": "Exa API key",
        "kind": "secret",
        "help": "Web search. Without it, ask answers from your journal only.",
        "env": "exa_api_key",
    },
    {
        "key": "provider",
        "label": "Provider routing (JSON)",
        "kind": "json",
        "help": 'OpenRouter provider preferences, e.g. {"order": ["DeepSeek"], '
        '"allow_fallbacks": false}. Blank means let OpenRouter choose.',
        "env": None,
    },
    {
        "key": "model_params",
        "label": "Model parameters (JSON)",
        "kind": "json",
        "help": 'Merged into the request body, e.g. {"temperature": 0.2} or '
        '{"reasoning": {"effort": "high"}}.',
        "env": None,
    },
    {
        "key": "prompt_answer",
        "label": "Answer prompt",
        "kind": "prompt",
        "help": "Voice and tone for answers. These are read on a watch, so keep the brevity "
        "instruction unless you want long replies.",
        "env": None,
    },
    {
        "key": "prompt_router",
        "label": "Routing prompt",
        "kind": "prompt",
        "help": "How a request is matched to a tool.",
        "env": None,
    },
    {
        "key": "prompt_schedule",
        "label": "Scheduling prompt",
        "kind": "prompt",
        "help": "How spoken times become calendar events.",
        "env": None,
    },
    {
        "key": "public_url",
        "label": "Public URL",
        "kind": "text",
        "help": "How the outside world reaches signet, for example "
        "https://signet.example.com. Only used to build the Google redirect URI.",
        "env": "public_url",
    },
    {
        "key": "google_client_id",
        "label": "Google client ID",
        "kind": "text",
        "help": "From a Google Cloud OAuth client of type Web application.",
        "env": None,
    },
    {
        "key": "google_client_secret",
        "label": "Google client secret",
        "kind": "secret",
        "help": "Stored server side and never shown again.",
        "env": None,
    },
    {
        "key": "daily_cost_cap_usd",
        "label": "Daily spend cap, dollars",
        "kind": "number",
        "help": "A runaway-loop breaker, not an economy measure. Capture is never affected.",
        "env": "daily_cost_cap_usd",
    },
]


async def settings_page(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    cfg: Config = request.app.state.cfg
    conn = _conn(request)
    try:
        fields = []
        for spec in SETTINGS_FIELDS:
            stored = db.get_config(conn, spec["key"])
            from_env = getattr(cfg, spec["env"], None) if spec["env"] else None
            if stored:
                source, value = "portal", stored
            elif from_env:
                source, value = "env", from_env
            else:
                source, value = "unset", None
            if not value and spec["kind"] == "prompt":
                value, source = prompts.DEFAULTS.get(spec["key"], ""), "default"
            fields.append(
                {
                    **spec,
                    # Secrets are never sent to the browser, only whether one exists.
                    "value": None if spec["kind"] == "secret" else value,
                    "is_set": bool(value),
                    "source": source,
                }
            )
    finally:
        conn.close()
    saved = request.session.pop("settings_saved", False)
    errors = request.session.pop("settings_errors", [])
    conn = _conn(request)
    try:
        # Both, not either. With only the client ID set the Connect button appeared, sent the
        # user through Google's consent screen, and then died at the token exchange with
        # "client_secret is missing" — after they had already granted access.
        has_id = bool(db.get_config(conn, "google_client_id"))
        has_secret = bool(db.get_config(conn, "google_client_secret"))
        google_ready = has_id and has_secret
        google_missing = (
            "client ID and secret"
            if not has_id and not has_secret
            else "client secret"
            if not has_secret
            else "client ID"
        )
        google_connected = google.connected(conn)

        current_model = db.get_config(conn, "model") or cfg.model
        models = await openrouter.fetch_models(conn)
        providers = await openrouter.fetch_providers(conn, current_model)
        selected_providers, allow_fallbacks = openrouter.read_provider_config(
            db.get_config(conn, "provider")
        )
        # Providers carry a structured-output flag, so order by name rather than by object.
        by_name = {p["name"]: p for p in providers}
        # Ticked ones first, in their saved order, so the list reads as the routing order.
        ordered = [by_name[name] for name in selected_providers if name in by_name]
        ordered += [p for p in providers if p["name"] not in selected_providers]
        cached_at = db.get_setting(conn, openrouter.CACHE_AT_KEY, "0")
    finally:
        conn.close()
    return _render(
        request,
        "settings.html",
        page="settings",
        fields=fields,
        saved=saved,
        errors=errors,
        google_ready=google_ready,
        google_missing=google_missing,
        google_connected=google_connected,
        redirect_uri=_redirect_uri(request),
        models=models,
        model_ids={m.id for m in models},
        current_model=current_model,
        providers=ordered,
        selected_providers=selected_providers,
        allow_fallbacks=allow_fallbacks,
        # Warn only when the choice actually breaks something: picked providers, none of
        # which can do structured output, and no fallback to rescue it.
        picked_all_unstructured=bool(selected_providers)
        and not allow_fallbacks
        and not any(p["structured"] for p in ordered if p["name"] in selected_providers),
        cached_ago=_ago(cached_at),
    )


async def save_settings(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    errors: list[str] = []
    # Clear and Refresh are submit buttons inside the same form as everything else. Treating
    # them as exclusive branches meant clicking either silently discarded every other edit on
    # the page, so a ticked provider could vanish without a word. Always save first, then
    # apply whichever button was pressed.
    clearing = str(form.get("clear") or "")
    refreshing = str(form.get("refresh") or "") == "models"

    conn = _conn(request)
    try:
        chosen_model = str(form.get("model") or "").strip()
        if chosen_model and clearing != "model":
            db.set_config(conn, "model", chosen_model)

        # Only names that are real providers for this model. A rendering bug once put
        # stringified objects in these values, and without this they would have been written
        # straight into the routing config, where they would match no endpoint.
        known = {
            p["name"]
            for p in await openrouter.fetch_providers(
                conn, db.get_config(conn, "model") or request.app.state.cfg.model
            )
        }
        # Checkbox values arrive in DOM order, which the up and down buttons control, so the
        # submitted order is the routing order.
        order = [str(v) for v in form.getlist("provider_order") if str(v) in known]
        allow_fallbacks = bool(form.get("allow_fallbacks"))
        if clearing != "provider":
            provider_config = openrouter.build_provider_config(order, allow_fallbacks)
            if provider_config:
                db.set_config(conn, "provider", json.dumps(provider_config))
            else:
                db.clear_config(conn, "provider")

        for spec in SETTINGS_FIELDS:
            if spec["key"] == clearing:
                continue
            submitted = str(form.get(spec["key"], "")).strip()
            if spec["kind"] == "secret" and not submitted:
                # Blank means "leave it alone", since the current value is never shown.
                continue
            if spec["kind"] == "number" and submitted:
                try:
                    float(submitted)
                except ValueError:
                    errors.append(f"{spec['label']}: not a number")
                    continue
            if spec["kind"] == "json" and submitted:
                try:
                    parsed = json.loads(submitted)
                except json.JSONDecodeError as exc:
                    errors.append(f"{spec['label']}: invalid JSON, {exc.msg}")
                    continue
                if not isinstance(parsed, dict):
                    errors.append(f"{spec['label']}: must be a JSON object")
                    continue
            if spec["kind"] == "prompt" and submitted == prompts.DEFAULTS.get(spec["key"]):
                # Unchanged from the default, so store nothing and keep following it.
                db.clear_config(conn, spec["key"])
                continue
            if submitted:
                db.set_config(conn, spec["key"], submitted)

        if clearing in db.CONFIGURABLE:
            db.clear_config(conn, clearing)
        if refreshing:
            await openrouter.fetch_models(conn, force=True)
            model_now = db.get_config(conn, "model") or request.app.state.cfg.model
            await openrouter.fetch_providers(conn, model_now, force=True)
    finally:
        conn.close()

    request.session["settings_saved"] = True
    request.session["settings_errors"] = errors
    return RedirectResponse("/app/settings", status_code=303)


CALLBACK_PATH = "/app/google/callback"


def _public_base(request: Request) -> str:
    """Where the outside world reaches signet.

    A configured value wins, because deriving this is genuinely ambiguous here: the Cloudflare
    Tunnel terminates TLS and forwards plain HTTP to the origin, so the request arrives looking
    like http even though the browser used https. Google requires https and an exact match, so
    a derived http URI fails with redirect_uri_mismatch.
    """
    conn = _conn(request)
    try:
        configured = db.get_config(conn, "public_url")
    finally:
        conn.close()
    configured = configured or request.app.state.cfg.public_url
    if configured:
        return configured.rstrip("/")

    host = request.headers.get("host", request.url.netloc)
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme
    # Anything not on localhost is reached over TLS in practice, and Google will not accept a
    # plain-http redirect for a real hostname anyway.
    if scheme != "https" and not host.startswith(("localhost", "127.0.0.1")):
        scheme = "https"
    return f"{scheme}://{host}"


def _redirect_uri(request: Request) -> str:
    """Must match a redirect URI registered on the Google OAuth client exactly."""
    return _public_base(request) + CALLBACK_PATH


async def google_connect(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        client_id = db.get_config(conn, "google_client_id")
        client_secret = db.get_config(conn, "google_client_secret")
    finally:
        conn.close()
    # Checked here as well as in the template, so a stale page cannot start a doomed flow.
    if not client_id or not client_secret:
        missing = "client ID" if not client_id else "client secret"
        request.session["settings_errors"] = [
            f"Google {missing} is not set. Fill both in below and save before connecting."
        ]
        return RedirectResponse("/app/settings", status_code=303)

    # CSRF: the state is generated here, kept in the session, and compared on return.
    state = secrets.token_urlsafe(24)
    request.session["google_state"] = state
    return RedirectResponse(
        google.authorize_url(client_id, _redirect_uri(request), state), status_code=303
    )


async def google_callback(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)

    expected = request.session.pop("google_state", None)
    supplied = request.query_params.get("state")
    if not expected or supplied != expected:
        request.session["settings_errors"] = ["Google sign-in did not match. Try again."]
        return RedirectResponse("/app/settings", status_code=303)

    code = request.query_params.get("code")
    if not code:
        reason = request.query_params.get("error", "no code returned")
        request.session["settings_errors"] = [f"Google sign-in failed: {reason}"]
        return RedirectResponse("/app/settings", status_code=303)

    conn = _conn(request)
    try:
        client_id = db.get_config(conn, "google_client_id") or ""
        client_secret = db.get_config(conn, "google_client_secret") or ""
        try:
            await google.exchange_code(conn, client_id, client_secret, code, _redirect_uri(request))
        except google.GoogleUnavailable as exc:
            request.session["settings_errors"] = [str(exc)]
            return RedirectResponse("/app/settings", status_code=303)
    finally:
        conn.close()

    request.session["settings_saved"] = True
    return RedirectResponse("/app/settings", status_code=303)


async def google_disconnect(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        google.disconnect(conn)
    finally:
        conn.close()
    return RedirectResponse("/app/settings", status_code=303)


async def approvals(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.expired_approvals(conn)
        pending = db.pending_approvals(conn)
        decided = list(
            conn.execute(
                "SELECT * FROM jobs WHERE status NOT IN (?, 'running') "
                "ORDER BY COALESCE(decided_at, created_at) DESC LIMIT 10",
                (db.AWAITING,),
            )
        )
    finally:
        conn.close()
    return _render(request, "approvals.html", page="approvals", pending=pending, decided=decided)


async def approve(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        outcome = await run_approved(
            get_registry(), conn, request.path_params["job_id"], by="portal"
        )
    finally:
        conn.close()
    request.session["approval_note"] = outcome.output
    return RedirectResponse("/app/approvals", status_code=303)


async def deny(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        db.decide_job(conn, request.path_params["job_id"], "denied", "portal")
    finally:
        conn.close()
    return RedirectResponse("/app/approvals", status_code=303)


async def upstreams(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        rows = upstream.list_upstreams(conn)
        tools_by_name = {}
        for row in rows:
            policy = upstream.policy_of(row)
            server = upstream.from_row(row)
            tools_by_name[row["name"]] = [
                {
                    "name": t["name"],
                    "description": (t.get("description") or "")[:90],
                    "mode": upstream.decide(t["name"], server, policy),
                    "chosen": t["name"] in policy,
                }
                for t in upstream.cached_tools(row)
            ]
    finally:
        conn.close()
    return _render(
        request,
        "upstreams.html",
        page="upstreams",
        rows=rows,
        tools_by_name=tools_by_name,
        errors=request.session.pop("upstream_errors", []),
        note=request.session.pop("upstream_note", None),
    )


async def _connect(conn, name: str) -> tuple[int, str | None]:
    """Fetch and cache a server's tools. Returns how many, and why not."""
    row = upstream.get_upstream(conn, name)
    if row is None:
        return 0, "No such server."
    try:
        tools = await upstream.fetch_tools(upstream.from_row(row))
    except Exception as exc:
        upstream.record_failure(conn, name, str(exc))
        return 0, f"Could not reach {name}: {str(exc)[:200]}"
    upstream.record_tools(conn, name, tools)
    reload_upstreams(conn)
    return len(tools), None


async def add_upstream(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    name = re.sub(r"[^a-z0-9_]", "", str(form.get("name") or "").strip().lower())
    url = str(form.get("url") or "").strip()
    if not name or not url:
        request.session["upstream_errors"] = ["A short name and a URL are both required."]
        return RedirectResponse("/app/upstreams", status_code=303)

    conn = _conn(request)
    try:
        upstream.save_upstream(
            conn,
            name=name,
            url=url,
            auth_header=str(form.get("auth_header") or "").strip() or None,
            trusted=bool(form.get("trusted")),
            approve_all=bool(form.get("approve_all")),
        )
        count, error = await _connect(conn, name)
    finally:
        conn.close()

    if error:
        request.session["upstream_errors"] = [error]
    else:
        plural = "" if count == 1 else "s"
        request.session["upstream_note"] = (
            f"{name}: {count} tool{plural} mounted. "
            f"Grant a token the mcp:{name} scope to let the ring use them."
        )
    return RedirectResponse("/app/upstreams", status_code=303)


async def refresh_upstream(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    name = request.path_params["name"]
    conn = _conn(request)
    try:
        count, error = await _connect(conn, name)
    finally:
        conn.close()
    if error:
        request.session["upstream_errors"] = [error]
    else:
        request.session["upstream_note"] = f"{name}: {count} tools."
    return RedirectResponse("/app/upstreams", status_code=303)


async def toggle_upstream(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        row = upstream.get_upstream(conn, request.path_params["name"])
        if row is not None:
            conn.execute(
                "UPDATE upstreams SET enabled = ? WHERE name = ?",
                (0 if row["enabled"] else 1, row["name"]),
            )
        reload_upstreams(conn)
    finally:
        conn.close()
    return RedirectResponse("/app/upstreams", status_code=303)


async def remove_upstream(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        upstream.delete_upstream(conn, request.path_params["name"])
        reload_upstreams(conn)
    finally:
        conn.close()
    return RedirectResponse("/app/upstreams", status_code=303)


async def set_tool_policy(request: Request) -> Response:
    """Save the per-tool choices for one server."""
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    form = await request.form()
    name = request.path_params["name"]
    policy = {
        key[len("tool:") :]: str(value)
        for key, value in form.items()
        if key.startswith("tool:") and str(value) in upstream.POLICIES
    }
    conn = _conn(request)
    try:
        upstream.set_policy(conn, name, policy)
        reload_upstreams(conn)
    finally:
        conn.close()
    request.session["upstream_note"] = f"{name}: saved, and in effect now."
    return RedirectResponse("/app/upstreams", status_code=303)


async def toggle_kill(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        current = db.kill_switch_on(conn)
        db.set_setting(conn, "kill_switch", "off" if current else "on")
    finally:
        conn.close()
    return RedirectResponse(request.headers.get("referer", "/app/"), status_code=303)


async def htmx_js(request: Request) -> Response:
    return Response(HTMX, media_type="application/javascript")


# A wax seal bearing a monogram, which is what a signet ring actually leaves behind.
# Checked at 16px before choosing: the earlier band-and-seal drawing collapsed into a smudge
# at favicon size, while a filled disc with a cut-out letter stays legible on light and dark.
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<circle cx="16" cy="16" r="14" fill="#c1272d"/>'
    '<path d="M20.5 11.2c-1-.9-2.5-1.5-4.2-1.5-3 0-5 1.6-5 3.9 0 2 1.4 3.1 4 3.7l1.6.4'
    "c1.5.4 2.1.8 2.1 1.6 0 1-1 1.7-2.6 1.7-1.7 0-3.2-.7-4.2-1.8l-1.5 2.5c1.3 1.3 3.4 2.1"
    " 5.7 2.1 3.2 0 5.4-1.6 5.4-4.1 0-2.1-1.4-3.2-4.2-3.9l-1.6-.4c-1.3-.3-1.9-.7-1.9-1.4"
    ' 0-.9.9-1.5 2.3-1.5 1.4 0 2.6.5 3.5 1.3z" fill="#fff"/>'
    "</svg>"
)


# The URL carries a hash of the drawing, so changing it always busts the cache. Without this
# Cloudflare kept serving the previous favicon from its edge for a month: it overrode the
# max-age with its own default for static assets, and the redeploy looked like it had failed.
FAVICON_VERSION = hashlib.sha256(FAVICON.encode()).hexdigest()[:8]


async def favicon(request: Request) -> Response:
    return Response(
        FAVICON,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )


def build(cfg: Config) -> Starlette | None:
    """Returns None when no admin password is set, which leaves the portal unmounted.

    Failing closed matters here: this process is reachable from the internet through the
    tunnel, so a portal with no password would be an open admin panel rather than a
    convenience.
    """
    if not cfg.admin_password:
        return None

    routes = [
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/", dashboard),
        Route("/feed", feed),
        Route("/feed/rows", feed_rows),
        Route("/journal", journal),
        Route("/journal/{entry_id}/edit", edit_journal, methods=["POST"]),
        Route("/journal/{entry_id}/delete", delete_journal, methods=["POST"]),
        Route("/journal/{entry_id}/restore", restore_journal, methods=["POST"]),
        Route("/journal/{entry_id}/purge", purge_journal, methods=["POST"]),
        Route("/todos", todos_page, methods=["GET"]),
        Route("/todos", create_todo, methods=["POST"]),
        Route("/todos/{todo_id}/toggle", toggle_todo, methods=["POST"]),
        Route("/todos/{todo_id}/edit", edit_todo, methods=["POST"]),
        Route("/todos/{todo_id}/delete", delete_todo, methods=["POST"]),
        Route("/todos/{todo_id}/restore", restore_todo, methods=["POST"]),
        Route("/todos/{todo_id}/purge", purge_todo, methods=["POST"]),
        Route("/tokens", tokens, methods=["GET"]),
        Route("/tokens", create_token, methods=["POST"]),
        Route("/tokens/{token_id:int}/revoke", revoke_token, methods=["POST"]),
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings", save_settings, methods=["POST"]),
        Route("/google/connect", google_connect, methods=["POST"]),
        Route("/google/callback", google_callback, methods=["GET"], name="google_callback"),
        Route("/google/disconnect", google_disconnect, methods=["POST"]),
        Route("/upstreams", upstreams, methods=["GET"]),
        Route("/upstreams", add_upstream, methods=["POST"]),
        Route("/upstreams/{name}/refresh", refresh_upstream, methods=["POST"]),
        Route("/upstreams/{name}/toggle", toggle_upstream, methods=["POST"]),
        Route("/upstreams/{name}/delete", remove_upstream, methods=["POST"]),
        Route("/upstreams/{name}/policy", set_tool_policy, methods=["POST"]),
        Route("/approvals", approvals),
        Route("/approvals/{job_id}/approve", approve, methods=["POST"]),
        Route("/approvals/{job_id}/deny", deny, methods=["POST"]),
        Route("/kill", toggle_kill, methods=["POST"]),
        Route("/static/htmx.min.js", htmx_js),
        Route("/favicon.svg", favicon),
    ]
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=cfg.session_secret,
                same_site="lax",
                https_only=False,  # the tunnel terminates TLS; the hop to signet is loopback
                max_age=60 * 60 * 24 * 14,
            )
        ],
    )
    app.state.cfg = cfg
    return app
