"""Google Calendar over OAuth.

Personal Google accounts cannot use service-account impersonation, so this is the installed-app
OAuth flow: consent once in the admin portal, store the refresh token, mint access tokens
server side from then on. That consent screen is a large part of why the portal exists
(`docs/00-research.md` section 5.7).

Only the calendar scope is requested. signet has no business reading anything else, and a
narrow grant is one less thing to regret.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from . import db

logger = logging.getLogger("signet.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# Read and write events, nothing else. Not calendar.readonly, because creating events is the
# entire point, and not the full calendar scope, which would also allow deleting calendars.
SCOPE = "https://www.googleapis.com/auth/calendar.events"

REFRESH_KEY = "google_refresh_token"
ACCESS_KEY = "google_access_token"
EXPIRY_KEY = "google_access_expires_at"


class GoogleUnavailable(RuntimeError):
    """Not connected, or Google refused. Callers degrade and say so plainly."""


@dataclass
class Event:
    id: str
    summary: str
    start: str
    end: str
    location: str | None = None
    html_link: str | None = None


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                # offline + consent is what actually returns a refresh token. Without them the
                # grant expires in an hour and the ring stops working tomorrow.
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )
    )


async def exchange_code(
    conn: sqlite3.Connection, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    if response.status_code != 200:
        raise GoogleUnavailable(f"token exchange failed: {response.text[:200]}")

    payload = response.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        raise GoogleUnavailable(
            "Google did not return a refresh token. Revoke signet's access in your Google "
            "account and connect again."
        )
    db.set_setting(conn, REFRESH_KEY, refresh)
    _store_access(conn, payload)


def _store_access(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    token = payload.get("access_token", "")
    # A minute of slack, so a token does not expire mid-request.
    expires_at = time.time() + float(payload.get("expires_in", 3600)) - 60
    db.set_setting(conn, ACCESS_KEY, token)
    db.set_setting(conn, EXPIRY_KEY, str(expires_at))


def connected(conn: sqlite3.Connection) -> bool:
    return bool(db.get_setting(conn, REFRESH_KEY, ""))


def disconnect(conn: sqlite3.Connection) -> None:
    for key in (REFRESH_KEY, ACCESS_KEY, EXPIRY_KEY):
        db.set_setting(conn, key, "")


async def access_token(conn: sqlite3.Connection, client_id: str, client_secret: str) -> str:
    """Cached until it expires, then refreshed. The refresh token is the durable credential."""
    cached = db.get_setting(conn, ACCESS_KEY, "")
    expires_at = db.get_setting(conn, EXPIRY_KEY, "0")
    try:
        still_valid = cached and float(expires_at) > time.time()
    except ValueError:
        still_valid = False
    if still_valid:
        return cached

    refresh = db.get_setting(conn, REFRESH_KEY, "")
    if not refresh:
        raise GoogleUnavailable("calendar is not connected")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        raise GoogleUnavailable(f"refresh failed: {response.text[:200]}")
    payload = response.json()
    _store_access(conn, payload)
    return payload.get("access_token", "")


class Calendar:
    def __init__(self, token: str, *, timeout: float = 20.0, transport=None) -> None:
        self.token = token
        self.timeout = timeout
        self.transport = transport  # injection point for tests

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def list_events(
        self, *, calendar_id: str = "primary", days: int = 7, limit: int = 20
    ) -> list[Event]:
        now = datetime.now().astimezone()
        params = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=days)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
        }
        async with self._client() as client:
            response = await client.get(
                f"{CALENDAR_API}/calendars/{calendar_id}/events", params=params
            )
        if response.status_code != 200:
            raise GoogleUnavailable(f"calendar list failed: {response.text[:200]}")
        return [_to_event(item) for item in response.json().get("items", [])]

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        location: str | None = None,
        description: str | None = None,
        calendar_id: str = "primary",
    ) -> Event:
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description

        async with self._client() as client:
            response = await client.post(
                f"{CALENDAR_API}/calendars/{calendar_id}/events", json=body
            )
        if response.status_code not in (200, 201):
            raise GoogleUnavailable(f"event creation failed: {response.text[:200]}")
        return _to_event(response.json())


def _to_event(item: dict[str, Any]) -> Event:
    start = item.get("start", {})
    end = item.get("end", {})
    return Event(
        id=item.get("id", ""),
        summary=item.get("summary", "(no title)"),
        # All-day events carry `date` instead of `dateTime`.
        start=start.get("dateTime") or start.get("date", ""),
        end=end.get("dateTime") or end.get("date", ""),
        location=item.get("location"),
        html_link=item.get("htmlLink"),
    )
