#!/usr/bin/env python3
"""Drive the Pebble app's 2025-06-18 handshake against a running signet, from outside.

Stdlib only — no venv, no jq, no pip. Runs anywhere python3 does, which is the point: it is
the check you run against the tunnel from whatever box you happen to be on.

    python3 scripts/legacy_handshake.py https://signet.example.com "$SIGNET_TOKEN"

Exit code 0 means the ring's protocol era is served correctly over that URL.

This mirrors tests/test_handshake_legacy.py. The in-process tests prove the server is right;
this proves the *path to it* is right — the tunnel and the edge in between.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PEBBLE_PROTOCOL_VERSION = "2025-06-18"
# Anything slower than this through the tunnel means edge buffering, not slow code.
STALL_THRESHOLD_S = 10.0

# Cloudflare's bot rules 403 the default `Python-urllib/<ver>` User-Agent before the request
# ever reaches the tunnel, which reads exactly like signet rejecting the token. Verified
# 2026-08-03: urllib's default -> 403, while curl, an absent UA, and `Ktor client` (what the
# Pebble app sends) all -> 200. So the ring is unaffected; only this script needed a UA.
USER_AGENT = "signet-legacy-handshake/0.1"

failures: list[str] = []
session_id: str | None = None


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' - {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def post(
    base: str,
    token: str,
    body: dict,
    *,
    expect_status: tuple[int, ...] = (200,),
    expect_json: bool = True,
) -> tuple[dict | None, dict, float]:
    """`expect_status`/`expect_json` differ for notifications: a JSON-RPC notification has no
    id and therefore no response, so the transport answers 202 Accepted with an empty body."""
    global session_id
    data = json.dumps(body).encode()
    request = urllib.request.Request(base.rstrip("/") + "/mcp", data=data, method="POST")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode()
            headers = {k.lower(): v for k, v in response.headers.items()}
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        headers = {k.lower(): v for k, v in exc.headers.items()}
        status = exc.code
    elapsed = time.monotonic() - started

    if "mcp-session-id" in headers:
        session_id = headers["mcp-session-id"]

    method = body.get("method")
    check(
        status in expect_status,
        f"HTTP {'/'.join(str(s) for s in expect_status)} for {method}",
        f"got {status}",
    )
    ctype = headers.get("content-type", "")
    # The whole reason the tunnel path works: single JSON, never an SSE stream.
    check("text/event-stream" not in ctype, f"{method} is not SSE-framed", ctype)
    check(
        elapsed < STALL_THRESHOLD_S,
        f"{method} returned promptly",
        f"{elapsed * 1000:.0f} ms",
    )

    if not expect_json:
        return None, headers, elapsed

    check("application/json" in ctype, f"{method} is application/json", ctype)
    try:
        return json.loads(raw), headers, elapsed
    except json.JSONDecodeError:
        check(False, "response body parses as JSON", raw[:200])
        return None, headers, elapsed


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SIGNET_TOKEN", "")
    if not token:
        print("No token. Pass it as argv[2] or set SIGNET_TOKEN.", file=sys.stderr)
        return 2

    # ASCII only in output: this runs on Windows consoles (cp1252) as well as on erebus.
    print(f"\nsignet legacy-handshake check -> {base}\n")

    print("[1] initialize")
    body, _, _ = post(
        base,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PEBBLE_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pebble-sim", "version": "0.8.3"},
            },
        },
    )
    check(bool(body) and "result" in body, "initialize returned a result, not an error")
    if body and "result" in body:
        result = body["result"]
        check(
            result.get("protocolVersion") == PEBBLE_PROTOCOL_VERSION,
            "negotiated 2025-06-18",
            str(result.get("protocolVersion")),
        )
        check(bool(result.get("instructions")), "instructions present")
        check(result.get("serverInfo", {}).get("name") == "signet", "serverInfo is signet")

    print("[2] notifications/initialized")
    post(
        base,
        token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        expect_status=(200, 202),
        expect_json=False,
    )

    print("[3] tools/list")
    body, _, _ = post(base, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    check(bool(body) and "result" in body, "tools/list returned a result, not an error")
    if body and "result" in body:
        result = body["result"]
        # The Kotlin client literally has TODO("Handle pagination") here.
        check("nextCursor" not in result, "no nextCursor in tools/list")
        names = [t.get("name") for t in result.get("tools", [])]
        check("capture" in names, "capture is offered", str(names))
        # A ~1B on-device model chooses from this list; too many tools degrade it badly.
        check(len(names) <= 8, "tool count stays small", f"{len(names)} tools")

    print("[4] tools/call capture")
    body, _, _ = post(
        base,
        token,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "capture",
                "arguments": {"text": "handshake test from legacy_handshake.py"},
            },
        },
    )
    # Fail loudly rather than skipping the checks below. A JSON-RPC error here still arrives
    # as HTTP 200 with a JSON body, so without this the run reports "all checks passed" while
    # capture is actually throwing — which is exactly what a permission-denied /data looked
    # like on first deploy.
    check(
        bool(body) and "result" in body,
        "tools/call returned a result, not an error",
        json.dumps(body.get("error")) if body and "error" in body else "",
    )
    if body and "result" in body:
        check(
            not body["result"].get("isError"),
            "capture did not report isError",
            json.dumps(body["result"].get("structuredContent", {}))[:200],
        )
        result = body["result"]
        meta = result.get("_meta", {})
        check(meta.get("coreSchema") == 1, "_meta.coreSchema == 1", str(meta))
        structured = result.get("structuredContent", {})
        # ToolCallResult.kt getValue()s both keys; a missing one throws on the phone.
        check("output" in structured, "structuredContent.output present")
        check("semanticResult" in structured, "structuredContent.semanticResult present")
        semantic = structured.get("semanticResult", {})
        check(semantic.get("type") == "Response", "semanticResult.type == Response")
        check(
            set(semantic) <= {"type", "text", "question"},
            "no unknown keys in semanticResult",
            str(sorted(semantic)),
        )

    print("\n[5] auth rejects a bad token")
    saved, globals()["session_id"] = session_id, None
    request = urllib.request.Request(base.rstrip("/") + "/mcp", data=b"{}", method="POST")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Authorization", "Bearer definitely-not-the-token")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            check(False, "bad token rejected", f"got {response.status}")
    except urllib.error.HTTPError as exc:
        check(exc.code == 401, "bad token rejected with 401", f"got {exc.code}")
        ctype = {k.lower(): v for k, v in exc.headers.items()}.get("content-type", "")
        check("application/json" in ctype, "401 body is JSON, not HTML", ctype)
    globals()["session_id"] = saved

    print()
    if failures:
        print(f"FAILED - {len(failures)} check(s): {', '.join(failures)}\n")
        return 1
    print("All checks passed. The ring's protocol era is served correctly over this URL.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
