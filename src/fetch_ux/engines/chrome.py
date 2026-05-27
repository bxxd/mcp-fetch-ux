"""Real-Chrome engine — Patchright + a persistent context.

Real Google Chrome (channel="chrome") with one warm persistent context (shared
cookie jar) and no manual stealth — real Chrome + Patchright present a coherent
fingerprint. Optional GPU/EGL when a DRM render node is present. Beats
Cloudflare/Datadome-class walls; does NOT beat Google's reCAPTCHA-Enterprise SERP
(use the invisible engine for that). Needs Chrome (`patchright install chrome`)
and a display — run headed under xvfb; FETCH_UX_HEADLESS=1 forces headless.
"""

import asyncio
import logging
import os
import shutil
import tempfile

from patchright.async_api import async_playwright

from fetch_ux.engines.base import BaseEngine

logger = logging.getLogger("fetch_ux")


def _chrome_args() -> list[str]:
    """If a DRM render node is present (a GPU was passed into the container), drive
    WebGL through it via ANGLE/EGL so the renderer reports the real GPU instead of
    'WebGL: false' — a glaring automation tell. No-op without a GPU."""
    if os.path.exists("/dev/dri/renderD128"):
        return [
            "--use-gl=angle",
            "--use-angle=gl-egl",   # ANGLE over native EGL → Mesa iris → render node
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
        ]
    return []


class ChromeEngine(BaseEngine):
    name = "chrome"

    # Chromium isolates each fetch in its own browser context and reads the clipboard
    # via a per-context permission grant (no global-clipboard or shared-target-creation
    # hazard), so it runs several fetches at once safely.
    concurrency = 3

    def __init__(self, timeout_ms: int = 60_000, headless: bool | None = None):
        self.timeout_ms = timeout_ms
        self._headless = (
            headless if headless is not None
            else os.environ.get("FETCH_UX_HEADLESS", "0") == "1"
        )
        self._pw = None
        self._context = None
        self._user_data_dir: str | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # Fresh profile dir each launch → recycling wipes cookies for free.
        self._user_data_dir = tempfile.mkdtemp(prefix="fetchux-chrome-")
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                self._user_data_dir,
                channel="chrome",          # real Google Chrome, not bundled Chromium
                headless=self._headless,
                no_viewport=True,          # use the real window size; don't fingerprint
                locale="en-US",
                permissions=["clipboard-read", "clipboard-write"],
                accept_downloads=True,
                args=_chrome_args(),
            )
        except Exception:
            # Don't leak the temp profile or the Playwright process on a bad launch.
            self._remove_dir(self._user_data_dir)
            self._user_data_dir = None
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            raise
        self._context.set_default_timeout(self.timeout_ms)
        logger.info("engine=chrome: real Chrome up (headless=%s)", self._headless)

    async def new_page(self):
        page = await self._context.new_page()
        try:
            page.set_default_timeout(self.timeout_ms)
        except Exception:
            pass
        return page

    async def capture_text(self, page) -> str:
        await self.select_all_and_copy(page)
        # Chromium honors the context's clipboard-read permission grant, so the
        # async readText API is reliable here.
        return await page.evaluate("navigator.clipboard.readText()")

    async def stop(self) -> None:
        ctx, pw, data_dir = self._context, self._pw, self._user_data_dir
        self._context = self._pw = self._user_data_dir = None
        if ctx:
            try:
                await asyncio.wait_for(ctx.close(), timeout=5.0)
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        self._remove_dir(data_dir)

    @staticmethod
    def _remove_dir(path: str | None):
        if path:
            shutil.rmtree(path, ignore_errors=True)
