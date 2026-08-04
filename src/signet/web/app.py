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

import json
import math
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

from .. import db, google, openrouter, prompts
from ..config import Config

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
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse(request, template, context)


def _conn(request: Request):
    cfg: Config = request.app.state.cfg
    return db.connect(cfg.db_path)


async def login_form(request: Request) -> Response:
    if _authed(request):
        return RedirectResponse("/app/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})


async def login(request: Request) -> Response:
    form = await request.form()
    cfg: Config = request.app.state.cfg
    supplied = str(form.get("password", ""))
    # Constant time: the password is short enough that a timing signal is worth avoiding.
    if cfg.admin_password and secrets.compare_digest(supplied, cfg.admin_password):
        request.session["admin"] = True
        return RedirectResponse("/app/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": "Wrong password."}, status_code=401
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


async def tokens(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        rows = list(conn.execute("SELECT * FROM tokens ORDER BY created_at"))
    finally:
        conn.close()
    new_token = request.session.pop("new_token", None)
    return _render(
        request,
        "tokens.html",
        page="tokens",
        tokens=rows,
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
        _, plaintext = db.create_token(conn, name, list(db.DEFAULT_RING_SCOPES))
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
    conn = _conn(request)
    try:
        clearing = str(form.get("clear") or "")
        if str(form.get("refresh") or "") == "models":
            await openrouter.fetch_models(conn, force=True)
            model_now = db.get_config(conn, "model") or request.app.state.cfg.model
            await openrouter.fetch_providers(conn, model_now, force=True)
        elif clearing in db.CONFIGURABLE:
            db.clear_config(conn, clearing)
        else:
            chosen_model = str(form.get("model") or "").strip()
            if chosen_model:
                db.set_config(conn, "model", chosen_model)

            # Checkbox values arrive in DOM order, which the up and down buttons control, so
            # the submitted order is the routing order.
            # Only real provider names. A rendering bug once put stringified objects in
            # these values, and without this they would have been written straight into the
            # routing config, where they would silently match no endpoint.
            known = {
                p["name"]
                for p in await openrouter.fetch_providers(
                    conn, db.get_config(conn, "model") or request.app.state.cfg.model
                )
            }
            order = [str(v) for v in form.getlist("provider_order") if str(v) in known]
            allow_fallbacks = bool(form.get("allow_fallbacks"))
            provider_config = openrouter.build_provider_config(order, allow_fallbacks)
            if provider_config:
                db.set_config(conn, "provider", json.dumps(provider_config))
            else:
                db.clear_config(conn, "provider")

            for spec in SETTINGS_FIELDS:
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


# A signet: the band, and the seal face you press. Inline SVG so there is no binary asset to
# manage and it stays crisp at any size.
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" fill="none"/>'
    '<circle cx="16" cy="20" r="8.5" fill="none" stroke="#c1272d" stroke-width="3.5"/>'
    '<rect x="10" y="3" width="12" height="10" rx="1.5" fill="#c1272d"/>'
    "</svg>"
)


async def favicon(request: Request) -> Response:
    return Response(
        FAVICON,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
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
        Route("/tokens", tokens, methods=["GET"]),
        Route("/tokens", create_token, methods=["POST"]),
        Route("/tokens/{token_id:int}/revoke", revoke_token, methods=["POST"]),
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings", save_settings, methods=["POST"]),
        Route("/google/connect", google_connect, methods=["POST"]),
        Route("/google/callback", google_callback, methods=["GET"], name="google_callback"),
        Route("/google/disconnect", google_disconnect, methods=["POST"]),
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
