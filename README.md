# mcp-fetch-ux

MCP server that fetches web pages using a **real, stealth browser**. Renders JavaScript, pierces Shadow DOM, returns clean text — and gets through bot walls that block everything else, **including live Google Search results**. Can interact with pages — click buttons, fill forms, download files.

Built because Claude Code's `WebFetch` [hangs indefinitely](https://github.com/anthropics/claude-code/issues/34565) on slow sites with no timeout, and existing fetch tools can't see inside Shadow DOM (or get captcha'd the moment they try).

## How it works

1. A **stealth browser** launches headed under `xvfb` and navigates to the URL — by default a fingerprint-patched Firefox that passes reCAPTCHA; swappable to real Chrome (see [Browser engine](#browser-engine))
2. Dismisses cookie/consent overlays automatically (including late, JS-injected banners)
3. Waits for JS to render (polls until page content stabilizes)
4. **Ctrl+A, Ctrl+C** — selects and copies the rendered page, capturing all visible text including content inside (closed) Shadow DOM
5. Discovers available actions (buttons, links, inputs) and returns them as hints
6. Optionally runs actions (click, fill, wait) — if a click triggers a download, returns the file content

No LLM in the loop. No API costs. 30-second timeout.

## Quick start

```bash
git clone https://github.com/bxxd/mcp-fetch-ux.git
cd mcp-fetch-ux
make install   # deps + the invisible engine (stealth Firefox) + 'fetch' CLI to ~/.local/bin/
# (optional) the chrome engine too:  make chrome
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

`page.innerText('body')` and `document.getSelection()` don't cross Shadow DOM boundaries. Readability can't see JS-rendered content. The clipboard route captures exactly what a user gets when they Ctrl+A, Ctrl+C in a real browser — the only reliable way to get all visible text (including *closed* Shadow DOM) from modern web pages.

## Why not crawl4ai?

Tested [crawl4ai](https://github.com/unclecode/crawl4ai) (50K+ stars) on the same Roche pipeline page. It returns 7,220 chars with zero drug names — can't see inside Shadow DOM. This tool returns 11,224 chars with all 131 drugs.

## Browser engine

The browser is a **swappable engine** — pick with `FETCH_UX_ENGINE`:

| Engine | Best for | How |
|--------|----------|-----|
| **`invisible`** (default) | hard targets, **incl. Google SERP** | Fingerprint-patched Firefox ([invisible_playwright](https://github.com/feder-cr/invisible_playwright)). Patches navigator / GPU / canvas / fonts / audio at the **C++ level** — no JS shims to detect — and passes reCAPTCHA v3 where Chromium-based stealth hits a ceiling. |
| **`chrome`** | Cloudflare / Datadome / Kasada-class walls | Real Google Chrome via Patchright + a warm persistent context. Coherent fingerprint, zero manual masks. Does **not** beat Google's reCAPTCHA SERP. Install its browser with `make chrome`. |

Both run a real browser headed, so they need a display — run under `xvfb` (the systemd unit and `cli` already do). `make setup` installs only the default (invisible) engine's browser; `make chrome` adds Chrome.

| Variable | Default | Effect |
|----------|---------|--------|
| `FETCH_UX_ENGINE` | `invisible` | set to `chrome` for the Patchright/Chrome engine |
| `FETCH_UX_RECYCLE_TTL` | `86400` | seconds before the browser is recycled (rotates cookies / fingerprint); `0` = never |
| `FETCH_UX_HEADLESS` | `0` | `1` → headless (more detectable; headed-under-xvfb is stealthier) — chrome engine |

GPU: with a DRM render node (`/dev/dri/renderD128`) present — e.g. a GPU passed into the container — the chrome engine drives WebGL through it via ANGLE/EGL, so the renderer reports the real GPU instead of `WebGL: false`. No-op without a GPU.

## Performance

The browser launches once at server startup and stays warm; each fetch opens and closes a page.

| Phase | Time |
|-------|------|
| Navigation | ~1-2s |
| JS render + stabilization | ~1-3s |
| Total per fetch | ~3-5s |

Concurrency is per-engine: **invisible** runs fetches one at a time (a single Firefox can't safely open targets in parallel), **chrome** runs up to 3.

## Dependencies

- **Engines** — [invisible_playwright](https://github.com/feder-cr/invisible_playwright) (stealth Firefox, default) · [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) (stealth Chrome)
- [Playwright](https://playwright.dev/) — browser automation
- [Starlette](https://www.starlette.io/) + [Uvicorn](https://www.uvicorn.org/) — HTTP/SSE server
- [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) — Model Context Protocol

## License

MIT
