"""The Pebble app's private result contract.

An ordinary MCP result works fine — the app hands `content` text to its on-device model.
But a result carrying `_meta.coreSchema` plus `structuredContent.semanticResult` is rendered
as a first-class item in the app's feed (Todos / Notes / Answers / Actions) instead of as a
raw blob. See `docs/00-research.md` §2.

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
