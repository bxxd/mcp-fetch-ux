# Developer Guide

## Design Principles

**ONE TRUE PATH**: One way to do each thing. CLI calls the handler. Handler calls the client. No alternate code paths, no fallbacks. The browser engine is selected *once at startup* via `FETCH_UX_ENGINE` — that's mode selection, not a runtime fallback, so the principle holds within a process.

**KISS**: Simplest solution that works. The clipboard approach — select-all + copy, then read the clipboard back — replaces hundreds of lines of DOM traversal, Shadow DOM piercing, and Readability extraction.

**SEPARATION OF CONCERNS**: Library fetches. Handler formats. Server routes. Tools describe. Each file does one thing.

**HEXAGONAL — two ports**:
- *Transport*: `mcp_fetch_ux/` wraps the `fetch_ux/` core — swap transport without touching the fetcher.
- *Browser*: `fetch_ux/engines/` (`BrowserEngine`) — swap Firefox ↔ Chrome via `FETCH_UX_ENGINE` without touching the extraction core. The core (`client.py`) holds zero browser-specific code; adapters own the lifecycle + clipboard read-back.

**PRIMARY SOURCES**: The browser renders the page. The clipboard captures what it renders. No intermediate summarization, no LLM interpretation. Raw content to the model.

**DON'T CHANGE SHIT THAT WORKS**: The clipboard approach works on Shadow DOM, web components, SPAs, and static pages. Don't add Readability back as a "better" path — it fails on the hard cases that motivated this project.

## Architecture

```
mcp-fetch-ux/
├── src/
│   ├── fetch_ux/              Pure library (no MCP deps)
│   │   ├── __init__.py
│   │   ├── client.py          FetchClient — engine-agnostic fetch + extraction core
│   │   └── engines/           Browser-engine port + adapters
│   │       ├── __init__.py    BrowserEngine protocol + make_engine factory
│   │       ├── base.py        BaseEngine — shared interactions (overlay dismiss, select+copy)
│   │       ├── invisible.py   InvisibleEngine — stealth Firefox (default)
│   │       └── chrome.py      ChromeEngine — real Chrome via Patchright
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
Poll: client._capture()  [clipboard capture; DOM inner_text if the copy didn't land]
    │  400-600ms jittered intervals, stable after 2 consecutive equal readings
    │  Empty never counts as stable — an unrendered page isn't a settled one
    │  Max 12 iterations (~6s)
    │
    ▼
Re-dismiss overlays (catch late SPA consent banners) → re-capture if one was removed
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

`page.innerText('body')` and `window.getSelection().toString()` don't cross Shadow DOM boundaries (closed roots especially). The clipboard does — Ctrl+A/Ctrl+C copies the *rendered* selection, including (closed) Shadow DOM. Tested on:

- Roche pipeline (web components + Shadow DOM) — gets the full pipeline
- Wikipedia (static HTML) — gets full article
- GitHub (React SPA) — gets full README

**Reading the clipboard back is engine-specific** — that's the crux of why this is a port (`engine._read_clipboard()`):
- **chrome**: `navigator.clipboard.readText()` — Chromium honors the context's `clipboard-read` permission grant.
- **invisible (Firefox)**: reads the X11 clipboard out-of-process with `xclip`. Reading it back *inside the page* (the obvious route — `readText()`, or pasting into a `<textarea>` and reading `.value`) goes through `page.evaluate`, which runs in Firefox's main world and is **blocked by strict site CSP** (`script-src` without `unsafe-eval` — Hacker News always, Roche intermittently): it throws `call to eval() blocked by CSP` or hangs the whole fetch to timeout. A subprocess reading the OS clipboard never touches the page, so CSP can't reach it.

### The stale-clipboard trap

A failed Ctrl+C is **silent**. The OS clipboard is one buffer per display that outlives pages, contexts and fetches, and nothing clears it — so reading it back after a copy that didn't land returns *the previous fetch's text*, under the current page's title, with no error anywhere. This shipped as a live bug: congress.gov bill pages (a JS app that navigates after first paint) defeat Ctrl+A/Ctrl+C every time, so three fetches in one session returned the body of the page fetched before them.

Two guards, both in `BaseEngine.capture_text`:

1. **Sentinel** — a unique token is stamped on the clipboard before every copy. If it survives, the copy didn't land and capture returns `""`, never a stale buffer. Any exception in the window (a navigation destroying the execution context — Chrome's `page.evaluate` raises *"Execution context was destroyed"*) is the same answer.
2. **Lock** (`_clipboard_lock`) — serializes stamp→copy→read. The clipboard is **not** isolated per browser context; without this, two concurrent captures read each other's page text (reproduced 6/6 before the lock).

`""` is not the end of the road: `FetchClient._capture` then reads `body.inner_text()`. That misses closed Shadow DOM, which is why it's the fallback and not the path — but it belongs to the page actually loaded, and it is what makes congress.gov work.

### Why poll for stability

JS frameworks render asynchronously. The DOM loads fast but data arrives via API calls and renders into components over 1-3 seconds. Polling detects when rendering is done without waiting for all network traffic (analytics, fonts, tracking pixels add seconds of pointless delay).

### Browser lifecycle

The engine (selected by `FETCH_UX_ENGINE`) launches one warm browser for the process lifetime via `engine.start()`. Each fetch gets a page from `engine.new_page()` and closes it (`_closer()`); the browser stays warm. `FETCH_UX_RECYCLE_TTL` (default 1 day) recycles the whole browser on a timer — `_recycle_browser()` stops then restarts the engine — so a flagged session/fingerprint can't poison us forever. The same path runs if a page close hangs (stuck renderer).

Both engines keep state warm across fetches:
- **invisible**: one Firefox with one shared context, created once at `start()`; each fetch opens a *tab* (`context.new_page`). Fresh coherent fingerprint per `start()`/recycle. Opening a target in this Firefox intermittently deadlocks (~7.5% under heavy churn) — a healthy creation returns in <0.7s, a bad one hangs forever — so `new_page()` bounds each attempt at 6s and retries; the retry returns in ~0.5s.
- **chrome**: one persistent context = a warm, **shared** cookie jar (cookies shared across fetches — fine for public pages; the recycle TTL bounds it).

Concurrent fetches are gated by an `asyncio.Semaphore` sized from `engine.concurrency`: **invisible = 3** (an on-demand pool of reused tabs; only tab *creation* is serialized), **chrome = 3** (one shared persistent context; page work parallelizes safely). Neither number covers the clipboard — that is a single OS resource for the whole display, guarded separately by `_clipboard_lock`.

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

Discovery is implemented with Playwright locators from the Python/driver side (not `page.evaluate`). This is deliberately CSP-safe and works on strict sites such as news.ycombinator.com that block `'unsafe-eval'`. The older JS-in-page version was replaced for this reason.

### Cookie/consent dismissal

Lives on the engine (`BaseEngine.dismiss_overlays`, shared by both adapters). Tries common selectors (OneTrust handlers; `Accept All`/`Reject All`/`Accept`/`OK`/`I Agree`; `[id*='cookie'] button`, `[id*='consent'] button`), 300ms visibility timeout each, clicks the first visible, returns `True` if it dismissed one.

Called twice by the core: right after `goto`, and again **after content renders** — that second call catches late, JS-injected banners (e.g. Quest) that don't exist yet at `goto`; if it removes one, the core re-captures so the banner text isn't in the output. (A third time before actions, if any.)

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
XAI_API_KEY=xai-...   # for read_blocked_webpage (Grok-backed fetch)

# .env (prod)
PORT=5007
XAI_API_KEY=xai-...
```

`XAI_API_KEY` is only touched by `read_blocked_webpage`; `read_webpage` runs
keyless. Without the key, `read_blocked_webpage` returns an error and
everything else works.

## Operations

**Engine**: both envs pin `FETCH_UX_ENGINE=chrome` in `.env`. The invisible
engine (the code default) cannot cold-start on this host — its stealth-Firefox
asset (`firefox-7`) 404s from the upstream `feder-cr/invisible_playwright`
release. Un-pinning requires the fork to host its own browser asset first.

**Chrome install**: the chrome engine launches real Google Chrome
(`channel="chrome"`, `/opt/google/chrome`), not Playwright's bundled Chromium.
`make chrome` installs it system-wide.

**Display**: Chrome runs headed (the stealth posture) and needs an X display.
`make server` wraps the process in `xvfb-run -a`; the prod unit
`idio-mcp-fetch-prod` carries `xvfb-run -a` in its ExecStart. If `read_webpage`
errors with "Missing X server or $DISPLAY", look for an orphaned `Xvfb`
process first — a stale display from a dead instance can make `xvfb-run`
hand the new server no display at all. Kill the orphan and restart.

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

- **Memory**: one warm browser/context for the process; each fetch opens/closes a page. `FETCH_UX_RECYCLE_TTL` (default 1 day) recycles the whole browser on a timer. The fetch semaphore is sized from `engine.concurrency` (invisible 1, chrome 3). Monitor under heavy load.

- **Timeouts**: 30s default on all Patchright operations. Actions get 5s each. No operation hangs indefinitely.

- **Server restarts**: Stateless HTTP transport — no session to lose. Clients survive server restarts without reconnecting.
