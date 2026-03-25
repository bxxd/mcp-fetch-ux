"""Playwright-based web fetcher with Readability extraction.

Renders JS, extracts visible article content, returns markdown.
30s timeout. No LLM. No cost.
"""

import asyncio
from dataclasses import dataclass

import markdownify
import readabilipy.simple_json
from playwright.async_api import async_playwright, Browser


@dataclass
class FetchResult:
    url: str
    content: str
    title: str
    length: int
    truncated: bool


def extract_content_from_html(html: str) -> tuple[str, str]:
    """Extract article content from HTML using Readability, convert to markdown.

    Returns (markdown_content, title).
    """
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
    """Fetches URLs with Playwright (JS rendering) + Readability (content extraction)."""

    def __init__(self, timeout_ms: int = 30_000, user_agent: str | None = None):
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self._browser: Browser | None = None
        self._pw = None
        self._semaphore = asyncio.Semaphore(3)  # max 3 concurrent fetches

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
        max_length: int = 5000,
        start_index: int = 0,
        raw: bool = False,
    ) -> FetchResult:
        """Fetch URL, render JS, extract content as markdown.

        Args:
            url: URL to fetch.
            max_length: Max chars to return per call.
            start_index: Start content extraction at this char index (for pagination).
            raw: Return raw HTML instead of Readability-extracted markdown.
        """
        if not self._browser:
            await self.start()

        async with self._semaphore:
            return await self._fetch_impl(url, max_length, start_index, raw)

    async def _fetch_impl(
        self,
        url: str,
        max_length: int,
        start_index: int,
        raw: bool,
    ) -> FetchResult:
        context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 720},
            permissions=["clipboard-read", "clipboard-write"],
        )
        context.set_default_timeout(self.timeout_ms)

        try:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

            # Dismiss cookie/consent overlays that may block content
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('Accept All')",
                "button:has-text('OK')",
                "button:has-text('I Agree')",
                "[id*='cookie'] button",
                "[class*='cookie'] button",
                "[id*='consent'] button",
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    continue

            # Wait for JS content to render: poll clipboard text until stable
            # Require 2 consecutive stable readings after at least 1s
            text = ""
            prev_len = 0
            stable_count = 0
            for i in range(12):  # max 6s (12 × 500ms)
                await page.wait_for_timeout(500)
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Control+c")
                text = await page.evaluate("navigator.clipboard.readText()")
                cur_len = len(text)
                if cur_len == prev_len and i >= 2:  # stable after at least 1s
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                prev_len = cur_len

            title = await page.title()

            if raw:
                content = await page.content()
            else:
                # Already captured via clipboard polling above
                content = text
        finally:
            await context.close()

        original_length = len(content)
        truncated = False

        if start_index >= original_length:
            content = ""
        else:
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

        return FetchResult(
            url=url,
            content=content,
            title=title,
            length=original_length,
            truncated=truncated,
        )
