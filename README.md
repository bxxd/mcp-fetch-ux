# mcp-fetch-ux

MCP server that fetches web pages using a real browser. Renders JavaScript, pierces Shadow DOM, returns clean text. Can interact with pages — click buttons, fill forms, download files.

Built because Claude Code's `WebFetch` [hangs indefinitely](https://github.com/anthropics/claude-code/issues/34565) on slow sites with no timeout, and existing fetch tools can't see inside Shadow DOM.

## How it works

1. **Playwright** launches headless Chromium and navigates to the URL
2. Dismisses cookie/consent overlays automatically
3. Waits for JS to render (polls until page content stabilizes)
4. **Ctrl+A, Ctrl+C** — uses the clipboard API to capture all visible text, including content inside Shadow DOM
5. Discovers available actions (buttons, links, inputs) and returns them as hints
6. Optionally runs actions (click, fill, wait) — if a click triggers a download, returns the file content

No LLM in the loop. No API costs. 30-second timeout.

## Quick start

```bash
git clone https://github.com/bxxd/mcp-fetch-ux.git
cd mcp-fetch-ux
make install   # install deps + Chromium + 'fetch' CLI to ~/.local/bin/
```

```bash
# Basic fetch
fetch https://www.roche.com/solutions/pipeline

# Click a button to download CSV
fetch https://roche.com/solutions/pipeline --click "button:has-text('Download current view as CSV')"

# Save to file
fetch https://roche.com/solutions/pipeline --click "button:has-text('Download current view as CSV')" -o pipeline.csv

# More content (default 50K chars via MCP, unlimited via CLI)
fetch https://en.wikipedia.org/wiki/Likelihood_ratio --max 100000
```

Start the MCP server:
```bash
make server    # start on port 5006
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
| `actions` | array | none | Actions to perform before capturing (click, fill, wait, select, scroll) |
| `max_length` | int | 5000 | Max characters to return |
| `start_index` | int | 0 | Resume from this index (pagination) |
| `raw` | bool | false | Return raw HTML instead of text |

### Two-step interaction

First call returns page content + available actions:

```
Contents of https://www.roche.com/solutions/pipeline:
Title: Roche | Product Development Pipeline

RG7716
faricimab
Vabysmo
macular edema secondary to branch retinal vein occlusion (BRVO)
...

---
Available actions on this page:
  - click: "button:has-text('Download current view as CSV')"
  - click: "button:has-text('Phase')"
  - fill: "search" (search)
```

Second call with actions gets the data:

```json
{
  "url": "https://www.roche.com/solutions/pipeline",
  "actions": [{"action": "click", "selector": "button:has-text('Download current view as CSV')"}]
}
```

Returns the full CSV (51K chars, 131 pipeline entries with descriptions).

## Why not just httpx/curl?

They don't render JavaScript. Roche's pipeline page returns an empty shell — the drug data loads via JS into Shadow DOM web components. `curl` gets nothing. This tool gets what a human sees.

## Why not innerText or Readability?

`page.innerText('body')` and `document.getSelection()` don't cross Shadow DOM boundaries. Readability can't see JS-rendered content. The clipboard API captures exactly what a user gets when they Ctrl+A, Ctrl+C in Chrome — the only reliable way to get all visible text from modern web pages.

## Why not crawl4ai?

Tested [crawl4ai](https://github.com/unclecode/crawl4ai) (50K+ stars) on the same Roche pipeline page. It returns 7,220 chars with zero drug names — can't see inside Shadow DOM. This tool returns 11,224 chars with all 131 drugs.

## Performance

Browser launches once at server startup and stays alive. Each fetch opens a fresh context, fetches, closes.

| Phase | Time |
|-------|------|
| Navigation | ~1-2s |
| JS render + stabilization | ~1-3s |
| Total per fetch | ~3-5s |

Concurrency limited to 3 simultaneous fetches.

## Dependencies

- [Playwright](https://playwright.dev/) — browser automation
- [Starlette](https://www.starlette.io/) + [Uvicorn](https://www.uvicorn.org/) — HTTP/SSE server
- [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) — Model Context Protocol

## License

MIT
