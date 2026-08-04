"""The Pebble app's private result contract. This is also how signet reaches the watch.

An ordinary MCP result works fine: the app hands `content` text to its on-device model. But a
result carrying `_meta.coreSchema` plus `structuredContent.semanticResult` is rendered as a
first-class item in the app's feed (Todos / Notes / Answers / Actions) instead of as a raw
blob, and `Response` is what the app surfaces in its completion notification, which is what
lands on the watch.

**Getting on the watch is therefore a property of the semanticResult, not a separate feature.**
Every capability picks the variant that matches what it did:

**The headline the user actually reads is `actionText()`, and most variants hard-code it.**
Read out of `ui/components/chat/SemanticResultUtil.kt`:

| Variant | Headline the user sees | Expanded detail |
| --- | --- | --- |
| `Response` | the fixed word **"Replied"** | `text`, only if expanded |
| `ListItemCreation` | **"Noted"**, or "Noted to {listUsed}" | `content` |
| `TaskCreation` | "Reminder added" | title and deadline |
| `CalendarEventCreation` | **"Added {title} at {date}, {time}"** | title, time, place |
| `SupportingData` | "Gathered info" | `summary` |
| `GenericSuccess` | "Action completed" | none |
| `GenericFailure` | "Action failed" | `userErrorMessage` |
| `ActionLogged` | **`title`, our own words** | none |

That table is the whole reason answers stopped reaching the watch. `Response` looks like the
obvious variant for an answer and renders as the single word "Replied"; the answer itself is
hidden behind a tap. Anything the user must read at a glance therefore goes in a variant whose
headline carries our text: `ActionLogged.title`, or one of the creation variants that build a
sentence from real fields.

Because these are read on a watch face, that text should be one short sentence.

See `docs/00-research.md` §2.

Verified against `mcp/src/commonMain/kotlin/coredevices/mcp/data/ToolCallResult.kt` in
coredevices/mobileapp on 2026-08-03:

- The sealed `SemanticResult` hierarchy uses kotlinx.serialization's **default** class
  discriminator, i.e. the key is `"type"` — no `@JsonClassDiscriminator` and no
  `classDiscriminator` override at the decode site.
- Variant names are their `@SerialName` strings, which match the Kotlin class names.

Every other client (Claude Code, hermes, anything modern) ignores `_meta` and
`structuredContent` it doesn't recognise, so emitting this costs nothing elsewhere.
"""

from __future__ import annotations

from typing import Any

import mcp.types as types

CORE_SCHEMA_VERSION = 1


def response(text: str, question: str | None = None) -> dict[str, Any]:
    """The agent's spoken answer. Surfaced in the app's completion notification."""
    out: dict[str, Any] = {"type": "Response", "text": text}
    if question is not None:
        out["question"] = question
    return out


def supporting_data(
    summary: str | None, *, assistive_only: bool = False, question: str | None = None
) -> dict[str, Any]:
    """Data for the on-device model to keep thinking with, rather than a final answer."""
    out: dict[str, Any] = {
        "type": "SupportingData",
        "summary": summary,
        "assistiveOnly": assistive_only,
    }
    if question is not None:
        out["question"] = question
    return out


def calendar_event(
    title: str, start_time: str, end_time: str, location: str | None = None
) -> dict[str, Any]:
    """Confirmation of a created event. Times are ISO-8601 strings."""
    out: dict[str, Any] = {"title": title, "startTime": start_time, "endTime": end_time}
    if location is not None:
        out["location"] = location
    return {"type": "CalendarEventCreation", **out}


def task_created(
    title: str,
    deadline: str,
    *,
    local_reminder_id: str | None = None,
    notify_before_millis: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "TaskCreation", "title": title, "deadline": deadline}
    if local_reminder_id is not None:
        out["localReminderId"] = local_reminder_id
    if notify_before_millis is not None:
        out["notifyBeforeMillis"] = notify_before_millis
    return out


def list_item(
    content: str,
    *,
    list_used: str | None = None,
    remind_at: str | None = None,
    resolved_list_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "ListItemCreation", "content": content}
    if list_used is not None:
        out["listUsed"] = list_used
    if remind_at is not None:
        out["remindAt"] = remind_at
    if resolved_list_id is not None:
        out["resolvedListId"] = resolved_list_id
    return out


def answer(text: str, *, limit: int = 140) -> dict[str, Any]:
    """An answer the user reads at a glance.

    Deliberately not `Response`, whose headline is the fixed word "Replied" with the text
    hidden behind a tap. `ActionLogged.title` is rendered verbatim, so it is the only variant
    that puts an actual answer in front of someone looking at a watch.

    Trimmed on a word boundary as a backstop; the prompt already asks for one or two
    sentences, and a headline is not the place to discover it did not comply.
    """
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "..."
    return action_logged("signet", text or "No answer", success=True)


def action_logged(tool_name: str, title: str, success: bool, body: str = "") -> dict[str, Any]:
    """The catch-all for "signet did a thing" that has no richer variant."""
    return {
        "type": "ActionLogged",
        "toolName": tool_name,
        "title": title,
        "success": success,
        "body": body,
    }


def generic_failure(
    user_error_message: str | None = None,
    *,
    llm_recoverable: bool = False,
    force_fallback_tool: bool = False,
) -> dict[str, Any]:
    """`llm_recoverable=False` tells the on-device model that retrying is pointless."""
    return {
        "type": "GenericFailure",
        "userErrorMessage": user_error_message,
        "llmRecoverable": llm_recoverable,
        "forceFallbackTool": force_fallback_tool,
    }


def result(
    output: str,
    semantic: dict[str, Any],
    *,
    is_error: bool = False,
) -> types.CallToolResult:
    """Build a CallToolResult in the coreSchema shape.

    `output` is the string handed to the on-device LLM; `semantic` is what the feed renders.
    `content` carries the same string so non-Pebble clients still see something useful.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=output)],
        structuredContent={"output": output, "semanticResult": semantic},
        meta={"coreSchema": CORE_SCHEMA_VERSION},
        isError=is_error,
    )


def headline(semantic: dict[str, Any]) -> str:
    """What the user actually reads, mirroring the app's `actionText()`.

    Kept here so there is one place that knows the app hard-codes most of these, and so tests
    can assert on the words a person sees rather than on a field they never look at.
    """
    variant = semantic.get("type")
    if variant == "ActionLogged":
        return str(semantic.get("title", ""))
    if variant == "ListItemCreation":
        listed = semantic.get("listUsed")
        return f"Noted to {listed}" if listed else "Noted"
    if variant == "CalendarEventCreation":
        return f"Added {semantic.get('title', '')} to calendar"
    if variant == "TaskCreation":
        return "Reminder added"
    if variant == "SupportingData":
        return "Gathered info"
    if variant == "Response":
        return "Replied"
    if variant == "GenericFailure":
        return "Action failed"
    if variant == "GenericSuccess":
        return "Action completed"
    if variant == "MessageSent":
        return f"Messaged {semantic.get('recipientName', '')}"
    return ""
