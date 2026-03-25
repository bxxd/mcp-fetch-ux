"""Unit tests — pure functions, no browser needed."""

import tempfile
from pathlib import Path

import pytest

from fetch_ux.client import _read_download, extract_content_from_html, FetchResult
from mcp_fetch_ux.tools import TOOLS
from mcp_fetch_ux.handlers import call_tool


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


# --- extract_content_from_html ---

def test_extract_content_basic():
    html = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"
    content, title = extract_content_from_html(html)
    assert "Hello world" in content


def test_extract_content_empty():
    content, title = extract_content_from_html("<html><body></body></html>")
    assert content == ""
    assert title == ""


# --- Tool schema ---

def test_tool_schema_valid():
    assert len(TOOLS) == 1
    tool = TOOLS[0]
    assert tool["name"] == "webfetch"
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
        assert tool.name == "webfetch"


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
