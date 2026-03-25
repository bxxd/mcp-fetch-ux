# mcp-fetch-ux

MCP server that fetches web pages using a real browser. Renders JavaScript, pierces Shadow DOM, returns clean text.

Built because Claude Code's `WebFetch` [hangs indefinitely](https://github.com/anthropics/claude-code/issues/34565) on slow sites with no timeout.

## How it works

1. **Playwright** launches headless Chromium and navigates to the URL
2. Waits for JS to render (polls until page content stabilizes)
3. Dismisses cookie/consent overlays
4. **Ctrl+A, Ctrl+C** — uses the clipboard API to capture all visible text, including content inside Shadow DOM
5. Returns plain text with pagination support

No LLM in the loop. No API costs. 30-second timeout.

## Quick start

```bash
git clone https://github.com/bxxd/mcp-fetch-ux.git
cd mcp-fetch-ux
make setup     # install deps + Chromium
make server    # start on port 5006
```

Test directly:
```bash
./cli https://www.roche.com/solutions/pipeline
```

## MCP configuration

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

## Tool

### `fetch`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | URL to fetch |
| `max_length` | int | 5000 | Max characters to return |
| `start_index` | int | 0 | Resume from this index (pagination) |
| `raw` | bool | false | Return raw HTML instead of text |

Large pages are paginated automatically. When truncated, the response includes the `start_index` for the next call.

## Why not just httpx/curl?

They don't render JavaScript. A page like Roche's pipeline (`roche.com/solutions/pipeline`) returns an empty shell — the drug data loads via JS into Shadow DOM web components. `curl` gets nothing. This tool gets what a human sees.

## Why not Readability alone?

Readability extracts article content from HTML — great for blog posts and news. But it can't see inside Shadow DOM, and it misses content rendered by JavaScript frameworks. The clipboard approach captures everything the browser renders, regardless of how it got there.

## Why clipboard?

`page.innerText('body')` and `document.getSelection()` don't cross Shadow DOM boundaries. The clipboard API does — it captures exactly what a user gets when they Ctrl+A, Ctrl+C in Chrome. This is the only reliable way to get all visible text from pages with web components.

## Performance

Browser launches once at server startup and stays alive. Each fetch opens a fresh browser context (clean state), fetches, closes the context.

| Phase | Time |
|-------|------|
| Navigation | ~1-2s |
| JS render + stabilization | ~1-3s |
| Total per fetch | ~3-5s |

Concurrency limited to 3 simultaneous fetches.

## Dependencies

- [Playwright](https://playwright.dev/) — browser automation
- [Readability](https://github.com/alan-j-hu/readabilipy) — article extraction (fallback for `raw` mode)
- [Starlette](https://www.starlette.io/) + [Uvicorn](https://www.uvicorn.org/) — HTTP/SSE server
- [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) — Model Context Protocol

## License

MIT
