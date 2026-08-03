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
import secrets
import statistics
import time
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

        stats = {
            "captures_today": captures_today,
            "requests_today": requests_today,
            "errors_today": errors_today,
            "spend_today": db.spend_today(conn),
            "cap": cfg.daily_cost_cap_usd,
            "journal_total": journal_total,
            "journal_week": journal_week,
            "latency": [
                {
                    "verb": verb,
                    "n": len(values),
                    "p50": int(statistics.median(values)),
                    "p95": int(sorted(values)[max(0, int(len(values) * 0.95) - 1)]),
                }
                for verb, values in sorted(by_verb.items())
            ],
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
    conn = _conn(request)
    try:
        if query:
            entries = db.search_journal(conn, query, limit=100)
        else:
            entries = list(conn.execute("SELECT * FROM journal ORDER BY created_at DESC LIMIT 100"))
    finally:
        conn.close()
    return _render(request, "journal.html", page="journal", entries=entries, q=query)


async def tokens(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        rows = list(conn.execute("SELECT * FROM tokens ORDER BY created_at"))
    finally:
        conn.close()
    new_token = request.session.pop("new_token", None)
    return _render(request, "tokens.html", page="tokens", tokens=rows, new_token=new_token)


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
        google_ready = bool(db.get_config(conn, "google_client_id"))
        google_connected = google.connected(conn)

        current_model = db.get_config(conn, "model") or cfg.model
        models = await openrouter.fetch_models(conn)
        providers = await openrouter.fetch_providers(conn, current_model)
        selected_providers, allow_fallbacks = openrouter.read_provider_config(
            db.get_config(conn, "provider")
        )
        # Ticked providers first, in their saved order, so the list reads as the routing order.
        ordered = [p for p in selected_providers if p in providers]
        ordered += [p for p in providers if p not in selected_providers]
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
        google_connected=google_connected,
        models=models,
        model_ids={m.id for m in models},
        current_model=current_model,
        providers=ordered,
        selected_providers=selected_providers,
        allow_fallbacks=allow_fallbacks,
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
            order = [str(v) for v in form.getlist("provider_order") if v]
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


def _redirect_uri(request: Request) -> str:
    """Must match a redirect URI registered on the Google OAuth client exactly."""
    return str(request.url_for("google_callback"))


async def google_connect(request: Request) -> Response:
    if not _authed(request):
        return RedirectResponse("/app/login", status_code=303)
    conn = _conn(request)
    try:
        client_id = db.get_config(conn, "google_client_id")
    finally:
        conn.close()
    if not client_id:
        request.session["settings_errors"] = ["Set a Google client ID first."]
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
