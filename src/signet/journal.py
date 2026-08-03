"""The P0 journal: append-only JSONL.

This is the one thing in signet that must never fail (`01-design-options.md`, tier 1:
"capture-to-inbox with zero failure modes"). So it does the least possible work —
no parsing, no LLM, no schema beyond three fields.

P1 replaces this with SQLite + FTS5 and imports whatever landed here; see
`docs/03-implementation-plan.md` P1-1. Keep the record shape stable until then.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_write_lock = threading.Lock()


def append(path: Path, text: str, **extra: Any) -> dict[str, Any]:
    """Append one capture and return the stored record."""
    record: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "received_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "text": text,
    }
    record.update(extra)

    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    return record


def read_all(path: Path) -> list[dict[str, Any]]:
    """Read every record. Test/inspection helper — not a query API."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
