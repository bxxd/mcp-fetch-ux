"""Stealth-Firefox engine — invisible_playwright.

A C++-fingerprint-patched Firefox (navigator/GPU/canvas/fonts/audio/WebRTC patched
at the source, not via JS shims), drawing a fresh coherent fingerprint per session.
Passes reCAPTCHA v3 including Google SERP, where Chromium-based stealth hits a
ceiling. Needs a display — run headed under xvfb on a server.
"""

import asyncio
import logging
import os
import shutil
import tempfile

from invisible_playwright.async_api import InvisiblePlaywright

from fetch_ux.engines.base import BaseEngine

logger = logging.getLogger("fetch_ux")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Hang-guard for one-shot browser-context creation (seconds). Normal creation is a
# few seconds; this only bounds a pathological stall so startup fails loudly instead
# of blocking. Override with FETCH_UX_CONTEXT_TIMEOUT.
CONTEXT_CREATE_TIMEOUT = _float_env("FETCH_UX_CONTEXT_TIMEOUT", 20.0)

# MIME types we auto-save to the download dir without a "what to do?" prompt. This
# Firefox build's Juggler doesn't fire Playwright's download event, so downloads must
# land on disk and be read from there (see download_dir + the core's disk capture).
_DOWNLOAD_SAVE_MIMES = ",".join([
    "text/csv", "application/csv", "application/x-csv", "text/comma-separated-values",
    "application/octet-stream", "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/pdf", "application/zip", "application/json", "text/plain",
])


class InvisibleEngine(BaseEngine):
    name = "invisible"

    # Up to N concurrent fetches, served by an on-demand pool of reused tabs (see
    # acquire_page). The hazard in this Firefox is opening a target *concurrently*
    # (see new_page) — so the pool serializes tab *creation* but lets fetches *operate*
    # on their own tabs in parallel, which the probes showed is safe. Tabs are reused,
    # so creation (and its deadlock risk) only happens during warmup / after an error,
    # not on the hot path.
    concurrency = 3

    def __init__(self, timeout_ms: int = 60_000):
        self.timeout_ms = timeout_ms
        self._inv = None       # InvisiblePlaywright async context manager
        self._browser = None   # Playwright Firefox Browser, kept warm
        self._context = None   # ONE shared context, created once; fetches open tabs
        self._pool: list = []  # idle reusable tabs (pages); acquire pops, release returns
        # Serializes tab CREATION only — two concurrent new_page() calls are what
        # deadlock. Operating existing tabs in parallel is fine, so release never takes
        # this lock (a finished fetch returns its tab without waiting on a creation).
        self._create_lock = asyncio.Lock()
        # The X11 clipboard is ONE global resource per display. Ctrl+C → xclip-read
        # must not interleave across concurrent pages, or one fetch reads another's
        # copied text. Serialize just that critical section (not the whole fetch).
        self._clip_lock = asyncio.Lock()
        # Downloads land here on disk (this Firefox can't fire Playwright's download
        # event). The core reads new files from this dir and deletes them after. One
        # dir per engine; Firefox's download.dir is browser-global so the pool shares it.
        self.download_dir = tempfile.mkdtemp(prefix="fetchux-ff-dl-")
        # The download dir is shared across all tabs, so a new file can't be attributed
        # to a tab by inspection. The core serializes the download window (snapshot →
        # actions → collect) on this lock, so at most one disk-download fetch is in that
        # window at a time and any new file in it is unambiguously that fetch's. Only
        # action-bearing fetches take it; the action-less hot path never does.
        self.download_lock = asyncio.Lock()

    def _download_prefs(self) -> dict:
        """Firefox prefs that save downloads straight to our dir with no prompt — the
        only way this build downloads, since its Juggler never fires Playwright's
        download event. pdfjs.disabled so PDFs download instead of opening inline."""
        return {
            "browser.download.folderList": 2,          # 2 = custom dir
            "browser.download.dir": self.download_dir,
            "browser.download.useDownloadDir": True,
            "browser.download.manager.showWhenStarting": False,
            "browser.download.always_ask_before_handling_new_types": False,
            "browser.helperApps.neverAsk.saveToDisk": _DOWNLOAD_SAVE_MIMES,
            "pdfjs.disabled": True,
        }

    async def start(self) -> None:
        # Start with a clean download dir each launch/recycle (stale files would
        # confuse the core's new-file detection).
        shutil.rmtree(self.download_dir, ignore_errors=True)
        os.makedirs(self.download_dir, exist_ok=True)
        # humanize=False: the Bezier-mouse hooks interfere with async goto (it
        # returns no status). We don't need them — plain fetches use the keyboard,
        # and _run_action already simulates mouse movement for clicks.
        self._inv = InvisiblePlaywright(humanize=False, extra_prefs=self._download_prefs())
        self._browser = await self._inv.__aenter__()
        # Create the browser context ONCE here. browser.new_page() (used previously)
        # spins up a fresh context every fetch, and *context* creation is the hang-prone
        # step in this Firefox (per invisible_playwright's own notes, the timezone/
        # viewport overrides could hang launch). Doing it once and opening only tabs
        # (context.new_page) per fetch eliminated the deadlock. Stealth is binary/
        # profile-level so every tab carries the full fingerprint.
        # accept_downloads=True so the browser keeps downloads instead of cancelling
        # them (Playwright's default is to cancel) — a real user's clicks that produce
        # files (CSV/PDF export) must work here too. Chrome's engine already sets this.
        self._context = await asyncio.wait_for(
            self._browser.new_context(accept_downloads=True),
            timeout=CONTEXT_CREATE_TIMEOUT,
        )
        self._pool = []  # fresh browser → no carried-over tabs
        logger.info("engine=invisible: stealth Firefox up")

    async def acquire_page(self):
        """Hand out a ready tab: reuse an idle one from the pool, else create one.

        Creation is serialized (`_create_lock`) because *concurrent* tab creation is
        what deadlocks this Firefox; reuse (the common case under load) takes no lock.
        """
        if self._pool:                       # sync pop — no await, so no race
            return self._pool.pop()
        async with self._create_lock:
            if self._pool:                   # someone released while we waited for the lock
                return self._pool.pop()
            return await self.new_page()     # has its own stall-and-retry guard

    async def release_page(self, page, *, ok: bool = True) -> None:
        """Return a healthy tab to the pool for reuse; discard a bad one so the next
        acquire makes a fresh tab (fault isolation — a wedged page isn't reused)."""
        if ok:
            self._pool.append(page)          # sync — release never blocks on a creation
            return
        try:
            await asyncio.wait_for(page.close(), timeout=5.0)
        except Exception:
            pass

    async def new_page(self):
        """Create a settled, *validated* tab. Opening a target in this Firefox is flaky
        whenever it overlaps other activity: it can stall (deadlock), come up with its
        browsingContext not ready ("can't access property loadURI"), or race FF150's
        internal about:newtab nav. A fixed delay can't cover all three, so: create, let
        the internal nav land, then prove the tab is navigable with a throwaway
        about:blank; on *any* failure, discard the tab and retry fresh. Reused tabs (the
        hot path) never run this — it's warmup / post-error only."""
        last_exc: Exception | None = None
        for attempt in range(3):
            page = None
            try:
                page = await asyncio.wait_for(self._context.new_page(), timeout=6.0)
                page.set_default_timeout(self.timeout_ms)
                await asyncio.sleep(0.4)  # let FF's internal about:newtab nav land
                # Prove the tab is actually navigable before handing it to a fetch.
                await asyncio.wait_for(
                    page.goto("about:blank", wait_until="domcontentloaded"), timeout=8.0
                )
                return page
            except Exception as exc:
                last_exc = exc
                logger.warning("new_page() attempt %d/3 failed: %s — retrying",
                               attempt + 1, type(exc).__name__)
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                await asyncio.sleep(0.25)
        raise last_exc  # all attempts failed — surface as a normal fetch error

    async def capture_text(self, page) -> str:
        """Read rendered text (incl. closed Shadow DOM) back via the OS clipboard.

        Ctrl+A/Ctrl+C puts the selection on the X11 clipboard; we read it with
        `xclip`. Select-then-copy is what reaches closed shadow roots that
        `innerText` / `locator.inner_text()` can't see (Roche pipeline, etc.).
        Subprocess clipboard read is CSP-proof — page-world JS can't reach it,
        so strict-CSP sites (Reddit, HN, Roche intermittently) work.

        Firefox's Ctrl+A only selects when the *document* has focus, and a fresh
        tab leaves focus on the URL bar (or nothing) until the user clicks. So
        we click the (1,1) pixel first — exactly what a user does when they
        click on the page before pressing Ctrl+A. (1,1) is inside the html
        element's edge, well outside any normal interactive region. Without
        this click, sites that don't autofocus content (Reddit shreddit) silently
        returned an empty clipboard — the bug this fixes.

        Pure user input throughout: a mouse click and two keystrokes. No
        page.evaluate, no JS-side focus(), nothing CSP could block.
        """
        async with self._clip_lock:
            # User-equivalent focus: a click on the page so Ctrl+A has a document
            # to select in. Cheap, idempotent on repeat captures (the polling loop
            # calls this up to 12 times per fetch — same caret position each time).
            try:
                await page.mouse.click(1, 1)
            except Exception:
                pass
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
        self._pool = []
        if inv:
            try:
                await asyncio.wait_for(inv.__aexit__(None, None, None), timeout=10.0)
            except Exception:
                pass
