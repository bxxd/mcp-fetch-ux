"""Server tests — HTTP endpoints and MCP protocol."""

import pytest
from starlette.testclient import TestClient

from mcp_fetch_ux.server_http import app


@pytest.fixture
def http_client():
    return TestClient(app)


def test_ping(http_client):
    resp = http_client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_mcp_endpoint_accepts_post():
    """POST to /mcp should be handled when lifespan is active."""
    # Use context manager to trigger lifespan (connect() must run first)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}',
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["serverInfo"]["name"] == "mcp-fetch-ux"


def test_shutdown_endpoint(http_client):
    resp = http_client.post("/shutdown")
    assert resp.status_code == 200
    assert resp.json() == {"status": "shutdown"}


def test_unknown_route_404(http_client):
    resp = http_client.get("/nonexistent")
    assert resp.status_code == 404


# --- /fetch JSON endpoint: 400 paths (no browser; validation precedes launch) ---

def test_fetch_invalid_json(http_client):
    resp = http_client.post("/fetch", content=b"not json")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_fetch_non_object_body(http_client):
    resp = http_client.post("/fetch", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_fetch_missing_url(http_client):
    resp = http_client.post("/fetch", json={"max_length": 100})
    assert resp.status_code == 400


def test_fetch_bad_max_length_type(http_client):
    resp = http_client.post("/fetch", json={"url": "http://example.com", "max_length": "50k"})
    assert resp.status_code == 400


def test_fetch_unsafe_url_rejected(http_client):
    # _validate_url rejects localhost before any browser launch → 400, not 500.
    resp = http_client.post("/fetch", json={"url": "http://localhost/"})
    assert resp.status_code == 400
    assert "error" in resp.json()
