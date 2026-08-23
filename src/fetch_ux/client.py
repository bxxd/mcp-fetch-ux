"""Engine-agnostic web fetcher with clipboard extraction.

Renders JS, captures visible text (including closed Shadow DOM) via Ctrl+A/Ctrl+C,
runs page interactions (click, fill, wait), and returns file downloads. No LLM,
no cost. This core holds zero browser-specific code.

The browser is a swappable port (`fetch_ux.engines`, picked via FETCH_UX_ENGINE):
- `invisible` (default) — a C++-fingerprint-patched Firefox that passes reCAPTCHA v3
  including Google SERP, where Chromium-based stealth hits a ceiling.
- `chrome` — real Google Chrome via Patchright, for Cloudflare/Datadome-class walls.
Both drive a real browser headed, so they need a display — run under xvfb.
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
        # Size the fetch gate from what the engine declares it can safely run at once
        # (invisible=1: single Firefox deadlocks on concurrent target creation;
        # chrome=3: Chromium isolates per context). Falls back to 1 for any engine
        # that doesn't declare it. Stored so recycle can drain exactly this many.
        self._concurrency = getattr(self._engine, "concurrency", 1)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._needs_restart = False
        self._recycle_lock = asyncio.Lock()

    async def start(self):
        """Launch the engine's browser once. Call at server startup."""
        await self._engine.start()
        self._started = True
        self._born = asyncio.get_running_loop().time()

    async def stop(self):
        """Close the engine's browser. Call at server shutdown."""
        try:
            await self._engine.stop()
        finally:
            self._started = False

    async def _recycle_browser(self):
        """Flush a stuck renderer / rotate the session: stop then restart the engine.

        Drains the fetch gate first — acquires every semaphore permit so no fetch is
        inside `_fetch_impl` when we stop the browser. Otherwise stopping invalidates
        the pages those in-flight fetches hold, failing unrelated requests. In-flight
        fetches finish and release their permits; new ones block on the gate until the
        fresh browser is up. (The triggering fetch hasn't taken its permit yet — it
        acquires the gate only after this returns — so there's no self-deadlock.)
        """
        acquired = 0
        try:
            for _ in range(self._concurrency):
                await self._semaphore.acquire()
                acquired += 1
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
            self._born = asyncio.get_running_loop().time()
        finally:
            for _ in range(acquired):
                self._semaphore.release()

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
            and asyncio.get_running_loop().time() - self._born > self._recycle_ttl
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

    async def _capture(self, page) -> str:
        """Page text: clipboard first, DOM read as the fallback.

        The engine returns "" when the Ctrl+C provably did not land — a mid-capture
        navigation, an unselectable page, or a destroyed execution context. That is a
        routine condition on JS-app sites (congress.gov defeats Ctrl+A/Ctrl+C on every
        bill page), so fall back to reading the rendered DOM. It misses closed Shadow
        DOM, which is why it isn't the primary path, but it needs no clipboard and no
        page-world JS, and it always belongs to the page actually loaded."""
        text = await self._engine.capture_text(page)
        if text.strip():
            return text
        try:
            return await page.locator("body").inner_text(timeout=8000)
        except Exception:
            return ""

    async def _goto(self, page, url: str):
        """Navigate, retrying once on a transient 'interrupted by another navigation'.
        A freshly opened tab can fire an internal about:blank/about:newtab navigation
        that interrupts the first goto; the retry lands after it settles."""
        for attempt in range(2):
            try:
                return await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
            except Exception as e:
                if attempt == 0 and "interrupted by another navigation" in str(e):
                    continue
                raise

    @staticmethod
    async def _collect_disk_download(dl_dir, before, settle=2.0, timeout=20.0):
        """For engines that save downloads to disk instead of firing Playwright's
        download event (invisible Firefox): poll `dl_dir` for a new *completed* file
        (Firefox writes a `.part` while in flight, renames when done). Returns
        (path, filename) or None. Waits up to `settle` for a download to even start,
        then up to `timeout` for it to finish."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < timeout:
            try:
                new = set(os.listdir(dl_dir)) - before
            except OSError:
                return None
            done = [f for f in new if not f.endswith((".part", ".tmp", ".crdownload"))]
            if done:
                newest = max(done, key=lambda n: os.path.getmtime(os.path.join(dl_dir, n)))
                return os.path.join(dl_dir, newest), newest
            # Nothing started within the settle window → this fetch has no download.
            if not new and (loop.time() - start) > settle:
                return None
            await asyncio.sleep(0.25)
        return None

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

        page = await self._engine.acquire_page()
        ok = False
        popups: list = []
        # Engines that can't use Playwright's download event (invisible Firefox) save
        # files to disk instead, into one browser-global dir shared by every tab. We
        # snapshot that dir just before running actions and diff after — but only while
        # holding the engine's download_lock, so a concurrent fetch's download can't land
        # in our diff and get misattributed (or deleted out from under it). The snapshot
        # is taken below, inside the lock; the action-less hot path skips all of this.
        dl_dir = getattr(self._engine, "download_dir", None)
        dl_lock = getattr(self._engine, "download_lock", None)
        dl_before: set = set()

        def _on_download(d: Download):
            nonlocal download_file
            download_file = d

        def _on_popup(p):
            # Some sites export a file by opening a new tab that then downloads (CSV/PDF
            # "open in new window"). Catch downloads there too, and track the popup so we
            # can close it — like a real user's browser, which handles the popup download.
            popups.append(p)
            p.on("download", _on_download)

        # Named handlers (not lambdas) so we can remove them before releasing — the tab
        # is reused by later fetches, and stale listeners would pile up.
        page.on("download", _on_download)
        page.on("popup", _on_popup)
        try:
            response = await self._goto(page, url)
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
            poll_start = asyncio.get_running_loop().time()
            for i in range(12):
                await page.wait_for_timeout(random.randint(400, 600))
                text = await self._capture(page)
                cur_len = len(text)
                # Stable = <0.5% change (dynamic ads/counters jitter slightly).
                # Safe against a stale buffer now: a failed copy yields "" from
                # _capture's clipboard leg, not a repeat of the previous fetch's
                # text, so "unchanging" can no longer mean "unchanged since the last
                # URL".
                delta = abs(cur_len - prev_len) / max(cur_len, 1)
                # `cur_len > 0`: an empty capture is not a settled page. Zero-length
                # reads compare equal to each other, so without this a page that
                # hasn't rendered yet looks maximally "stable" and we give up after
                # ~2s. (Before the sentinel this never showed: a failed copy returned
                # the previous fetch's text, which was non-empty and looked stable
                # for the same wrong reason.)
                if cur_len > 0 and delta < 0.005 and i >= 2:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                prev_len = cur_len
                # If 15s elapsed and we have content, stop waiting — something's wrong
                if asyncio.get_running_loop().time() - poll_start > 15 and cur_len > 0:
                    break

            # Late cookie/consent banners (SPA frameworks inject them after the
            # initial dismiss). Now that content is rendered, try once more — and if
            # we actually removed one, re-capture so its text isn't in the output.
            try:
                if await asyncio.wait_for(self._engine.dismiss_overlays(page), timeout=3.0):
                    await page.wait_for_timeout(300)
                    text = await self._capture(page)
            except (asyncio.TimeoutError, Exception):
                pass

            # Run actions + resolve any download they triggered. For disk-download
            # engines (shared dir) this whole window is serialized on download_lock so
            # the snapshot/diff can't collide with a concurrent fetch's download; the
            # action-less path holds no lock and stays concurrent.
            download_filename = None
            use_dl_lock = bool(dl_dir and dl_lock and actions)
            if use_dl_lock:
                await dl_lock.acquire()
            try:
                if actions:
                    # Snapshot the shared download dir now (inside the lock) so anything
                    # new after the actions is unambiguously this fetch's download.
                    if dl_dir and os.path.isdir(dl_dir):
                        dl_before = set(os.listdir(dl_dir))

                    # Dismiss overlays again right before actions — some sites (Roche)
                    # re-show or delay their OneTrust modal until after initial load.
                    for _ in range(2):
                        try:
                            await asyncio.wait_for(self._engine.dismiss_overlays(page), timeout=4.0)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        await page.wait_for_timeout(300)

                    for act in actions:
                        await self._run_action(page, act)
                        # Check if a download was triggered
                        if download_file:
                            break
                        # Human-like pause between actions
                        await page.wait_for_timeout(random.randint(100, 400))

                # If a download was triggered, read its content.
                if download_file:
                    # Playwright download event (Chromium).
                    path = await download_file.path()
                    download_filename = download_file.suggested_filename
                    if path:
                        download_url = download_file.url or url
                        download_content = _read_download(path, download_filename, download_url)
                elif dl_dir and actions:
                    # Disk-based capture (invisible Firefox): the file was saved to the
                    # download dir. Grab the new file, read it, then delete it so the dir
                    # doesn't accumulate and the next fetch's snapshot stays clean.
                    got = await self._collect_disk_download(dl_dir, dl_before)
                    if got:
                        path, download_filename = got
                        download_content = _read_download(path, download_filename, url)
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            finally:
                if use_dl_lock:
                    dl_lock.release()

            # Best-effort: a late client-side navigation (some SPAs) can destroy the
            # execution context right here; the title isn't worth failing the fetch.
            try:
                title = await page.title()
            except Exception:
                title = ""

            if download_content is not None:
                content = download_content
            elif raw:
                content = await page.content()
            else:
                # Re-capture clipboard after actions (content may have changed)
                if actions:
                    text = await self._capture(page)
                content = text

            # Discover available actions on the page
            actions_available = await self._discover_actions(page)
            ok = True
        finally:
            try:
                page.remove_listener("download", _on_download)
                page.remove_listener("popup", _on_popup)
            except Exception:
                pass
            # Close any popups this fetch opened (a popup download was already read
            # above) so they don't accumulate in the reused context.
            for p in popups:
                try:
                    await p.close()
                except Exception:
                    pass
            # Healthy tab → back to the pool for reuse; a failed fetch's tab is
            # discarded so the next fetch starts clean (engine decides — see
            # BaseEngine/InvisibleEngine.release_page).
            await self._engine.release_page(page, ok=ok)

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
        """Find interactive elements the agent can act on.

        This version uses Playwright locators from the Python side (driver protocol).
        It is CSP-safe: it does not require page.evaluate("..."), so it works on
        strict sites like news.ycombinator.com that block 'unsafe-eval'.

        We prioritize completeness ("get all the actions") over micro-optimizing
        latency. Caps are set reasonably high; the final result is still truncated
        to 50 actions.
        """
        actions: list[str] = []
        seen: set[str] = set()

        def add(desc: str) -> None:
            if desc not in seen and len(desc) < 200:
                seen.add(desc)
                actions.append(desc)

        try:
            # Buttons
            button_locator = page.locator("button:visible")
            button_texts = await button_locator.all_text_contents()
            for text in button_texts[:60]:
                t = (text or "").strip()
                if 2 <= len(t) <= 80:
                    safe = t.replace("'", "\\'")
                    add(f'click: "button:has-text(\'{safe}\')"')

            # Links (the most important source on most real pages)
            link_locator = page.locator("a[href]:visible")
            link_texts = await link_locator.all_text_contents()
            # We still need per-element hrefs. Do them in one batched evaluate_all
            # (this is still safer / more contained than the old full evaluate blob).
            try:
                hrefs = await link_locator.evaluate_all("els => els.map(el => el.getAttribute('href') || '')")
            except Exception:
                # Fallback if evaluate_all is blocked on a very strict page
                hrefs = [""] * len(link_texts)

            for text, href in zip(link_texts, hrefs):
                t = (text or "").strip()
                h = (href or "").strip()
                if 2 <= len(t) <= 80 and h and not h.startswith(("#", "javascript:")):
                    safe = t.replace("'", "\\'")
                    add(f'click: "a:has-text(\'{safe}\')" → {h[:80]}')

            # Download-oriented links (explicit or by extension)
            download_locator = page.locator(
                "a[download]:visible, a[href$='.csv']:visible, a[href$='.pdf']:visible, a[href$='.xlsx']:visible"
            )
            dl_texts = await download_locator.all_text_contents()
            try:
                dl_hrefs = await download_locator.evaluate_all(
                    "els => els.map(el => el.getAttribute('href') || el.getAttribute('download') || '')"
                )
            except Exception:
                dl_hrefs = [""] * len(dl_texts)

            for text, href in zip(dl_texts, dl_hrefs):
                name = (text or "").strip() or (href or "").strip()
                if name:
                    add(f'download: "{name[:80]}"')

            # Form controls - inputs
            input_locator = page.locator("input:not([type=hidden]):visible")
            input_elements = await input_locator.all()
            for el in input_elements[:30]:
                try:
                    name = (
                        await el.get_attribute("name")
                        or await el.get_attribute("id")
                        or await el.get_attribute("aria-label")
                        or await el.get_attribute("type")
                        or "input"
                    )
                    if name:
                        typ = await el.get_attribute("type") or "text"
                        add(f'fill: "{name}" ({typ})')
                except Exception:
                    continue

            # Textareas
            textarea_locator = page.locator("textarea:visible")
            textarea_elements = await textarea_locator.all()
            for el in textarea_elements[:15]:
                try:
                    name = (
                        await el.get_attribute("name")
                        or await el.get_attribute("id")
                        or await el.get_attribute("aria-label")
                        or "textarea"
                    )
                    if name:
                        add(f'fill: "{name}" (textarea)')
                except Exception:
                    continue

            # Selects
            select_locator = page.locator("select:visible")
            select_elements = await select_locator.all()
            for el in select_elements[:15]:
                try:
                    name = (
                        await el.get_attribute("name")
                        or await el.get_attribute("id")
                        or await el.get_attribute("aria-label")
                        or "select"
                    )
                    if name:
                        add(f'select: "{name}"')
                except Exception:
                    continue

        except Exception:
            # Page is hostile or closed; best effort only
            pass

        return actions[:50]

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
                # Event-based engines (Chromium): wrap the click in expect_download so
                # a click that triggers a download is caught. Disk-based engines
                # (invisible Firefox) never fire that event — they capture downloads
                # from the download dir after actions — so they click plainly; wrapping
                # would just burn the full timeout on every non-download click.
                if getattr(self._engine, "download_dir", None) is None:
                    try:
                        async with page.expect_download(timeout=timeout):
                            await loc.click(timeout=timeout)
                        return  # download triggered — captured by the event listener
                    except Exception:
                        await loc.click(timeout=timeout)  # no download — normal click
                else:
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
            # Use keyboard input protocol (CSP-safe, no page.evaluate).
            # "End"/"Home" reliably jump to the actual scroll boundaries even on very tall pages.
            try:
                if direction == "bottom":
                    await page.keyboard.press("End")
                elif direction == "top":
                    await page.keyboard.press("Home")
            except Exception as e:
                logger.warning(f"Action scroll({direction}) failed: {e}")
            await page.wait_for_timeout(500)
