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

    def __init__(self, timeout_ms: int = 60_000):
        self.timeout_ms = timeout_ms
        self._inv = None       # InvisiblePlaywright async context manager
        self._browser = None   # Playwright Firefox Browser, kept warm

    # Firefox gates navigator.clipboard.readText() behind transient user
    # activation and (unlike Chromium) ignores Playwright's clipboard permission
    # grant — so the Ctrl+A/Ctrl+C → readText extraction stalls on a permission
    # prompt on heavy pages (e.g. Google). These prefs bypass the activation gate.
    _PREFS = {
        "dom.events.asyncClipboard.readText": True,
        "dom.events.testing.asyncClipboard": True,
        "dom.events.asyncClipboard.clipboardItem": True,
    }

    async def start(self) -> None:
        # humanize=False: the Bezier-mouse hooks interfere with async goto +
        # clipboard.readText() (goto returns no status, readText hangs). We don't
        # need them — plain fetches use the keyboard, and _run_action already
        # simulates mouse movement for clicks.
        self._inv = InvisiblePlaywright(humanize=False, extra_prefs=self._PREFS)
        self._browser = await self._inv.__aenter__()
        logger.info("engine=invisible: stealth Firefox up")

    async def new_page(self):
        page = await self._browser.new_page()
        try:
            page.set_default_timeout(self.timeout_ms)
        except Exception:
            pass
        return page

    async def capture_text(self, page) -> str:
        await self.select_all_and_copy(page)
        # Firefox gates navigator.clipboard.readText() behind transient user
        # activation (and ignores Playwright's permission grant), so it hangs on
        # heavy pages. Read the Ctrl+C'd text back the un-gated way: paste into a
        # throwaway textarea (an ordinary user paste) and read its value.
        await page.evaluate("""() => {
            let t = document.getElementById('__fux_grab__') || document.createElement('textarea');
            t.id = '__fux_grab__';
            t.value = '';
            t.style.position = 'fixed';
            t.style.left = '-9999px';
            t.style.top = '0';
            document.body.appendChild(t);
            t.focus();
        }""")
        await page.keyboard.press("Control+v")
        return await page.evaluate("""() => {
            let t = document.getElementById('__fux_grab__');
            let v = t ? t.value : '';
            if (t) t.remove();
            return v;
        }""")

    async def stop(self) -> None:
        inv = self._inv
        self._inv = None
        self._browser = None
        if inv:
            try:
                await asyncio.wait_for(inv.__aexit__(None, None, None), timeout=10.0)
            except Exception:
                pass
