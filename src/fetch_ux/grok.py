"""Grok-backed fetch for blocked sites.

Ported from mcp-grok-search-ux (GrokClient.fetch) — xai-sdk agentic web_search
pointed at one URL. Beats IP blocks and paywalls the browser engines can't
(Bloomberg, Seeking Alpha, Reddit). Costs money per call; the browser engine
stays the hot path.
"""

import os
from dataclasses import dataclass

from xai_sdk import Client
from xai_sdk.chat import user
from xai_sdk.tools import web_search

GROK_MODEL = "grok-4.3"
PRICE_INPUT_PER_M = 1.25
PRICE_OUTPUT_PER_M = 2.50
PRICE_TOOL_CALL = 0.005

DEFAULT_FETCH_PROMPT = "Extract full article: author, date, key arguments, specific data points, and conclusions."


@dataclass
class GrokFetchResult:
    content: str
    citations: list[str]
    tool_count: int
    cost: float


def _calc_cost(usage, tool_count: int) -> float:
    reasoning_tokens = getattr(usage, "reasoning_tokens", 0) or 0
    input_tokens = usage.total_tokens - usage.completion_tokens - reasoning_tokens
    return (
        (input_tokens * PRICE_INPUT_PER_M / 1_000_000)
        + ((usage.completion_tokens + reasoning_tokens) * PRICE_OUTPUT_PER_M / 1_000_000)
        + (tool_count * PRICE_TOOL_CALL)
    )


def grok_fetch(url: str, prompt: str | None = None) -> GrokFetchResult:
    """Fetch a URL through Grok's server-side web search. Blocking call."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        raise ValueError("XAI_API_KEY not found in environment")

    prompt = prompt or DEFAULT_FETCH_PROMPT
    full_prompt = f"{prompt}\n\nContext: {url}" if url.startswith("http") else prompt

    client = Client(api_key=api_key)
    chat = client.chat.create(
        model=GROK_MODEL,
        tools=[web_search(enable_image_understanding=False)],
        reasoning_effort="high",
    )
    chat.append(user(full_prompt))

    tool_count = 0
    content_parts: list[str] = []
    for response, chunk in chat.stream():
        tool_count += len(chunk.tool_calls)
        if chunk.content:
            content_parts.append(chunk.content)

    return GrokFetchResult(
        content="".join(content_parts),
        citations=list(response.citations) if response.citations else [],
        tool_count=tool_count,
        cost=_calc_cost(response.usage, tool_count),
    )
