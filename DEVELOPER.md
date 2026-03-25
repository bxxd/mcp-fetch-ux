# Developer Guide

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
├── cli                         Quick test script (no server needed)
├── Makefile
├── pyproject.toml
└── .env                        PORT config (gitignored)
```

### Separation of concerns

- `fetch_ux/` — pure library. No MCP imports. Reusable from CLI, tests, other projects.
- `mcp_fetch_ux/` — MCP server. Thin routing layer that calls `fetch_ux`.
- `server_http.py` — transport only. SSE, health check, shutdown.
- `handlers.py` — tool logic + shared `FetchClient` lifecycle.
- `tools.py` — JSON schema definitions for MCP tool discovery.

### Content extraction pipeline

```
page.goto(url, wait_until="domcontentloaded")
    │
    ▼
Dismiss cookie/consent overlays (button click)
    │
    ▼
Poll loop: Ctrl+A → Ctrl+C → clipboard.readText()
    │  Repeat every 500ms until text length stabilizes
    │  (2 consecutive equal readings after ≥1s)
    │  Max 6s (12 polls)
    │
    ▼
Return text with pagination (start_index / max_length)
```

**Why clipboard, not innerText?** Shadow DOM. `page.innerText('body')` and
`window.getSelection().toString()` don't cross shadow root boundaries. The
clipboard API does — it captures what a human gets with Ctrl+A, Ctrl+C.

**Why poll for stability?** JS frameworks render asynchronously. The DOM loads
fast but data arrives via API calls and renders into components over the next
1-3 seconds. Polling detects when rendering is done without waiting for all
network traffic (which includes analytics, fonts, tracking pixels).

### Browser lifecycle

One Chromium instance lives for the lifetime of the server process. Each fetch
creates a fresh `BrowserContext` (isolated cookies, storage, permissions) and
closes it in a `finally` block. This gives clean state per request without
browser restart overhead.

`asyncio.Semaphore(3)` limits concurrent fetches to prevent memory spikes.

### Cookie/consent dismissal

Tries common selectors in order, clicks the first visible match:
- `button:has-text('Accept')`, `Accept All`, `OK`, `I Agree`
- `[id*='cookie'] button`, `[class*='cookie'] button`, `[id*='consent'] button`

Quick 500ms visibility timeout per selector. Runs before content polling so
overlays don't block the real content.

## Configuration

```bash
# .env
PORT=5006    # Server port (default 5006)
```

No API keys needed. No secrets.

## Commands

```bash
make setup     # poetry install + playwright install chromium
make server    # Start server (nohup, PID file, health check)
make kill      # Stop server
make logs      # Tail logs
make ping      # Health check
```

## Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/ping` | GET | Health check |
| `/sse` | GET | SSE connection (MCP protocol) |
| `/messages` | POST | MCP message handling |
| `/shutdown` | POST | Graceful shutdown (closes browser) |

## Testing

```bash
# Direct fetch (no server needed)
./cli https://example.com
./cli https://example.com 10000    # custom max_length

# With server running
make server
# Connect via MCP client or test with curl:
# SSE connection at http://127.0.0.1:5006/sse
```

## Known behaviors

- **SPA data loading**: Sites that load data via AJAX into web components (like
  Roche's pipeline page) work because the clipboard captures Shadow DOM content.
  The stabilization poll waits for this data to render.

- **Headless detection**: Some sites detect headless browsers. The default
  user-agent mimics Chrome on macOS. For sites that still block, consider
  adding `--disable-blink-features=AutomationControlled` to browser args.

- **Memory**: Each fetch creates and destroys a browser context. The semaphore
  limits concurrent contexts to 3. Monitor memory if running under heavy load.

- **PDF/binary content**: Not handled. Use `raw=true` to get the HTML response
  for non-HTML content types.
