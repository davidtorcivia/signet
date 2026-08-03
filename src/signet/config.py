"""Environment configuration. Fails fast and loudly — a half-configured signet
that starts and then 500s on the first ring press is worse than one that won't boot.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    data_dir: Path
    host: str
    port: int
    openrouter_api_key: str | None = None
    model: str = "deepseek/deepseek-v4-flash-0731"
    daily_cost_cap_usd: float = 2.0

    @property
    def journal_path(self) -> Path:
        """P0's flat file. Still referenced so its contents can be imported on first boot
        after the SQLite migration; see db.import_legacy_journal."""
        return self.data_dir / "journal.jsonl"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "signet.db"


def load(env: dict[str, str] | None = None) -> Config:
    src = os.environ if env is None else env

    token = src.get("SIGNET_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "SIGNET_TOKEN is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if len(token) < 32:
        raise ConfigError(
            f"SIGNET_TOKEN is only {len(token)} characters. This token sits in a phone app "
            "and cannot be rotated from there; use at least 32 characters of real entropy."
        )

    data_dir = Path(src.get("SIGNET_DATA_DIR", "data")).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"SIGNET_DATA_DIR {data_dir} cannot be created: {exc}") from exc

    # Prove we can actually write before accepting a single request. Capture is the one thing
    # that must never fail (`docs/01-design-options.md`, tier 1), so an unwritable data dir has
    # to stop the process at boot — where Docker's healthcheck and `compose ps` will show it —
    # rather than surfacing as a thrown tool call on the first ring press. A bind mount owned
    # by a different uid than the container user is the way this happens in practice.
    probe = data_dir / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ConfigError(
            f"SIGNET_DATA_DIR {data_dir} is not writable by uid {os.getuid()}: {exc}\n"
            "If this is a bind mount, chown it to the container's user, or set the compose "
            "`user:` to the owner of the host directory."
            if hasattr(os, "getuid")
            else f"SIGNET_DATA_DIR {data_dir} is not writable: {exc}"
        ) from exc

    return Config(
        token=token,
        data_dir=data_dir,
        host=src.get("SIGNET_HOST", "0.0.0.0"),
        port=int(src.get("SIGNET_PORT", "8300")),
        # Optional on purpose. Without it signet is still a recorder and a search engine,
        # which is most of the value, so a missing key degrades rather than blocks.
        openrouter_api_key=(src.get("OPENROUTER_API_KEY") or "").strip() or None,
        model=src.get("SIGNET_MODEL", "deepseek/deepseek-v4-flash-0731"),
        daily_cost_cap_usd=float(src.get("SIGNET_DAILY_COST_CAP_USD", "2.00")),
    )


_cached: Config | None = None


def set_cached(cfg: Config) -> None:
    """Publish the active config so capabilities can reach it without threading it through
    every call. Set once at startup and in tests."""
    global _cached
    _cached = cfg


def load_cached() -> Config:
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def load_or_exit() -> Config:
    try:
        return load()
    except ConfigError as exc:
        print(f"signet: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
