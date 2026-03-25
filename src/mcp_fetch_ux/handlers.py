"""Tool routing and execution."""

import logging
import time

from fetch_ux import FetchClient

logger = logging.getLogger("mcp_fetch_ux")

# Shared client — browser launched once, reused across requests
_client: FetchClient | None = None


async def get_client() -> FetchClient:
    global _client
    if _client is None:
        _client = FetchClient()
        await _client.start()
    return _client


async def shutdown_client():
    global _client
    if _client:
        await _client.stop()
        _client = None


async def call_tool(name: str, arguments: dict) -> str:
    if name != "webfetch":
        raise ValueError(f"Unknown tool: {name}")

    return await handle_fetch(**arguments)


async def handle_fetch(
    url: str,
    actions: list[dict] | None = None,
    max_length: int = 50000,
    start_index: int = 0,
    raw: bool = False,
) -> str:
    """Fetch URL with Playwright, optionally interact, return content."""
    client = await get_client()

    start = time.time()
    result = await client.fetch(
        url=url,
        actions=actions,
        max_length=max_length,
        start_index=start_index,
        raw=raw,
    )
    elapsed = time.time() - start

    logger.info(
        f"fetch url={url} status={result.status} "
        f"{'download=' + result.download_filename + ' ' if result.download_filename else ''}"
        f"title={result.title!r} "
        f"length={result.length} truncated={result.truncated} "
        f"time={elapsed:.1f}s"
    )

    if result.status >= 400:
        return f"HTTP {result.status} fetching {url}\n\n{result.content}"

    if not result.content:
        return f"No content extracted from {url}"

    lines = []
    if result.download_filename:
        lines.append(f"Downloaded: {result.download_filename} ({result.length} chars)")
    else:
        lines.append(f"Contents of {url}:")
        if result.title:
            lines.append(f"Title: {result.title}")
    lines.append("")
    lines.append(result.content)

    if result.actions_available:
        lines.append("")
        lines.append("---")
        lines.append("Available actions on this page:")
        for a in result.actions_available:
            lines.append(f"  - {a}")

    return "\n".join(lines)
