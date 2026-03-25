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
