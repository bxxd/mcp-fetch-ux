"""Unit tests — pure functions, no browser needed."""

import asyncio
import os
import tempfile

import pytest

from fetch_ux.client import _read_download, FetchClient, FetchResult
from fetch_ux.engines import make_engine
from fetch_ux.engines.base import BaseEngine
from fetch_ux.engines.chrome import _chrome_args, ChromeEngine
from mcp_fetch_ux.tools import TOOLS
from mcp_fetch_ux.handlers import call_tool, _validate_url, UnsafeUrl, _looks_blocked


# --- _read_download ---

def test_read_download_text_file():
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        f.write("a,b,c\n1,2,3\n")
        f.flush()
        result = _read_download(f.name, "data.csv")
    assert "a,b,c" in result
    assert "1,2,3" in result


def test_read_download_binary_extension():
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(b"\x00\x01\x02")
        f.flush()
        result = _read_download(f.name, "sheet.xlsx", "https://example.com/sheet.xlsx")
    assert "Binary file" in result
    assert "sheet.xlsx" in result
    assert "curl" in result


def test_read_download_no_url_no_curl_hint():
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(b"\x00")
        f.flush()
        result = _read_download(f.name, "archive.zip")
    assert "Binary file" in result
    assert "curl -sL -o" not in result  # no specific curl command without URL


# --- Tool schema ---

def test_tool_schema_valid():
    assert [t["name"] for t in TOOLS] == ["read_webpage", "read_blocked_webpage"]
    for tool in TOOLS:
        assert "url" in tool["inputSchema"]["properties"]
        assert "url" in tool["inputSchema"]["required"]


def test_tool_schema_has_all_params():
    props = TOOLS[0]["inputSchema"]["properties"]
    assert "url" in props
    assert "actions" in props
    assert "max_length" in props
    assert "start_index" in props
    assert "raw" in props


# --- Tool can be constructed as mcp.types.Tool ---

def test_tool_schema_constructs_as_mcp_tool():
    from mcp.types import Tool
    assert {Tool(**t).name for t in TOOLS} == {"read_webpage", "read_blocked_webpage"}


# --- handlers.call_tool routing ---

@pytest.mark.asyncio
async def test_call_tool_unknown():
    with pytest.raises(ValueError, match="Unknown tool"):
        await call_tool("nonexistent", {})


# --- FetchResult dataclass ---

def test_fetch_result_defaults():
    r = FetchResult(url="http://x", content="hi", title="T", length=2, truncated=False)
    assert r.status == 200
    assert r.download_filename is None
    assert r.actions_available is None


# --- Engine factory (hexagonal: swap the engine, no browser) ---

def test_make_engine_default_is_invisible(monkeypatch):
    monkeypatch.delenv("FETCH_UX_ENGINE", raising=False)
    assert make_engine().name == "invisible"


def test_make_engine_selects_chrome(monkeypatch):
    monkeypatch.setenv("FETCH_UX_ENGINE", "chrome")
    assert make_engine().name == "chrome"


def test_make_engine_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("FETCH_UX_ENGINE", "chrome")
    assert make_engine(name="invisible").name == "invisible"


def test_make_engine_unknown_raises():
    with pytest.raises(ValueError, match="unknown FETCH_UX_ENGINE"):
        make_engine(name="webkit")


def test_engines_satisfy_port():
    """Both adapters expose the BrowserEngine interface."""
    for name in ("invisible", "chrome"):
        e = make_engine(name=name)
        for m in ("start", "new_page", "acquire_page", "release_page",
                  "capture_text", "dismiss_overlays", "stop"):
            assert callable(getattr(e, m)), f"{name} missing {m}"


# --- Chrome engine: GPU args + config (no browser) ---

def test_chrome_args_empty_without_render_node(monkeypatch):
    """No DRM render node → no GPU flags (safe no-op on GPU-less hosts)."""
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert _chrome_args() == []


def test_chrome_args_present_with_render_node(monkeypatch):
    """Render node present → drive WebGL through it via ANGLE/EGL."""
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/dri/renderD128")
    args = _chrome_args()
    assert "--use-gl=angle" in args
    assert "--use-angle=gl-egl" in args


def test_chrome_headless_default(monkeypatch):
    monkeypatch.delenv("FETCH_UX_HEADLESS", raising=False)
    assert ChromeEngine()._headless is False


def test_chrome_headless_env(monkeypatch):
    monkeypatch.setenv("FETCH_UX_HEADLESS", "1")
    assert ChromeEngine()._headless is True


def test_chrome_remove_dir_safe_on_none():
    ChromeEngine._remove_dir(None)  # must not raise


def test_chrome_remove_dir_removes_profile(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "cookies").write_text("x")
    ChromeEngine._remove_dir(str(d))
    assert not d.exists()


# --- FetchClient recycle-TTL config (engine injected, no browser) ---

def test_recycle_ttl_default(monkeypatch):
    monkeypatch.delenv("FETCH_UX_RECYCLE_TTL", raising=False)
    assert FetchClient(engine=object())._recycle_ttl == 86400


def test_recycle_ttl_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("FETCH_UX_RECYCLE_TTL", "999")
    assert FetchClient(engine=object(), recycle_ttl_sec=123)._recycle_ttl == 123


def test_recycle_ttl_garbage_falls_back(monkeypatch):
    """Non-integer env must not crash client creation."""
    monkeypatch.setenv("FETCH_UX_RECYCLE_TTL", "1h")
    assert FetchClient(engine=object())._recycle_ttl == 86400


def test_recycle_ttl_negative_clamped():
    assert FetchClient(engine=object(), recycle_ttl_sec=-5)._recycle_ttl == 0


# --- recycle drains the fetch gate (don't stop the browser mid-fetch) ---

class _FakeEngine:
    """Records start/stop calls; `concurrency` drives the client's fetch gate."""
    name = "fake"
    concurrency = 2

    def __init__(self):
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


@pytest.mark.asyncio
async def test_recycle_waits_for_inflight_fetches():
    """_recycle_browser must not stop the engine while a fetch is mid-flight (its
    page would be invalidated). It drains every semaphore permit first, so an
    in-flight fetch holding a permit blocks the recycle until it finishes."""
    eng = _FakeEngine()
    client = FetchClient(engine=eng)
    assert client._concurrency == 2

    # Simulate one in-flight fetch holding a permit.
    await client._semaphore.acquire()

    recycle = asyncio.create_task(client._recycle_browser())
    await asyncio.sleep(0.05)  # let recycle try to drain
    # It can't acquire both permits (we hold one), so it must NOT have stopped yet.
    assert eng.stopped == 0
    assert not recycle.done()

    # The in-flight fetch completes → releases its permit.
    client._semaphore.release()
    await asyncio.wait_for(recycle, timeout=1.0)

    assert eng.stopped == 1 and eng.started == 1
    # Every permit released back — normal fetching resumes.
    assert client._semaphore._value == 2


# --- disk download capture (invisible engine's shared-dir path) ---

@pytest.mark.asyncio
async def test_collect_disk_download_picks_newest_completed(tmp_path):
    """A completed file that wasn't in the snapshot is returned; in-flight (.part)
    and pre-existing files are ignored."""
    (tmp_path / "old.csv").write_text("stale")          # pre-existing → in `before`
    before = set(os.listdir(tmp_path))
    (tmp_path / "report.csv").write_text("a,b")         # new + completed
    (tmp_path / "half.crdownload").write_text("...")    # new but in-flight
    got = await FetchClient._collect_disk_download(str(tmp_path), before, settle=0.1)
    assert got is not None
    path, name = got
    assert name == "report.csv"
    assert path.endswith("report.csv")


@pytest.mark.asyncio
async def test_collect_disk_download_none_when_nothing_new(tmp_path):
    (tmp_path / "old.csv").write_text("stale")
    before = set(os.listdir(tmp_path))
    got = await FetchClient._collect_disk_download(str(tmp_path), before, settle=0.05, timeout=0.3)
    assert got is None


def test_invisible_engine_exposes_download_lock():
    """The core serializes the shared-download-dir window on this lock; it must exist
    and be distinct from the clipboard/creation locks."""
    from fetch_ux.engines.invisible import InvisibleEngine
    e = InvisibleEngine()
    assert isinstance(e.download_lock, asyncio.Lock)
    assert e.download_lock is not e._clipboard_lock
    assert e.download_lock is not e._create_lock


def test_chrome_engine_has_no_download_lock():
    """Chromium uses the Playwright download event, not the shared-dir path, so it
    exposes neither download_dir nor download_lock — the core's getattr falls back."""
    assert getattr(ChromeEngine(), "download_dir", None) is None
    assert getattr(ChromeEngine(), "download_lock", None) is None


# --- _validate_url (SSRF guard) — IP literals/scheme/hostname need no DNS ---

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com",
    "gopher://example.com",
    "http://localhost/",
    "http://foo.local/",
    "http://bar.internal/",
    "http://127.0.0.1/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://[::1]/",
    "https://0.0.0.0/",
])
async def test_validate_url_rejects_unsafe(url):
    with pytest.raises(UnsafeUrl):
        await _validate_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://8.8.8.8/",       # public IP literal — no DNS needed
    "https://1.1.1.1/",
])
async def test_validate_url_allows_public_ip(url):
    assert await _validate_url(url) is None


# --- clipboard capture: a failed copy must never yield the previous page's text ---
#
# The OS clipboard is one global buffer that outlives pages, contexts and fetches,
# and nothing clears it. Before the sentinel, a Ctrl+C that didn't land left the
# previous fetch's text there and capture_text returned it under the new page's
# title — silently. These pin that shut.

class _FakePage:
    """Minimal Page stand-in: records keystrokes, never navigates."""

    def __init__(self):
        self.keys = []

    class _KB:
        def __init__(self, outer): self.outer = outer
        async def press(self, key): self.outer.keys.append(key)

    @property
    def keyboard(self):
        return self._KB(self)

    async def wait_for_timeout(self, ms):
        return


class _ClipEngine(BaseEngine):
    """Engine over a fake OS clipboard. `copy_works` toggles whether Ctrl+C lands."""

    def __init__(self, buffer="", copy_works=True):
        self.buffer = buffer          # survives across captures, like the real thing
        self.copy_works = copy_works
        self.page_text = "CURRENT PAGE TEXT"

    async def _write_clipboard(self, page, text):
        self.buffer = text

    async def _read_clipboard(self, page):
        return self.buffer

    async def select_all_and_copy(self, page):
        await super().select_all_and_copy(page)
        if self.copy_works:
            self.buffer = self.page_text


@pytest.mark.asyncio
async def test_capture_text_returns_page_text_when_copy_lands():
    eng = _ClipEngine(buffer="PREVIOUS PAGE TEXT", copy_works=True)
    assert await eng.capture_text(_FakePage()) == "CURRENT PAGE TEXT"


@pytest.mark.asyncio
async def test_capture_text_returns_empty_when_copy_silently_fails():
    """The regression: clipboard still holds the prior fetch, Ctrl+C does nothing."""
    eng = _ClipEngine(buffer="PREVIOUS PAGE TEXT", copy_works=False)
    got = await eng.capture_text(_FakePage())
    assert got == "", f"stale read leaked: {got!r}"
    assert "PREVIOUS" not in got


@pytest.mark.asyncio
async def test_capture_text_returns_empty_when_read_raises():
    """A navigation mid-capture destroys the execution context — Chrome's
    page.evaluate raises. That's a failed capture, not a reason to guess."""
    eng = _ClipEngine(buffer="PREVIOUS PAGE TEXT")

    async def boom(page):
        raise RuntimeError("Execution context was destroyed")
    eng._read_clipboard = boom
    assert await eng.capture_text(_FakePage()) == ""


@pytest.mark.asyncio
async def test_capture_text_returns_empty_when_baseline_write_fails():
    """No sentinel written (e.g. xclip missing) → the read can't be trusted."""
    eng = _ClipEngine(buffer="PREVIOUS PAGE TEXT")

    async def boom(page, text):
        raise FileNotFoundError("xclip")
    eng._write_clipboard = boom
    assert await eng.capture_text(_FakePage()) == ""


@pytest.mark.asyncio
async def test_capture_text_sentinel_is_not_leaked_as_content():
    eng = _ClipEngine(copy_works=False)
    assert BaseEngine._SENTINEL_PREFIX not in await eng.capture_text(_FakePage())


@pytest.mark.asyncio
async def test_capture_text_serializes_concurrent_captures():
    """One OS clipboard per display: without the lock, two captures interleave and
    each reads the other's text. The lock must hold across stamp→copy→read."""
    eng = _ClipEngine()
    inside = 0
    overlapped = False
    real_copy = eng.select_all_and_copy

    async def watched(page):
        nonlocal inside, overlapped
        inside += 1
        if inside > 1:
            overlapped = True
        await asyncio.sleep(0.01)
        await real_copy(page)
        inside -= 1
    eng.select_all_and_copy = watched

    await asyncio.gather(*(eng.capture_text(_FakePage()) for _ in range(4)))
    assert not overlapped


# --- DOM fallback: what makes congress.gov work again ---

class _FallbackPage:
    """Page whose clipboard path is dead but whose DOM reads fine."""

    def __init__(self, dom_text="REAL PAGE BODY"):
        self.dom_text = dom_text

    def locator(self, sel):
        assert sel == "body"
        page = self

        class _Loc:
            async def inner_text(self, timeout=None):
                return page.dom_text
        return _Loc()


class _DeadClipboardEngine:
    async def capture_text(self, page):
        return ""          # copy provably didn't land


@pytest.mark.asyncio
async def test_capture_falls_back_to_dom_when_clipboard_dead():
    client = FetchClient(engine=_DeadClipboardEngine())
    assert await client._capture(_FallbackPage()) == "REAL PAGE BODY"


@pytest.mark.asyncio
async def test_capture_prefers_clipboard_when_it_works():
    """The DOM read misses closed Shadow DOM, so it stays the fallback, not the path."""
    class _GoodEngine:
        async def capture_text(self, page):
            return "CLIPBOARD TEXT"
    client = FetchClient(engine=_GoodEngine())
    assert await client._capture(_FallbackPage()) == "CLIPBOARD TEXT"


@pytest.mark.asyncio
async def test_capture_returns_empty_when_both_paths_fail():
    class _DeadPage:
        def locator(self, sel):
            raise RuntimeError("page closed")
    client = FetchClient(engine=_DeadClipboardEngine())
    assert await client._capture(_DeadPage()) == ""


# --- bot-wall interstitials (SSRN's Cloudflare turnstile) ---

@pytest.mark.parametrize("title,content", [
    ("Just a moment...", "papers.ssrn.com\nPerforming security verification"),
    ("", "Checking your browser before accessing the site"),
    ("Attention Required! | Cloudflare", "blocked"),
    ("Access", "Please enable JavaScript and cookies to continue"),
])
def test_looks_blocked_detects_interstitials(title, content):
    assert _looks_blocked(title, content)


@pytest.mark.parametrize("title,content", [
    ("Text - H.R.9340 - 119th Congress", "To amend the Federal Power Act..."),
    ("", ""),
    ("A moment in history", "An article about a just cause and a moment of change."),
])
def test_looks_blocked_ignores_real_pages(title, content):
    assert not _looks_blocked(title, content)


def test_looks_blocked_only_probes_the_head_of_the_content():
    """A page that merely quotes the phrase deep in its body isn't an interstitial."""
    assert not _looks_blocked("Real Article", "x" * 3000 + "just a moment")
