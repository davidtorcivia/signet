"""Web search.

The one capability that pulls untrusted text into signet. Its results carry `untrusted=True`
so callers know to fence them, and it is deliberately not `destructive`, since reading is safe;
the danger is what a model might be persuaded to do next by what it read.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import coreschema
from ..capability import Capability
from ..config import load_cached
from ..envelope import Outcome, Request
from ..search import Exa, SearchUnavailable


class WebSearchArgs(BaseModel):
    query: str = Field(description="What to search the web for.")
    results: int = Field(default=5, ge=1, le=10)


async def web_search(request: Request, args: WebSearchArgs) -> Outcome:
    cfg = load_cached()
    exa = Exa(cfg.exa_api_key)

    if not exa.available:
        return Outcome(
            output="Web search is not configured.",
            semantic=coreschema.generic_failure("Web search is not set up.", llm_recoverable=False),
            is_error=True,
        )

    try:
        response = await exa.search(args.query, results=args.results)
    except SearchUnavailable as exc:
        return Outcome(
            output=f"Web search failed: {exc}",
            semantic=coreschema.generic_failure("Search failed.", llm_recoverable=True),
            is_error=True,
        )

    if not response.results:
        return Outcome(
            output=f"No web results for {args.query!r}.",
            semantic=coreschema.response("Nothing found."),
            data={"results": [], "untrusted": True},
        )

    from ..search import fence

    return Outcome(
        # Fenced here rather than at the call site, so there is no way to use these results
        # without the untrusted marker attached.
        output=fence(response.results),
        semantic=coreschema.supporting_data(
            f"{len(response.results)} web results", assistive_only=True
        ),
        data={
            "results": [
                {"title": r.title, "url": r.url, "published": r.published} for r in response.results
            ],
            "untrusted": True,
        },
        cost_usd=response.cost_usd,
        untrusted=True,
    )


CAPABILITIES = [
    Capability(
        name="search.web",
        description="Search the web for current information the user's own notes would not have.",
        schema=WebSearchArgs,
        handler=web_search,
        scopes=("search:read",),
        exposure="internal",
        tier="fast",
        returns_untrusted=True,
    )
]
