"""Entry point: `signet` or `python -m signet`."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_or_exit
from .server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_or_exit()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, access_log=True)


if __name__ == "__main__":
    main()
