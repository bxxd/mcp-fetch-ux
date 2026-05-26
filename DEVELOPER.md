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
│   │   └── client.py          FetchClient — Patchright + clipboard extraction
│   │                           (real Chrome via Patchright, one persistent context)
│   └── mcp_fetch_ux/          MCP server
│       ├── __init__.py
│       ├── __main__.py         Entry point
│       ├── server_http.py      Streamable HTTP transport (Starlette + Uvicorn)
│       ├── handlers.py         Tool routing, shared FetchClient lifecycle
│       └── tools.py            Tool schema definitions
├── tests/
│   ├── conftest.py             Fixtures (local HTTP server, FetchClient)
│   ├── fixtures/               Test HTML pages
│   ├── test_unit.py            Pure function tests (no browser)
│   ├── test_client.py          Integration tests (real Patchright)
│   └── test_server.py          HTTP endpoint tests
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
Human-like pause (200-600ms random)
    │
    ▼
Poll: Ctrl+A → Ctrl+C → clipboard.readText()
    │  400-600ms jittered intervals, stable after 2 consecutive equal readings
    │  Max 12 iterations (~6s)
    │
    ▼
Run actions (if any) — click, fill, wait, select, scroll
    │  Mouse moves to target before click, random delays between actions
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

One real Chrome (via Patchright `launch_persistent_context(channel="chrome")`) for the lifetime of the server process. One persistent context → a warm, **shared** cookie jar. Each fetch opens and closes a *page*, not a context; the context stays warm. `FETCH_UX_COOKIE_TTL` (default 1 day) throws the jar away on a timer via the recycle path, so a flagged session can't poison it forever.

Trade-off of one shared context: cookies are shared across fetches (fine for public-page fetching; the daily TTL bounds it). The alternative — a fresh context per fetch — would give isolation but forfeits the warm jar and is not Patchright's recommended stealth config.

`asyncio.Semaphore(3)` limits concurrent fetches to prevent memory spikes. The per-fetch page release is built by `_closer()`.

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

## MCP Transport

**Streamable HTTP** — stateless, no sessions. Server restarts don't break clients.

- Transport: `StreamableHTTPServerTransport` with `is_json_response_enabled=True`
- Server runs with `stateless=True` — skips MCP initialization handshake
- Long-lived transport in Starlette lifespan, mounted at `/mcp/`
- Claude Code config: `"type": "http"` in `.mcp.json`

Why not SSE? SSE sessions are in-memory. Server restart = dead sessions = `"Could not find session"` errors. Clients must manually reconnect. Streamable HTTP has no persistent sessions — each request is self-contained.

## Browser Fingerprint Evasion

**Coherence over masking.** Real Google Chrome via Patchright's documented "completely undetected" config. We deliberately do **not** spoof anything by hand — layering manual patches on top of Patchright creates *inconsistencies* a fingerprinter catches (e.g. a faked NVIDIA WebGL string on a machine with no GPU). Real Chrome just tells the truth; there's nothing to catch.

| Concern | How it's handled |
|---------|------------------|
| CDP `Runtime.enable` / `Console.enable` leaks | Patchright (isolated execution contexts) |
| `navigator.webdriver`, `--enable-automation` | Patchright sets the right args itself |
| UA / `Sec-Ch-Ua` / client hints | real Chrome sends consistent, real values — no override |
| `window.chrome`, plugins, permissions, hardware | real Chrome — nothing to mask |
| WebGL renderer | real GPU via ANGLE/EGL when `/dev/dri/renderD128` exists (`--use-gl=angle --use-angle=gl-egl`); else Chrome's default |
| Viewport | `no_viewport=True` — the real window size |
| Instant click timing | mouse moves to target over 5-15 steps before click |
| Fixed polling intervals | jittered 400-600ms per poll, random pauses between actions |

Config = `channel="chrome"`, `launch_persistent_context`, `no_viewport=True`, no header/UA/init-script injection. Requires real Chrome (`patchright install chrome`) and a display (run under `xvfb`; `FETCH_UX_HEADLESS=1` forces headless, which is more detectable).

This beats Cloudflare/Datadome/Kasada-class walls. It does **not** beat Google's reCAPTCHA-Enterprise SERP wall — that scores the whole environment (GPU + Google account history + behavior), which a coherent-but-automated session can't satisfy from a single box. For a Firefox-based engine that does pass reCAPTCHA v3 (`invisible_playwright`), see the follow-up branch.

**Not fixable here**: datacenter-IP reputation (use a residential egress).

## Configuration

```bash
# .env (dev)
PORT=5017

# .env (prod)
PORT=5007
```

No API keys. No secrets.

## Commands

```bash
make setup     # poetry install + patchright install chrome  (server runs under xvfb)
make install   # setup + install 'fetch' CLI to ~/.local/bin/
make server    # start (nohup, PID file, health check)
make kill      # stop
make logs      # tail
make ping      # health check
```

## Tests

```bash
poetry run pytest tests/ -v          # all tests (~80s, launches real browser)
poetry run pytest tests/test_unit.py # fast, no browser
```

- `test_unit.py` — pure functions: `_read_download`, FetchClient config + GPU args, tool schema, routing
- `test_client.py` — real Patchright against local fixture server: content extraction, JS rendering, cookie dismissal, truncation/pagination, actions, stealth fingerprints, action discovery
- `test_server.py` — HTTP endpoints: ping, MCP initialize, shutdown, 404

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
| `/mcp/` | POST | MCP message handling (streamable HTTP) |
| `/shutdown` | POST | Graceful shutdown (closes browser) |

## Known behaviors

- **Shadow DOM / SPAs**: Clipboard captures everything the browser renders, regardless of Shadow DOM boundaries or JS framework.

- **File downloads**: Detected via Patchright's download event. Content returned as text. Binary files (PDFs) returned with `errors="replace"` — good enough for text extraction, not for binary fidelity.

- **Bot detection**: real Chrome + Patchright present a coherent fingerprint (no manual masking). Beats Cloudflare/Datadome-class walls; not Google's reCAPTCHA-Enterprise SERP. Datacenter IPs still get blocked regardless — use a residential egress.

- **Memory**: one persistent context for the process; each fetch opens/closes a page. `FETCH_UX_COOKIE_TTL` recycles the whole context (fresh profile) on a timer. Semaphore caps at 3 concurrent. Monitor under heavy load.

- **Timeouts**: 30s default on all Patchright operations. Actions get 5s each. No operation hangs indefinitely.

- **Server restarts**: Stateless HTTP transport — no session to lose. Clients survive server restarts without reconnecting.
