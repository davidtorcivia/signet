"""Routing.

The bias throughout: when the router is unsure, it captures. Mistaking a question for a note
costs a search later. Mistaking a note for a question loses the note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signet.llm import LLM, Completion, LLMUnavailable
from signet.router import DEFAULT_RULES, Plan, Router, apply_rules, load_rules

CATALOGUE = [
    ("journal.write", "Save a note."),
    ("journal.search", "Search notes."),
    ("calendar.create_event", "Create an event."),
]


class FakeLLM(LLM):
    def __init__(self, data=None, fail=False):
        super().__init__(api_key="test-key")
        self._data = data
        self._fail = fail
        self.calls = 0

    async def complete(self, system, user, *, schema=None, max_tokens=600, timeout=None):
        self.calls += 1
        if self._fail:
            raise LLMUnavailable("provider down")
        return Completion(
            text="", data=self._data, model="fake", tokens_in=10, tokens_out=5, cost_usd=0.0
        )


def test_remember_rule_extracts_the_note():
    plan = apply_rules("remember that the darkroom timer needs a bulb", DEFAULT_RULES)
    assert plan.capability == "journal.write"
    assert plan.args["text"] == "the darkroom timer needs a bulb"


def test_remember_rule_without_that():
    plan = apply_rules("note: buy more fixer", DEFAULT_RULES)
    assert plan.capability == "journal.write"
    assert plan.args["text"] == "buy more fixer"


def test_recall_question_routes_to_search():
    plan = apply_rules("what did I say about the enlarger", DEFAULT_RULES)
    assert plan.capability == "journal.search"
    assert "enlarger" in plan.args["query"]


def test_unmatched_text_falls_through():
    assert apply_rules("turn the kitchen lights off", DEFAULT_RULES) is None


async def test_no_model_captures_rather_than_guessing():
    router = Router(LLM(api_key=None))
    plan = await router.route("turn the kitchen lights off", CATALOGUE)
    assert plan.capability == "journal.write"
    assert plan.args["text"] == "turn the kitchen lights off"


async def test_classifier_result_is_used():
    llm = FakeLLM(
        {"capability": "calendar.create_event", "args": {"title": "coffee"}, "reason": "r"}
    )
    router = Router(llm)
    plan = await router.route("put coffee with Sarah on Friday", CATALOGUE)
    assert plan.capability == "calendar.create_event"
    assert plan.args == {"title": "coffee"}


async def test_hallucinated_tool_is_rejected():
    """A classifier that invents a tool must not reach the registry with a bad name."""
    llm = FakeLLM({"capability": "launch.missiles", "args": {}, "reason": "r"})
    router = Router(llm)
    plan = await router.route("do something", CATALOGUE)
    assert plan.capability == "journal.write"
    assert "unknown tool" in plan.reason


async def test_classifier_failure_still_captures():
    router = Router(FakeLLM(fail=True))
    plan = await router.route("do something odd", CATALOGUE)
    assert plan.capability == "journal.write"


async def test_rules_short_circuit_before_the_model():
    llm = FakeLLM({"capability": "calendar.create_event", "args": {}, "reason": "r"})
    router = Router(llm)
    plan = await router.route("remember that I parked on level 3", CATALOGUE)
    assert plan.capability == "journal.write"
    assert llm.calls == 0, "a matched rule must not cost a model call"


def test_missing_rules_file_uses_defaults(tmp_path: Path):
    assert load_rules(tmp_path / "nope.yaml") == DEFAULT_RULES


def test_broken_rules_file_falls_back_to_defaults(tmp_path: Path):
    bad = tmp_path / "rules.yaml"
    bad.write_text("this: [is: not: valid", encoding="utf-8")
    assert load_rules(bad) == DEFAULT_RULES


def test_custom_rules_file_is_used(tmp_path: Path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "- match: prefix\n"
        "  pattern: 'log '\n"
        "  capability: journal.write\n"
        "  args:\n"
        "    text: '{rest}'\n",
        encoding="utf-8",
    )
    plan = apply_rules("log the shutter is sticking", load_rules(rules))
    assert plan.capability == "journal.write"
    assert plan.args["text"] == "the shutter is sticking"


def test_plan_serialises_for_the_feed():
    assert Plan(capability="x", args={"a": 1}).as_dict()["capability"] == "x"


@pytest.mark.parametrize(
    "text",
    ["remember to call the lab", "make a note that the fixer is low", "jot down 400 ISO"],
)
def test_capture_phrasings_all_route_to_the_journal(text: str):
    assert apply_rules(text, DEFAULT_RULES).capability == "journal.write"
