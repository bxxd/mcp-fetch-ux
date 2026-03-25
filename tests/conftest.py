"""Shared fixtures for mcp-fetch-ux tests."""

import asyncio
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
import pytest_asyncio

from fetch_ux import FetchClient


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FixtureHandler(SimpleHTTPRequestHandler):
    """Serves files from the fixtures directory."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

    def log_message(self, format, *args):
        pass  # Silence request logs during tests


@pytest.fixture(scope="session")
def local_server():
    """Run a local HTTP server serving test fixtures."""
    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest_asyncio.fixture
async def client():
    """A FetchClient instance with browser launched."""
    c = FetchClient(timeout_ms=10_000)
    await c.start()
    yield c
    await c.stop()
