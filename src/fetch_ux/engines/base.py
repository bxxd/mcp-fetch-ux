"""Shared engine behavior — the browser interactions that don't depend on which
browser. Adapters subclass this and add the lifecycle (start/new_page/stop) plus
the engine-specific clipboard read-back (capture_text)."""

import asyncio


class BaseEngine:
    # Max concurrent fetches this engine can safely run; the client sizes its fetch
    # semaphore from this. Default 1 (the safe floor): an engine driving a single
    # browser process may deadlock on concurrent target creation or share a global
    # OS clipboard. Engines that isolate per browser-context (e.g. Chromium) raise it.
    concurrency = 1

    # Where the engine saves downloads on disk, when it can't use Playwright's
    # download event (invisible Firefox sets a real dir; Chromium leaves this None
    # and captures via the event).
    download_dir = None

    # Cookie/consent overlay buttons. Engine-agnostic.
    # We try multiple passes because some sites (Roche, etc.) show layered
    # OneTrust modals — a banner first, then a full Preference Center with a
    # dark filter that blocks clicks until dismissed.
    _OVERLAY_SELECTORS = [
        # OneTrust — direct + modal Preference Center buttons (Roche and many others)
        "#onetrust-reject-all-handler",
        "#onetrust-accept-btn-handler",
        "button.ot-pc-refuse-all-handler",
        "button.ot-pc-accept-btn-handler",
        "#onetrust-pc-btn-handler",
        "button#accept-recommended-btn-handler",

        # Text-based (works across many CMPs)
        "button:has-text('Accept All')",
        "button:has-text('Reject All')",
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "button:has-text('I Agree')",
        "button:has-text('Confirm My Choices')",
        "button:has-text('Reject Non-Essential')",

        # Broad attribute matches
        "[id*='cookie'] button",
        "[class*='cookie'] button",
        "[id*='consent'] button",
        "button[aria-label*='Reject All']",
        "button[aria-label*='Accept All']",
        "[class*='onetrust'] button",
    ]

    async def dismiss_overlays(self, page) -> bool:
        """Click through a cookie/consent overlay if one blocks the page: one pass,
        click the FIRST visible consent button, stop. Returns True if it dismissed one.

        Deliberately minimal. An earlier multi-pass version that also clicked the
        OneTrust dark-filter and every button in the SDK destabilized pages and broke
        legitimate action clicks (e.g. Roche's "Download CSV" export stopped firing).
        One clean accept/reject dismisses the banner, and the consent cookie persists
        in our warm context, so subsequent fetches don't see it again."""
        for selector in self._OVERLAY_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=300):
                    await btn.click(timeout=1000)
                    await page.wait_for_timeout(400)
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

    # --- per-fetch page lifecycle (engines that pool/reuse pages override these) ---

    async def acquire_page(self):
        """Get a ready page for one fetch. Default: a fresh page each time."""
        return await self.new_page()

    async def release_page(self, page, *, ok: bool = True) -> None:
        """Release a page after a fetch. Default: close it (fresh page per fetch),
        so `ok` is irrelevant here — a pooling engine uses it to decide reuse."""
        try:
            await asyncio.wait_for(page.close(), timeout=5.0)
        except Exception:
            pass
