"""Boot-time configuration checks.

The theme: signet refuses to start rather than starting into a state where `capture` throws.
Capture is the product promise (`docs/01-design-options.md`, tier 1) and the ring gets no
second chance — a dropped note is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signet.config import ConfigError, load

GOOD_TOKEN = "x" * 48


def test_missing_token_refuses_to_start(tmp_path: Path):
    with pytest.raises(ConfigError, match="SIGNET_TOKEN is not set"):
        load({"SIGNET_DATA_DIR": str(tmp_path)})


def test_short_token_refuses_to_start(tmp_path: Path):
    """The token cannot be rotated from the phone, so a weak one is a lasting problem."""
    with pytest.raises(ConfigError, match="characters"):
        load({"SIGNET_TOKEN": "short", "SIGNET_DATA_DIR": str(tmp_path)})


def test_unusable_data_dir_refuses_to_start(tmp_path: Path):
    """Regression: the first erebus deploy came up healthy with an unwritable bind mount and
    only failed on the first real capture, as a thrown tool call. Fail at boot instead."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("i am a file", encoding="utf-8")

    with pytest.raises(ConfigError, match="SIGNET_DATA_DIR"):
        load({"SIGNET_TOKEN": GOOD_TOKEN, "SIGNET_DATA_DIR": str(blocker / "data")})


def test_writable_data_dir_is_accepted_and_probe_is_cleaned_up(tmp_path: Path):
    cfg = load({"SIGNET_TOKEN": GOOD_TOKEN, "SIGNET_DATA_DIR": str(tmp_path / "data")})
    assert cfg.data_dir.is_dir()
    assert list(cfg.data_dir.iterdir()) == [], "the write probe must not be left behind"
    assert cfg.journal_path.name == "journal.jsonl"
