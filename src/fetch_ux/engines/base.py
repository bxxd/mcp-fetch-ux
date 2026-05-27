"""Shared engine behavior — the browser interactions that don't depend on which
browser. Adapters subclass this and add the lifecycle (start/new_page/stop) plus
the engine-specific clipboard read-back (capture_text)."""


class BaseEngine:
    # Max concurrent fetches this engine can safely run; the client sizes its fetch
    # semaphore from this. Default 1 (the safe floor): an engine driving a single
    # browser process may deadlock on concurrent target creation or share a global
    # OS clipboard. Engines that isolate per browser-context (e.g. Chromium) raise it.
    concurrency = 1

    # Cookie/consent overlay buttons, tried in order; the first visible one is
    # clicked. Engine-agnostic — same recipe for Chrome and Firefox.
    _OVERLAY_SELECTORS = [
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
    ]

    async def dismiss_overlays(self, page) -> bool:
        """Click through a cookie/consent overlay if one is blocking the page.
        Returns True if it dismissed one (so the core can re-capture)."""
        for selector in self._OVERLAY_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=300):
                    await btn.click(timeout=1000)
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                continue
        return False

    async def select_all_and_copy(self, page) -> None:
        """Select the whole rendered page and copy it to the OS clipboard — the
        step that captures Shadow-DOM text (including closed roots) that
        innerText/Readability miss. Reading it back is engine-specific
        (see `capture_text`)."""
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Control+c")
