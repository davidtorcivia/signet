"""The result shapes the Pebble app will accept.

These matter more than typical serialisation tests because the failure is remote and silent:
the app decodes `semanticResult` with kotlinx.serialization's default `Json`, which does not
ignore unknown keys. An extra field does not degrade, it throws on the phone. And since
`Response` is what reaches the watch, getting this wrong means the watch shows nothing.
"""

from __future__ import annotations

import json

from signet import coreschema

# Exact field sets from ToolCallResult.kt. Optional fields may be absent; nothing else may
# ever appear.
ALLOWED = {
    "Response": {"type", "text", "question"},
    "SupportingData": {"type", "summary", "assistiveOnly", "question"},
    "CalendarEventCreation": {"type", "title", "startTime", "endTime", "location"},
    "TaskCreation": {"type", "title", "deadline", "localReminderId", "notifyBeforeMillis"},
    "ListItemCreation": {"type", "content", "listUsed", "remindAt", "resolvedListId"},
    "ActionLogged": {"type", "toolName", "title", "success", "body"},
    "GenericFailure": {"type", "userErrorMessage", "llmRecoverable", "forceFallbackTool"},
}


def assert_valid(semantic: dict) -> None:
    variant = semantic["type"]
    assert variant in ALLOWED, f"unknown variant {variant}"
    extra = set(semantic) - ALLOWED[variant]
    assert not extra, f"{variant} carries fields the app will reject: {sorted(extra)}"


def test_every_builder_emits_only_known_fields():
    for semantic in (
        coreschema.response("Saved."),
        coreschema.response("Yes.", question="Did it rain?"),
        coreschema.supporting_data("three notes matched"),
        coreschema.calendar_event("Coffee", "2026-08-04T15:00:00", "2026-08-04T16:00:00"),
        coreschema.calendar_event("Coffee", "2026-08-04T15:00:00", "2026-08-04T16:00:00", "Cafe"),
        coreschema.task_created("Call the lab", "2026-08-05"),
        coreschema.list_item("fixer", list_used="darkroom"),
        coreschema.action_logged("home.lights", "Lights off", True),
        coreschema.generic_failure("Calendar is not connected", llm_recoverable=False),
    ):
        assert_valid(semantic)


def test_result_carries_both_structured_keys():
    """The app reads output and semanticResult with getValue(), which throws on a missing
    key. A result with _meta.coreSchema and only one of them is worse than no _meta at all."""
    result = coreschema.result("Saved.", coreschema.response("Saved."))
    payload = result.model_dump(by_alias=True, exclude_none=True)

    assert payload["_meta"] == {"coreSchema": 1}
    assert isinstance(payload["_meta"]["coreSchema"], int)
    assert set(payload["structuredContent"]) == {"output", "semanticResult"}
    assert payload["content"][0]["text"] == "Saved."


def test_result_is_json_serialisable():
    result = coreschema.result("done", coreschema.action_logged("x", "y", True))
    json.dumps(result.model_dump(by_alias=True, exclude_none=True))


def test_watch_answer_and_model_answer_can_differ():
    """`output` goes to the on-device model, `text` goes to the watch. Keeping them separate
    is how a long answer stays readable on a watch face without truncating what the model
    sees."""
    result = coreschema.result(
        "It rained 12mm on Tuesday, the wettest day of the week.",
        coreschema.response("12mm on Tuesday."),
    )
    payload = result.model_dump(by_alias=True, exclude_none=True)
    assert payload["structuredContent"]["output"].startswith("It rained")
    assert payload["structuredContent"]["semanticResult"]["text"] == "12mm on Tuesday."
