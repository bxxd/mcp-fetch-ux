"""Unit tests — pure functions, no browser needed."""

import os
import tempfile

import pytest

from fetch_ux.client import _read_download, FetchClient, FetchResult
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


# --- FetchClient config: real-Chrome engine + GPU args (no browser) ---


def test_chrome_args_empty_without_render_node(monkeypatch):
    """No DRM render node → no GPU flags (safe no-op on GPU-less hosts)."""
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert FetchClient._chrome_args() == []


def test_chrome_args_present_with_render_node(monkeypatch):
    """Render node present → drive WebGL through it via ANGLE/EGL."""
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/dri/renderD128")
    args = FetchClient._chrome_args()
    assert "--use-gl=angle" in args
    assert "--use-angle=gl-egl" in args


def test_init_defaults(monkeypatch):
    """Unset env → headed (xvfb-friendly) with a 1-day cookie TTL."""
    monkeypatch.delenv("FETCH_UX_HEADLESS", raising=False)
    monkeypatch.delenv("FETCH_UX_COOKIE_TTL", raising=False)
    c = FetchClient()
    assert c._headless is False
    assert c._cookie_ttl == 86400


def test_init_explicit_args_win_over_env(monkeypatch):
    """Explicit kwargs override env vars."""
    monkeypatch.setenv("FETCH_UX_HEADLESS", "0")
    monkeypatch.setenv("FETCH_UX_COOKIE_TTL", "999")
    c = FetchClient(headless=True, cookie_ttl_sec=123)
    assert c._headless is True
    assert c._cookie_ttl == 123


def test_init_cookie_ttl_from_env(monkeypatch):
    monkeypatch.setenv("FETCH_UX_COOKIE_TTL", "3600")
    assert FetchClient()._cookie_ttl == 3600


def test_init_cookie_ttl_garbage_falls_back(monkeypatch):
    """Non-integer env must not crash client creation."""
    monkeypatch.setenv("FETCH_UX_COOKIE_TTL", "1h")
    assert FetchClient()._cookie_ttl == 86400


def test_init_cookie_ttl_blank_falls_back(monkeypatch):
    monkeypatch.setenv("FETCH_UX_COOKIE_TTL", "")
    assert FetchClient()._cookie_ttl == 86400


def test_init_cookie_ttl_negative_clamped(monkeypatch):
    monkeypatch.delenv("FETCH_UX_COOKIE_TTL", raising=False)
    assert FetchClient(cookie_ttl_sec=-5)._cookie_ttl == 0


def test_init_headless_env_toggle(monkeypatch):
    monkeypatch.setenv("FETCH_UX_HEADLESS", "1")
    assert FetchClient()._headless is True


def test_remove_dir_safe_on_none():
    FetchClient._remove_dir(None)  # must not raise


def test_remove_dir_removes_profile(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "cookies").write_text("x")
    FetchClient._remove_dir(str(d))
    assert not d.exists()


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
