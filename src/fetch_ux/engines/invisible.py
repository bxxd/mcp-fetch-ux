"""Stealth-Firefox engine — invisible_playwright.

A C++-fingerprint-patched Firefox (navigator/GPU/canvas/fonts/audio/WebRTC patched
at the source, not via JS shims), drawing a fresh coherent fingerprint per session.
Passes reCAPTCHA v3 including Google SERP, where Chromium-based stealth hits a
ceiling. Needs a display — run headed under xvfb on a server.
"""

import asyncio
import logging

from invisible_playwright.async_api import InvisiblePlaywright

from fetch_ux.engines.base import BaseEngine

logger = logging.getLogger("fetch_ux")


class InvisibleEngine(BaseEngine):
    name = "invisible"

    # One fetch at a time. Opening a browser target in this Firefox intermittently
    # deadlocks (see new_page), and the clipboard read-back rides one global X
    # selection per display — running fetches in parallel multiplies both hazards.
    # Serial execution plus the new_page retry is the combination observed reliable
    # here; true parallelism would need a pool of separate Firefox processes (later).
    concurrency = 1

    def __init__(self, timeout_ms: int = 60_000):
        self.timeout_ms = timeout_ms
        self._inv = None       # InvisiblePlaywright async context manager
        self._browser = None   # Playwright Firefox Browser, kept warm
        self._context = None   # ONE shared context, created once; fetches open tabs
        # The X11 clipboard is ONE global resource per display. Ctrl+C → xclip-read
        # must not interleave across concurrent pages, or one fetch reads another's
        # copied text. Serialize just that critical section (not the whole fetch).
        self._clip_lock = asyncio.Lock()

    async def start(self) -> None:
        # humanize=False: the Bezier-mouse hooks interfere with async goto (it
        # returns no status). We don't need them — plain fetches use the keyboard,
        # and _run_action already simulates mouse movement for clicks.
        self._inv = InvisiblePlaywright(humanize=False)
        self._browser = await self._inv.__aenter__()
        # Create the browser context ONCE here. browser.new_page() (used previously)
        # spins up a fresh context every fetch, and *context* creation is the hang-prone
        # step in this Firefox (per invisible_playwright's own notes, the timezone/
        # viewport overrides could hang launch). Doing it once and opening only tabs
        # (context.new_page) per fetch eliminated the deadlock. Stealth is binary/
        # profile-level so every tab carries the full fingerprint.
        self._context = await asyncio.wait_for(self._browser.new_context(), timeout=60.0)
        logger.info("engine=invisible: stealth Firefox up")

    async def new_page(self):
        # Opening a target in this Firefox intermittently deadlocks (~7.5% under heavy
        # churn): a healthy creation returns in <0.7s, a bad one hangs forever — but
        # the evidence is clear that *abandoning a hung attempt and retrying succeeds
        # immediately* (the next new_page returns in ~0.5s). So bound each attempt
        # well above the normal time and retry. (A tab in the shared context, not a
        # new context — with concurrency=1 only one tab is ever live at a time.)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                page = await asyncio.wait_for(self._context.new_page(), timeout=6.0)
                try:
                    page.set_default_timeout(self.timeout_ms)
                except Exception:
                    pass
                return page
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning("new_page() stalled (attempt %d/3) — retrying", attempt + 1)
                await asyncio.sleep(0.25)
        raise last_exc  # all attempts stalled — surface as a normal fetch error

    async def capture_text(self, page) -> str:
        """Read rendered text (incl. closed Shadow DOM) back via the OS clipboard.

        Ctrl+A/Ctrl+C puts the selection on the X11 clipboard; we read it with
        `xclip`, NOT page.evaluate. evaluate runs in Firefox's main world, so a
        strict page CSP (`script-src` without `unsafe-eval` — HN always, Roche
        intermittently) blocks the eval() Playwright uses to run it: it either
        throws "call to eval() blocked by CSP" or hangs the whole fetch to timeout.
        A subprocess reading the clipboard never touches the page, so CSP can't
        reach it. The copy+read is serialized (`_clip_lock`) because the clipboard
        is global per display.
        """
        async with self._clip_lock:
            await self.select_all_and_copy(page)
            # Tiny settle: Firefox sets the X CLIPBOARD selection owner just after
            # the Ctrl+C keystroke is processed; reading instantly can race it.
            await page.wait_for_timeout(60)
            return await self._read_x_clipboard()

    @staticmethod
    async def _read_x_clipboard() -> str:
        """Read the X11 CLIPBOARD selection via xclip. The worker runs headed under
        xvfb, so DISPLAY is set and inherited by the subprocess. Returns "" on any
        failure — the caller's stability loop re-captures, so a transient miss is
        self-healing rather than fatal."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard", "-o",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return out.decode("utf-8", "replace")
        except Exception:
            return ""

    async def stop(self) -> None:
        inv = self._inv
        self._inv = None
        self._browser = None
        self._context = None
        if inv:
            try:
                await asyncio.wait_for(inv.__aexit__(None, None, None), timeout=10.0)
            except Exception:
                pass
