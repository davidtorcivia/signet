"""Deciding what a request means.

Deterministic rules run first because they are free, instant, and predictable. Only what falls
through reaches a model. Rules are data, not code, so they stay editable from the web app.

The router must work with no rules file and no API key. In that state signet still captures and
still searches, which is the floor it should never drop below.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .llm import LLM, LLMUnavailable

logger = logging.getLogger("signet.router")


@dataclass
class Plan:
    capability: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    agent_loop: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "args": self.args,
            "agent_loop": self.agent_loop,
            "reason": self.reason,
        }


# Built in so the router is useful before anyone writes a rules file. `capture` is
# deliberately generous: mistaking a question for a note loses nothing, while mistaking a note
# for a question loses the note.
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "match": "regex",
        "pattern": (
            r"^\s*(remember|note|jot down|write down|make a note)"
            r"\b[:,]?\s*(that\s+)?(?P<rest>.+)"
        ),
        "capability": "journal.write",
        "args": {"text": "{rest}"},
    },
    {
        "match": "regex",
        "pattern": r"^\s*(what|when|where|who|how)\b.*\b(did|have)\s+i\b\s*(?P<rest>.*)",
        "capability": "journal.search",
        "args": {"query": "{rest}"},
    },
    {
        "match": "regex",
        "pattern": r"\bsearch (my )?(notes|journal)\s+(for\s+)?(?P<rest>.+)",
        "capability": "journal.search",
        "args": {"query": "{rest}"},
    },
    {
        "match": "regex",
        "pattern": (
            r"^\s*(search|google|look up|find out)"
            r"\b\s*(the web for\s+|online for\s+)?(?P<rest>.+)"
        ),
        "capability": "search.web",
        "args": {"query": "{rest}"},
    },
]

CLASSIFY_SYSTEM = (
    "You route a voice request to exactly one tool. Reply with the tool name and its "
    "arguments. If nothing fits, use journal.write to save the text verbatim. Never invent a "
    "tool that is not listed."
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "capability": {"type": "string"},
        "args": {"type": "object", "additionalProperties": True},
        "reason": {"type": "string"},
    },
    "required": ["capability", "args", "reason"],
    "additionalProperties": False,
}


def load_rules(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return list(DEFAULT_RULES)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        logger.exception("rules file is not valid YAML, falling back to defaults")
        return list(DEFAULT_RULES)
    if not isinstance(loaded, list):
        logger.warning("rules file must be a list, falling back to defaults")
        return list(DEFAULT_RULES)
    return loaded


def apply_rules(text: str, rules: list[dict[str, Any]]) -> Plan | None:
    for rule in rules:
        pattern = rule.get("pattern", "")
        kind = rule.get("match", "regex")
        if kind == "prefix":
            if not text.lower().startswith(pattern.lower()):
                continue
            groups = {"rest": text[len(pattern) :].strip()}
        else:
            found = re.search(pattern, text, re.IGNORECASE)
            if not found:
                continue
            groups = {k: (v or "").strip() for k, v in found.groupdict().items()}

        args = {
            key: (value.format(**groups, text=text) if isinstance(value, str) else value)
            for key, value in (rule.get("args") or {}).items()
        }
        return Plan(
            capability=rule["capability"],
            args=args,
            reason=f"rule: {pattern[:40]}",
        )
    return None


class Router:
    def __init__(
        self, llm: LLM, rules_path: Path | None = None, system_prompt: str | None = None
    ) -> None:
        self.llm = llm
        self.rules_path = rules_path
        self.system_prompt = system_prompt or CLASSIFY_SYSTEM

    async def route(self, text: str, catalogue: list[tuple[str, str]]) -> Plan:
        """`catalogue` is [(capability name, description)] the caller is allowed to use."""
        rules = load_rules(self.rules_path)
        planned = apply_rules(text, rules)
        if planned is not None:
            logger.info("routed by rule -> %s", planned.capability)
            return planned

        if not self.llm.available or not catalogue:
            # No model and no matching rule. Keeping it rather than guessing is the right
            # failure: a note in the journal can be found later, a dropped one cannot.
            return Plan(
                capability="journal.write",
                args={"text": text},
                reason="no model available, captured instead",
            )

        listing = "\n".join(f"- {name}: {description}" for name, description in catalogue)
        try:
            completion = await self.llm.complete(
                system=self.system_prompt,
                user=f"Tools:\n{listing}\n\nRequest: {text}",
                schema=CLASSIFY_SCHEMA,
                max_tokens=300,
                timeout=20.0,
            )
        except LLMUnavailable:
            logger.warning("classifier unavailable, capturing instead")
            return Plan(
                capability="journal.write",
                args={"text": text},
                reason="classifier unavailable, captured instead",
            )

        data = completion.data or {}
        chosen = data.get("capability")
        known = {name for name, _ in catalogue}
        if chosen not in known:
            logger.warning("classifier picked unknown capability %r", chosen)
            return Plan(
                capability="journal.write",
                args={"text": text},
                reason=f"classifier picked unknown tool {chosen!r}, captured instead",
            )

        return Plan(
            capability=chosen,
            args=data.get("args") or {},
            reason=data.get("reason", "classified"),
        )
