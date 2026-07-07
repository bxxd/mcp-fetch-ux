"""Tool routing and execution."""

import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

from fetch_ux import FetchClient

logger = logging.getLogger("mcp_fetch_ux")


_DISALLOWED_SUFFIXES = (
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".localhost",
)


class UnsafeUrl(ValueError):
    """Raised when a URL points somewhere we refuse to fetch (loopback, private,
    link-local, metadata services, non-http schemes)."""


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparsable → reject
    return (
        ip.is_loopback
        or ip.is_private          # covers RFC1918, CGNAT 100.64/10, ULA, etc.
        or ip.is_link_local       # 169.254/16 (AWS+GCP+Azure metadata) and fe80::/10
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


async def _validate_url(url: str) -> None:
    """Raise UnsafeUrl if `url` points anywhere we refuse to fetch."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl(f"disallowed scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrl("URL has no host")
    host_lc = host.lower()
    if host_lc == "localhost":
        raise UnsafeUrl(f"disallowed hostname: {host}")
    for suffix in _DISALLOWED_SUFFIXES:
        if host_lc.endswith(suffix):
            raise UnsafeUrl(f"disallowed hostname: {host}")
    # If hostname is itself an IP literal, check directly.
    try:
        ipaddress.ip_address(host)
        if _is_blocked_ip(host):
            raise UnsafeUrl(f"disallowed address: {host}")
        return
    except ValueError:
        pass
    # Resolve DNS off the event loop (getaddrinfo is blocking). Reject if ANY
    # resolved address is blocked.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise UnsafeUrl(f"host did not resolve: {e}") from e
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise UnsafeUrl(f"disallowed address: {ip_str} (for host {host})")

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
    if name != "read_webpage":
        raise ValueError(f"Unknown tool: {name}")

    return await handle_fetch(**arguments)


async def handle_fetch(
    url: str,
    actions: list[dict] | None = None,
    max_length: int = 50000,
    start_index: int = 0,
    raw: bool = False,
) -> str:
    """Fetch URL with real Chrome (Patchright), optionally interact, return content."""
    # Defense in depth: the Rust edge does this too, but anyone hitting the
    # Python worker directly (or via MCP without going through the edge) is
    # still protected here.
    await _validate_url(url)

    client = await get_client()

    start = time.time()
    try:
        result = await client.fetch(
            url=url,
            actions=actions,
            max_length=max_length,
            start_index=start_index,
            raw=raw,
        )
    except TimeoutError as e:
        elapsed = time.time() - start
        logger.error(f"fetch url={url} timed out after {elapsed:.1f}s")
        return f"Error: {e}"
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
