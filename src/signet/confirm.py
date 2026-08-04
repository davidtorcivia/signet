"""Answering a pending approval by voice.

There is no way for signet to reach the phone first. MCP is request and response, so the
server only ever speaks when spoken to, and the `question` field on a result turned out to be
a feed item title rather than a prompt. But the ring talks to signet constantly, so a waiting
approval can simply be answered on the next press.

The phrases are deliberately narrow and anchored to the whole utterance. "Yes" on its own is
almost never a note; "yes, remember to call the lab" is, and must not be read as consent to
something destructive.
"""

from __future__ import annotations

import re

CONFIRM = re.compile(
    r"^\s*(?:yes|yep|yeah|yup|confirm(?:ed)?|approve[d]?|do it|go ahead|please do|"
    r"that's right|correct|ok|okay|sure)\s*[.!]*\s*$",
    re.IGNORECASE,
)

DECLINE = re.compile(
    r"^\s*(?:no|nope|nah|deny|denied|cancel|stop|don'?t|forget it|never\s?mind)"
    r"\s*[.!]*\s*$",
    re.IGNORECASE,
)


def is_confirmation(text: str) -> bool:
    return bool(CONFIRM.match(text or ""))


def is_refusal(text: str) -> bool:
    return bool(DECLINE.match(text or ""))
