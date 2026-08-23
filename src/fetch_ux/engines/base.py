"""Shared engine behavior — the browser interactions that don't depend on which
browser. Adapters subclass this and add the lifecycle (start/new_page/stop) plus
the engine-specific clipboard primitives (_write_clipboard/_read_clipboard); the
sentinel-guarded capture built on them is shared here."""

import asyncio
import uuid


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
        (see `_read_clipboard`)."""
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Control+c")

    # --- clipboard capture (shared; engines supply the read/write primitives) ---

    # A failed Ctrl+C is silent. The OS clipboard keeps whatever the last successful
    # copy put there, so reading it back after a failed copy yields the PREVIOUS
    # page's text — under the current page's title, with no error anywhere. We make
    # that detectable by stamping a unique sentinel on the clipboard before every
    # copy: if the sentinel survives, the copy did not land.
    _SENTINEL_PREFIX = "fetchux-uncopied-"

    @property
    def _clipboard_lock(self) -> asyncio.Lock:
        """Serializes the stamp→copy→read window. The OS clipboard is ONE buffer per
        display — shared by every tab, page and browser context — so two captures
        running at once read each other's text. Lazily built so engines need no
        cooperating __init__."""
        lock = self.__dict__.get("_clipboard_lock_obj")
        if lock is None:
            lock = self.__dict__["_clipboard_lock_obj"] = asyncio.Lock()
        return lock

    async def _write_clipboard(self, page, text: str) -> None:
        """Put `text` on the OS clipboard. Engine-specific."""
        raise NotImplementedError

    async def _read_clipboard(self, page) -> str:
        """Read the OS clipboard back. Engine-specific."""
        raise NotImplementedError

    async def _focus_document(self, page) -> None:
        """Give the document focus so Ctrl+A has something to select. No-op by
        default; engines whose browser leaves focus off the document override it."""
        return

    async def capture_text(self, page) -> str:
        """Rendered page text (incl. closed Shadow DOM), read back via the clipboard.

        Returns "" when the copy provably did not land — never the previous fetch's
        text. Callers treat "" as "the clipboard path is unavailable here" and fall
        back to a DOM read (see FetchClient._capture)."""
        async with self._clipboard_lock:
            token = self._SENTINEL_PREFIX + uuid.uuid4().hex
            try:
                await self._write_clipboard(page, token)
            except Exception:
                # No baseline → a read can't be told apart from a stale one.
                return ""
            try:
                await self._focus_document(page)
                await self.select_all_and_copy(page)
                # The browser takes clipboard ownership just after the keystroke is
                # processed; reading instantly can race it.
                await page.wait_for_timeout(60)
                text = await self._read_clipboard(page)
            except Exception:
                # A navigation mid-capture destroys the execution context. That is a
                # failed capture, not a reason to hand back a stale buffer.
                return ""
            return "" if text.strip() == token else text

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
