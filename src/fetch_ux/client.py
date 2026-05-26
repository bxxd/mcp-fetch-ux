"""Stealth-Firefox web fetcher with clipboard extraction.

Renders JS, captures visible text (including Shadow DOM) via Ctrl+A/Ctrl+C.
Supports page interactions (click, fill, wait) and file downloads.
30s timeout. No LLM. No cost.

Engine: invisible_playwright — a C++-fingerprint-patched Firefox that passes
reCAPTCHA v3 (including Google SERP), where Chromium-based stealth hits a
ceiling. Drop-in Playwright API. Needs a display — run headed under xvfb.
"""

import asyncio
import logging
import os
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Download

from fetch_ux.engines import make_engine

logger = logging.getLogger("fetch_ux")


@dataclass
class FetchResult:
    url: str
    content: str
    title: str
    length: int
    truncated: bool
    status: int = 200
    download_filename: str | None = None
    actions_available: list[str] | None = None


BINARY_EXTENSIONS = {".pdf", ".pptx", ".xlsx", ".xls", ".docx", ".doc", ".zip", ".gz", ".tar", ".png", ".jpg", ".gif"}


def _read_download(path: str, filename: str | None, url: str | None = None) -> str:
    """Read downloaded file, converting binary formats to text where possible."""
    p = Path(path)
    ext = Path(filename).suffix.lower() if filename else p.suffix.lower()
    size = p.stat().st_size
    curl_hint = f"\n\nTo save locally: curl -sL -o {filename} '{url}'" if url else ""

    if ext == ".pdf":
        try:
            result = subprocess.run(
                ["pdftotext", str(p), "-"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"pdftotext failed: {e}")
        return f"[Binary PDF: {filename} ({size} bytes) — pdftotext failed]{curl_hint}"

    if ext in BINARY_EXTENSIONS:
        return f"[Binary file: {filename} ({size} bytes) — use curl to download]{curl_hint}"

    # Text-like file (csv, txt, json, xml, html, etc.)
    with open(p, "r", errors="replace") as f:
        return f.read()


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to `default` on unset/blank/garbage
    (e.g. "" or "1h") instead of crashing client creation."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("invalid %s=%r — using default %d", name, raw, default)
        return default


class FetchClient:
    """Fetches URLs: a swappable browser engine + clipboard extraction.

    Hexagonal — the browser is a port (`fetch_ux.engines.BrowserEngine`). This
    core only does extraction (clipboard/Shadow-DOM capture, overlay dismissal,
    actions, downloads, truncation) on a Playwright Page; *which* browser produces
    that page is the adapter's job. Pick it with FETCH_UX_ENGINE (default
    `invisible` — stealth Firefox that beats reCAPTCHA v3; or `chrome`).

    Periodic recycle (FETCH_UX_RECYCLE_TTL, default 1 day) rotates the session —
    cookies for Chrome, fingerprint for invisible.
    """

    def __init__(
        self,
        timeout_ms: int = 60_000,
        engine=None,
        *,
        recycle_ttl_sec: int | None = None,
    ):
        self.timeout_ms = timeout_ms
        self._engine = engine if engine is not None else make_engine(timeout_ms)
        ttl = (
            recycle_ttl_sec if recycle_ttl_sec is not None
            else _int_env("FETCH_UX_RECYCLE_TTL", 86400)
        )
        self._recycle_ttl = max(0, ttl)  # negative is meaningless; 0 = never recycle
        self._started = False
        self._born: float = 0.0
        self._semaphore = asyncio.Semaphore(3)
        self._needs_restart = False
        self._recycle_lock = asyncio.Lock()

    async def start(self):
        """Launch the engine's browser once. Call at server startup."""
        await self._engine.start()
        self._started = True
        self._born = asyncio.get_event_loop().time()

    async def stop(self):
        """Close the engine's browser. Call at server shutdown."""
        try:
            await self._engine.stop()
        finally:
            self._started = False

    async def _recycle_browser(self):
        """Flush a stuck renderer / rotate the session: stop then restart the
        engine. New fetches use the fresh browser immediately."""
        logger.warning(
            "Recycling engine=%s — flushing renderer / rotating session",
            getattr(self._engine, "name", "?"),
        )
        self._needs_restart = False
        try:
            await self._engine.stop()
        except Exception:
            pass
        await self._engine.start()
        self._born = asyncio.get_event_loop().time()

    async def fetch(
        self,
        url: str,
        actions: list[dict] | None = None,
        max_length: int = 5000,
        start_index: int = 0,
        raw: bool = False,
    ) -> FetchResult:
        """Fetch URL, render JS, optionally interact, extract content.

        Args:
            url: URL to fetch.
            actions: List of actions to perform before capturing.
            max_length: Max chars to return per call.
            start_index: Start content extraction at this char index.
            raw: Return raw HTML instead of clipboard text.
        """
        if not self._started:
            await self.start()

        # Periodic recycle: rotate the session so a flagged one can't poison us
        # forever. Routes through the same recycle path as stuck renderers.
        if (
            self._recycle_ttl > 0
            and self._born
            and asyncio.get_event_loop().time() - self._born > self._recycle_ttl
        ):
            self._needs_restart = True

        if self._needs_restart:
            async with self._recycle_lock:
                if self._needs_restart:  # re-check under lock
                    await self._recycle_browser()

        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._fetch_impl(url, actions, max_length, start_index, raw),
                    timeout=self.timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timed out after {self.timeout_ms // 1000}s fetching {url}")

    def _closer(self, page):
        """Build the per-fetch release coroutine; on a close hang, flag a recycle."""
        async def _release(url: str = ""):
            try:
                await asyncio.wait_for(page.close(), timeout=5.0)
            except Exception:
                if not self._recycle_lock.locked():
                    self._needs_restart = True
                logger.warning("page.close() timed out for %s — scheduling recycle", url)
        return _release

    async def _acquire_page(self):
        """Get a page from the engine; the engine keeps the browser warm, we just
        open and close the page per fetch."""
        page = await self._engine.new_page()
        return page, self._closer(page)

    async def _fetch_impl(
        self,
        url: str,
        actions: list[dict] | None,
        max_length: int,
        start_index: int,
        raw: bool,
    ) -> FetchResult:
        download_file: Download | None = None
        download_content: str | None = None

        page, _release_page = await self._acquire_page()
        try:
            # Listen for downloads
            page.on("download", lambda d: _capture_download(d))

            def _capture_download(d: Download):
                nonlocal download_file
                download_file = d

            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            status = response.status if response else 0

            try:
                await asyncio.wait_for(self._engine.dismiss_overlays(page), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

            # Small human-like pause after page load
            await page.wait_for_timeout(random.randint(200, 600))

            # Wait for JS content to render
            text = ""
            prev_len = 0
            stable_count = 0
            poll_start = asyncio.get_event_loop().time()
            for i in range(12):
                await page.wait_for_timeout(random.randint(400, 600))
                text = await self._engine.capture_text(page)
                cur_len = len(text)
                # Stable = <0.5% change (dynamic ads/counters jitter slightly)
                delta = abs(cur_len - prev_len) / max(cur_len, 1)
                if delta < 0.005 and i >= 2:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                prev_len = cur_len
                # If 15s elapsed and we have content, stop waiting — something's wrong
                if asyncio.get_event_loop().time() - poll_start > 15 and cur_len > 0:
                    break

            # Late cookie/consent banners (SPA frameworks inject them after the
            # initial dismiss). Now that content is rendered, try once more — and if
            # we actually removed one, re-capture so its text isn't in the output.
            try:
                if await asyncio.wait_for(self._engine.dismiss_overlays(page), timeout=3.0):
                    await page.wait_for_timeout(300)
                    text = await self._engine.capture_text(page)
            except (asyncio.TimeoutError, Exception):
                pass

            # Run actions
            if actions:
                # Dismiss any overlays that appeared after content load
                try:
                    await asyncio.wait_for(self._engine.dismiss_overlays(page), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                for act in actions:
                    await self._run_action(page, act)
                    # Check if a download was triggered
                    if download_file:
                        break
                    # Human-like pause between actions
                    await page.wait_for_timeout(random.randint(100, 400))

            # If a download was triggered, read its content
            download_filename = None
            if download_file:
                path = await download_file.path()
                download_filename = download_file.suggested_filename
                if path:
                    download_url = download_file.url or url
                    download_content = _read_download(path, download_filename, download_url)

            title = await page.title()

            if download_content is not None:
                content = download_content
            elif raw:
                content = await page.content()
            else:
                # Re-capture clipboard after actions (content may have changed)
                if actions:
                    text = await self._engine.capture_text(page)
                content = text

            # Discover available actions on the page
            actions_available = await self._discover_actions(page)
        finally:
            await _release_page(url)

        original_length = len(content)
        truncated = False

        if start_index >= original_length:
            content = ""
        elif max_length > 0:
            chunk = content[start_index : start_index + max_length]
            # End on a line boundary
            last_nl = chunk.rfind("\n")
            if last_nl > 0 and len(chunk) == max_length:
                chunk = chunk[: last_nl + 1]
            remaining = original_length - (start_index + len(chunk))
            if remaining > 0:
                truncated = True
                next_start = start_index + len(chunk)
                chunk += (
                    f"\n<truncated>Content truncated. "
                    f"Call fetch with start_index={next_start} for more.</truncated>"
                )
            content = chunk
        elif start_index > 0:
            content = content[start_index:]

        return FetchResult(
            url=url,
            content=content,
            title=title,
            length=original_length,
            truncated=truncated,
            status=status,
            download_filename=download_filename,
            actions_available=actions_available if actions_available else None,
        )

    async def _discover_actions(self, page) -> list[str]:
        """Find interactive elements on the page the agent can use."""
        try:
            return await page.evaluate("""() => {
                const actions = [];
                const seen = new Set();

                // Helper to add unique action
                function add(action) {
                    if (!seen.has(action) && action.length < 200) {
                        seen.add(action);
                        actions.push(action);
                    }
                }

                // Buttons with visible text
                document.querySelectorAll('button').forEach(el => {
                    const text = el.textContent?.trim();
                    if (text && text.length > 1 && text.length < 80 && el.offsetParent !== null) {
                        add('click: "button:has-text(\\'' + text.replace(/'/g, "\\\\'") + '\\')"');
                    }
                });

                // Links with text (not nav/footer)
                document.querySelectorAll('a[href]').forEach(el => {
                    const text = el.textContent?.trim();
                    if (text && text.length > 1 && text.length < 80 && el.offsetParent !== null) {
                        const href = el.getAttribute('href');
                        if (href && !href.startsWith('#') && !href.startsWith('javascript:')) {
                            add('click: "a:has-text(\\'' + text.replace(/'/g, "\\\\'") + '\\')" → ' + href.substring(0, 80));
                        }
                    }
                });

                // Elements with onclick window.location (clickable divs etc.)
                document.querySelectorAll('[onclick]').forEach(el => {
                    const onclick = el.getAttribute('onclick') || '';
                    const m = onclick.match(/window\.location\s*=\s*['"]([^'"]+)['"]/);
                    if (m && el.offsetParent !== null) {
                        const href = m[1];
                        const text = el.querySelector('.post-title, h1, h2, h3')?.textContent?.trim()
                            || el.textContent?.trim().substring(0, 60);
                        if (text) {
                            add('click: "[onclick]" → ' + href + ' (' + text.replace(/\s+/g, ' ') + ')');
                        }
                    }
                });

                // Download links
                document.querySelectorAll('a[download], a[href$=".csv"], a[href$=".pdf"], a[href$=".xlsx"]').forEach(el => {
                    const text = el.textContent?.trim() || el.getAttribute('download') || el.href;
                    add('download: "' + text + '"');
                });

                // Input fields
                document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(el => {
                    const name = el.name || el.id || el.getAttribute('aria-label') || el.type;
                    if (name && el.offsetParent !== null) {
                        const tag = el.tagName.toLowerCase();
                        if (tag === 'select') {
                            const opts = Array.from(el.options).slice(0, 5).map(o => o.text).join(', ');
                            add('select: "' + name + '" options=[' + opts + ']');
                        } else {
                            add('fill: "' + name + '" (' + (el.type || tag) + ')');
                        }
                    }
                });

                return actions.slice(0, 50);  // Cap at 50
            }""")
        except Exception:
            return []

    async def _run_action(self, page, act: dict):
        """Execute a single page action."""
        action = act.get("action")
        selector = act.get("selector")
        value = act.get("value")
        timeout = act.get("timeout", 5000)

        if action == "click":
            if not selector:
                return
            try:
                loc = page.locator(selector).first
                # Human-like: move mouse near target before clicking
                try:
                    box = await loc.bounding_box(timeout=timeout)
                    if box:
                        await page.mouse.move(
                            box["x"] + box["width"] * random.uniform(0.2, 0.8),
                            box["y"] + box["height"] * random.uniform(0.2, 0.8),
                            steps=random.randint(5, 15),
                        )
                        await page.wait_for_timeout(random.randint(50, 200))
                except Exception:
                    pass  # Best-effort — proceed to click even if move fails
                # Check if click triggers a download
                try:
                    async with page.expect_download(timeout=timeout):
                        await loc.click(timeout=timeout)
                    # Download was triggered — it's captured by the event listener
                    return
                except Exception:
                    # No download — normal click
                    await loc.click(timeout=timeout)
            except Exception as e:
                logger.warning(f"Action click({selector}) failed: {e}")

        elif action == "fill":
            if not selector or value is None:
                return
            try:
                await page.locator(selector).first.fill(value, timeout=timeout)
            except Exception as e:
                logger.warning(f"Action fill({selector}) failed: {e}")

        elif action == "wait":
            if selector:
                try:
                    await page.locator(selector).first.wait_for(
                        state="visible", timeout=timeout
                    )
                except Exception as e:
                    logger.warning(f"Action wait({selector}) failed: {e}")
            else:
                await page.wait_for_timeout(timeout)

        elif action == "select":
            if not selector or value is None:
                return
            try:
                await page.locator(selector).first.select_option(
                    value, timeout=timeout
                )
            except Exception as e:
                logger.warning(f"Action select({selector}) failed: {e}")

        elif action == "scroll":
            direction = act.get("direction", "bottom")
            if direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
