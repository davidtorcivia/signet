"""Test fixtures.

Everything runs against a **real uvicorn server on a real socket**, not an in-process ASGI
transport. Two reasons: the Streamable HTTP session manager needs its lifespan actually run,
and the point of these tests is the bytes on the wire — response headers included.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from signet.config import Config
from signet.server import create_app

TOKEN = "test-" + secrets.token_urlsafe(48)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(token=TOKEN, data_dir=tmp_path, host="0.0.0.0", port=_free_port())


@pytest.fixture
def server(cfg: Config) -> Iterator[str]:
    """Run signet in a background thread; yield its base URL."""
    config = uvicorn.Config(
        create_app(cfg),
        host="127.0.0.1",
        port=cfg.port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            server.should_exit = True
            raise RuntimeError("signet did not start within 15s")
        time.sleep(0.02)

    try:
        yield f"http://127.0.0.1:{cfg.port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
