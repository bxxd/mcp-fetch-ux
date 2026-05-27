"""Unit tests — pure functions, no browser needed."""

import asyncio
import os
import tempfile

import pytest

from fetch_ux.client import _read_download, FetchClient, FetchResult
from fetch_ux.engines import make_engine
from fetch_ux.engines.chrome import _chrome_args, ChromeEngine
from mcp_fetch_ux.tools import TOOLS
from mcp_fetch_ux.handlers import call_tool, _validate_url, UnsafeUrl


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
    assert len(TOOLS) == 1
    tool = TOOLS[0]
    assert tool["name"] == "read_webpage"
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
    for t in TOOLS:
        tool = Tool(**t)
        assert tool.name == "read_webpage"


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
        for m in ("start", "new_page", "capture_text", "dismiss_overlays", "stop"):
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
