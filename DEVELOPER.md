# Developer Guide

## Design Principles

**ONE TRUE PATH**: One way to do each thing. CLI calls the handler. Handler calls the client. No alternate code paths, no fallbacks. If it's wrong, you'll know fast.

**KISS**: Simplest solution that works. The clipboard approach is one line (`navigator.clipboard.readText()`) that replaces hundreds of lines of DOM traversal, Shadow DOM piercing, and Readability extraction for most pages.

**SEPARATION OF CONCERNS**: Library fetches. Handler formats. Server routes. Tools describe. Each file does one thing.

**HEXAGONAL**: `fetch_ux/` is the core — no MCP deps, no server deps. `mcp_fetch_ux/` is the adapter. Swap the transport without touching the fetcher.

**PRIMARY SOURCES**: The browser renders the page. The clipboard captures what it renders. No intermediate summarization, no LLM interpretation. Raw content to the model.

**DON'T CHANGE SHIT THAT WORKS**: The clipboard approach works on Shadow DOM, web components, SPAs, and static pages. Don't add Readability back as a "better" path — it fails on the hard cases that motivated this project.

## Architecture

```
mcp-fetch-ux/
├── src/
│   ├── fetch_ux/              Pure library (no MCP deps)
│   │   ├── __init__.py
│   │   └── client.py          FetchClient — Playwright + clipboard extraction
│   └── mcp_fetch_ux/          MCP server
│       ├── __init__.py
│       ├── __main__.py         Entry point
│       ├── server_http.py      HTTP/SSE transport (Starlette + Uvicorn)
│       ├── handlers.py         Tool routing, shared FetchClient lifecycle
│       └── tools.py            Tool schema definitions
├── cli                         Test script (calls handler directly)
├── Makefile
├── pyproject.toml
└── .env                        PORT config (gitignored)
```

### Content extraction pipeline

```
page.goto(url, wait_until="domcontentloaded")
    │
    ▼
Dismiss cookie/consent overlays
    │
    ▼
Poll: Ctrl+A → Ctrl+C → clipboard.readText()
    │  500ms intervals, stable after 2 consecutive equal readings (≥1s)
    │  Max 6s
    │
    ▼
Run actions (if any) — click, fill, wait, select, scroll
    │
    ├─ Action triggers download? → return file content
    │
    ▼
Discover available actions (buttons, links, inputs)
    │
    ▼
Return text + available actions + pagination
```

### Why clipboard

`page.innerText('body')` and `window.getSelection().toString()` don't cross Shadow DOM boundaries. The clipboard API does — it captures what a human gets with Ctrl+A, Ctrl+C. Tested on:

- Roche pipeline (web components + Shadow DOM) — gets all 131 drugs
- Wikipedia (static HTML) — gets full article
- GitHub (React SPA) — gets full README

### Why poll for stability

JS frameworks render asynchronously. The DOM loads fast but data arrives via API calls and renders into components over 1-3 seconds. Polling detects when rendering is done without waiting for all network traffic (analytics, fonts, tracking pixels add seconds of pointless delay).

### Browser lifecycle

One Chromium instance for the lifetime of the server process. Each fetch creates a fresh `BrowserContext` (isolated cookies, storage, permissions) and closes it in a `finally` block. Clean state per request without browser restart overhead.

`asyncio.Semaphore(3)` limits concurrent fetches to prevent memory spikes.

### Actions

Actions run after initial content capture. Supported:

| Action | Parameters | Example |
|--------|-----------|---------|
| `click` | `selector` | `{"action": "click", "selector": "text=Download CSV"}` |
| `fill` | `selector`, `value` | `{"action": "fill", "selector": "input[name=q]", "value": "query"}` |
| `wait` | `selector` or `timeout` | `{"action": "wait", "selector": ".results"}` |
| `select` | `selector`, `value` | `{"action": "select", "selector": "select#lang", "value": "en"}` |
| `scroll` | `direction` | `{"action": "scroll", "direction": "bottom"}` |

If a click triggers a file download (CSV, PDF, etc.), the file content is returned instead of page text.

### Action discovery

After fetching, the tool scans for visible interactive elements and appends them as hints:

```
Available actions on this page:
  - click: "button:has-text('Download current view as CSV')"
  - click: "button:has-text('Phase')"
  - fill: "search" (search)
  - click: "a:has-text('Careers')" → https://careers.roche.com
```

The agent sees what's available and can call again with actions. Two-step flow: discover, then act.

### Cookie/consent dismissal

Runs automatically before content capture AND before actions. Tries common selectors:
- `#onetrust-reject-all-handler`, `#onetrust-accept-btn-handler`
- `button:has-text('Accept All')`, `Reject All`, `Accept`, `OK`, `I Agree`
- `[id*='cookie'] button`, `[class*='consent'] button`

300ms visibility timeout per selector. Clicks first match.

## Configuration

```bash
# .env
PORT=5006
```

No API keys. No secrets.

## Commands

```bash
make setup     # poetry install + playwright install chromium
make install   # setup + install 'fetch' CLI to ~/.local/bin/
make server    # start (nohup, PID file, health check)
make kill      # stop
make logs      # tail
make ping      # health check
```

## CLI

Installed as `fetch` via `make install`.

```bash
fetch <url>                                             # full page content
fetch <url> --max 5000                                  # limit output
fetch <url> --click "text=Download current view as CSV" # interact
fetch <url> --click "#btn" --fill "input[name=q]=test"  # multiple actions
fetch <url> --raw                                       # raw HTML
fetch <url> -o output.txt                               # save to file
```

CLI defaults to unlimited output. MCP tool defaults to 50K chars with line-boundary truncation and pagination.

### Downloads

PDF downloads are automatically converted to text via `pdftotext`. Other binary formats (pptx, xlsx, zip, etc.) return a message with a `curl` command the agent can use to save the file locally.

## Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/ping` | GET | Health check |
| `/sse` | GET | SSE connection (MCP protocol) |
| `/messages` | POST | MCP message handling |
| `/shutdown` | POST | Graceful shutdown (closes browser) |

## Known behaviors

- **Shadow DOM / SPAs**: Clipboard captures everything the browser renders, regardless of Shadow DOM boundaries or JS framework.

- **File downloads**: Detected via Playwright's download event. Content returned as text. Binary files (PDFs) returned with `errors="replace"` — good enough for text extraction, not for binary fidelity.

- **Headless detection**: Default user-agent mimics Chrome on macOS. Some sites still detect headless. Reddit blocks datacenter IPs regardless of user-agent — use Grok for those.

- **Memory**: Each fetch creates and destroys a browser context. Semaphore caps at 3 concurrent. Monitor under heavy load.

- **Timeouts**: 30s default on all Playwright operations. Actions get 5s each. No operation hangs indefinitely.
