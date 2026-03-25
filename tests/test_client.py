"""Integration tests — real Playwright against local fixture server."""

import json

import pytest


@pytest.mark.asyncio
async def test_fetch_simple_page(client, local_server):
    result = await client.fetch(f"{local_server}/simple.html", max_length=50000)
    assert result.status == 200
    assert result.title == "Simple Test Page"
    assert "Hello Fetch" in result.content
    assert "simple test page" in result.content.lower()


@pytest.mark.asyncio
async def test_fetch_returns_actions(client, local_server):
    result = await client.fetch(f"{local_server}/interactive.html", max_length=50000)
    assert result.actions_available is not None
    actions_text = "\n".join(result.actions_available)
    # Should discover the search button, input, select, and links
    assert "Search" in actions_text or "search" in actions_text


@pytest.mark.asyncio
async def test_fetch_js_rendered_content(client, local_server):
    """Clipboard polling should wait for JS to render."""
    result = await client.fetch(f"{local_server}/js_render.html", max_length=50000)
    assert "JS Content Loaded Successfully" in result.content
    assert "Loading..." not in result.content


@pytest.mark.asyncio
async def test_fetch_cookie_dismissal(client, local_server):
    """Cookie banner should be auto-dismissed."""
    result = await client.fetch(f"{local_server}/cookie.html", max_length=50000)
    assert "Content Behind Cookies" in result.content


@pytest.mark.asyncio
async def test_fetch_raw_html(client, local_server):
    result = await client.fetch(f"{local_server}/simple.html", raw=True, max_length=50000)
    assert "<h1>" in result.content
    assert "</html>" in result.content


@pytest.mark.asyncio
async def test_fetch_truncation(client, local_server):
    result = await client.fetch(f"{local_server}/long.html", max_length=500)
    assert result.truncated
    assert "<truncated>" in result.content
    assert "start_index=" in result.content
    assert result.length > 500


@pytest.mark.asyncio
async def test_fetch_pagination(client, local_server):
    """Fetch with start_index should return content from that offset."""
    full = await client.fetch(f"{local_server}/simple.html", max_length=0)
    partial = await client.fetch(f"{local_server}/simple.html", start_index=10, max_length=0)
    assert len(partial.content) == len(full.content) - 10


@pytest.mark.asyncio
async def test_fetch_start_index_past_end(client, local_server):
    result = await client.fetch(f"{local_server}/simple.html", start_index=999999, max_length=50000)
    assert result.content == ""


@pytest.mark.asyncio
async def test_fill_action(client, local_server):
    """Fill an input then click a button, verify result appears."""
    result = await client.fetch(
        f"{local_server}/interactive.html",
        actions=[
            {"action": "fill", "selector": "#search-input", "value": "test query"},
            {"action": "click", "selector": "#search-btn"},
        ],
        max_length=50000,
    )
    assert "Results for: test query" in result.content


@pytest.mark.asyncio
async def test_scroll_action(client, local_server):
    """Scroll action should not error."""
    result = await client.fetch(
        f"{local_server}/long.html",
        actions=[{"action": "scroll", "direction": "bottom"}],
        max_length=50000,
    )
    assert result.status == 200


@pytest.mark.asyncio
async def test_stealth_fingerprints(client, local_server):
    """Verify anti-detection measures are active."""
    result = await client.fetch(f"{local_server}/stealth.html", max_length=50000)
    # Parse the JSON from the page
    data = json.loads(result.content.strip())
    assert data["webdriver"] is False, "navigator.webdriver should be false"
    assert data["plugins_length"] > 0, "navigator.plugins should not be empty"
    assert "en-US" in data["languages"], "navigator.languages should include en-US"
    assert data["chrome"] is True, "window.chrome should exist"
    assert data["chrome_runtime"] is True, "window.chrome.runtime should exist"


@pytest.mark.asyncio
async def test_404_status(client, local_server):
    result = await client.fetch(f"{local_server}/nonexistent.html", max_length=50000)
    assert result.status == 404


@pytest.mark.asyncio
async def test_action_discovery_finds_inputs(client, local_server):
    result = await client.fetch(f"{local_server}/interactive.html", max_length=50000)
    actions = result.actions_available or []
    actions_text = "\n".join(actions)
    assert "fill:" in actions_text.lower() or "search" in actions_text.lower()


@pytest.mark.asyncio
async def test_action_discovery_finds_select(client, local_server):
    result = await client.fetch(f"{local_server}/interactive.html", max_length=50000)
    actions = result.actions_available or []
    actions_text = "\n".join(actions)
    assert "select:" in actions_text.lower() or "filter" in actions_text.lower()


@pytest.mark.asyncio
async def test_action_discovery_finds_download_links(client, local_server):
    result = await client.fetch(f"{local_server}/interactive.html", max_length=50000)
    actions = result.actions_available or []
    actions_text = "\n".join(actions)
    assert "download" in actions_text.lower() or "csv" in actions_text.lower()
