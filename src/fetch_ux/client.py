"""Playwright-based web fetcher with clipboard extraction.

Renders JS, captures visible text (including Shadow DOM) via Ctrl+A/Ctrl+C.
Supports page interactions (click, fill, wait) and file downloads.
30s timeout. No LLM. No cost.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import markdownify
import readabilipy.simple_json
from playwright.async_api import async_playwright, Browser, Download

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


def extract_content_from_html(html: str) -> tuple[str, str]:
    """Extract article content from HTML using Readability, convert to markdown."""
    ret = readabilipy.simple_json.simple_json_from_html_string(
        html, use_readability=True
    )
    if not ret["content"]:
        return "", ""
    content = markdownify.markdownify(
        ret["content"],
        heading_style=markdownify.ATX,
    )
    title = ret.get("title") or ""
    return content, title


class FetchClient:
    """Fetches URLs with Playwright (JS rendering) + clipboard extraction."""

    def __init__(self, timeout_ms: int = 30_000, user_agent: str | None = None):
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self._browser: Browser | None = None
        self._pw = None
        self._semaphore = asyncio.Semaphore(3)

    async def start(self):
        """Launch browser. Call once at server startup."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
        )

    async def stop(self):
        """Close browser. Call at server shutdown."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

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
        if not self._browser:
            await self.start()

        async with self._semaphore:
            return await self._fetch_impl(url, actions, max_length, start_index, raw)

    async def _fetch_impl(
        self,
        url: str,
        actions: list[dict] | None,
        max_length: int,
        start_index: int,
        raw: bool,
    ) -> FetchResult:
        context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 720},
            permissions=["clipboard-read", "clipboard-write"],
            accept_downloads=True,
        )
        context.set_default_timeout(self.timeout_ms)

        download_file: Download | None = None
        download_content: str | None = None

        try:
            page = await context.new_page()

            # Listen for downloads
            page.on("download", lambda d: _capture_download(d))

            def _capture_download(d: Download):
                nonlocal download_file
                download_file = d

            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            status = response.status if response else 0

            await self._dismiss_overlays(page)

            # Wait for JS content to render
            text = ""
            prev_len = 0
            stable_count = 0
            for i in range(12):
                await page.wait_for_timeout(500)
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Control+c")
                text = await page.evaluate("navigator.clipboard.readText()")
                cur_len = len(text)
                if cur_len == prev_len and i >= 2:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                prev_len = cur_len

            # Run actions
            if actions:
                # Dismiss any overlays that appeared after content load
                await self._dismiss_overlays(page)
                for act in actions:
                    await self._run_action(page, act)
                    # Check if a download was triggered
                    if download_file:
                        break

            # If a download was triggered, read its content
            download_filename = None
            if download_file:
                path = await download_file.path()
                download_filename = download_file.suggested_filename
                if path:
                    with open(path, "r", errors="replace") as f:
                        download_content = f.read()

            title = await page.title()

            if download_content is not None:
                content = download_content
            elif raw:
                content = await page.content()
            else:
                # Re-capture clipboard after actions (content may have changed)
                if actions:
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Control+c")
                    text = await page.evaluate("navigator.clipboard.readText()")
                content = text

            # Discover available actions on the page
            actions_available = await self._discover_actions(page)
        finally:
            await context.close()

        original_length = len(content)
        truncated = False

        if start_index >= original_length:
            content = ""
        elif max_length > 0:
            chunk = content[start_index : start_index + max_length]
            remaining = original_length - (start_index + len(chunk))
            if remaining > 0 and len(chunk) == max_length:
                truncated = True
                next_start = start_index + len(chunk)
                chunk += (
                    f"\n\n<truncated>Content truncated. "
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

                return actions.slice(0, 20);  // Cap at 20 to keep output reasonable
            }""")
        except Exception:
            return []

    async def _dismiss_overlays(self, page):
        """Dismiss cookie/consent overlays blocking interaction."""
        for selector in [
            "#onetrust-reject-all-handler",
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All')",
            "button:has-text('Reject All')",
            "button:has-text('Accept')",
            "button:has-text('OK')",
            "button:has-text('I Agree')",
            "[id*='cookie'] button",
            "[class*='cookie'] button",
            "[id*='consent'] button",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=300):
                    await btn.click()
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

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
                # Check if click triggers a download
                try:
                    async with page.expect_download(timeout=timeout) as dl_info:
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
