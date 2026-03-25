# mcp-fetch-ux

Playwright-based web fetch MCP server with Readability extraction.

## Why

Claude Code's built-in `WebFetch` has no timeout — a single hung request stalls the entire agent permanently. This replaces it with a proper fetch that:

- **Renders JavaScript** (Playwright/Chromium) — works on JS-heavy sites
- **30-second timeout** — never hangs
- **Readability extraction** — clean markdown, no nav/ads/hidden content
- **Pagination** — large pages read in chunks via `start_index`
- **No LLM** — raw content to the model, no extra cost

## Quick Start

```bash
make setup     # Install deps + Playwright browsers
make server    # Start on port 5006 (or PORT from .env)
./cli https://www.roche.com/solutions/pipeline   # Test directly
```

## Tools

### `fetch(url, max_length?, start_index?, raw?)`

Fetch URL, render JS, extract content as markdown.

- `url` — required
- `max_length` — max chars to return (default 5000, for pagination)
- `start_index` — resume from this char index after truncation
- `raw` — return raw HTML instead of extracted markdown

## Architecture

```
fetch_ux/           Pure library (no MCP deps)
  client.py         FetchClient — Playwright + Readability

mcp_fetch_ux/       MCP server
  server_http.py    HTTP/SSE transport (Starlette + Uvicorn)
  handlers.py       Tool routing
  tools.py          Tool definitions
```

## Ports

| Environment | Port |
|-------------|------|
| Dev         | 5006 |
| Prod        | 5006 |

## MCP Configuration

```json
{
  "mcpServers": {
    "fetch-ux": {
      "type": "sse",
      "url": "http://127.0.0.1:5006/sse"
    }
  }
}
```
