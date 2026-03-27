"""HTTP MCP server for fetch tool — streamable HTTP transport (stateless)."""

import logging
import os
import sys
from contextlib import asynccontextmanager

import anyio
import uvicorn
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import handlers
from .tools import TOOLS

load_dotenv()

# Logging
logger = logging.getLogger("mcp_fetch_ux")


class MillisecondFormatter(logging.Formatter):
    default_msec_format = "%s.%03d"


def setup_logging():
    fmt = MillisecondFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("fetch_ux").setLevel(logging.INFO)

    # Quiet noisy loggers
    logging.getLogger("patchright").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


# MCP server
mcp_server = Server("mcp-fetch-ux")

# Streamable HTTP — stateless, survives server restarts
transport = StreamableHTTPServerTransport(
    mcp_session_id=None,
    is_json_response_enabled=True,
)

# SSE — legacy fallback for clients that don't support streamable HTTP
sse = SseServerTransport("/messages")


@mcp_server.list_tools()
async def list_tools():
    return [Tool(**t) for t in TOOLS]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await handlers.call_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


# SSE routes (legacy)
async def handle_sse(request: Request) -> Response:
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )
    return Response()


# HTTP routes
async def handle_ping(request: Request):
    return JSONResponse({"status": "ok"})


async def handle_shutdown(request: Request):
    await handlers.shutdown_client()
    return JSONResponse({"status": "shutdown"})


@asynccontextmanager
async def lifespan(app):
    """Start MCP server in background, keep transport alive for all requests."""
    logger.info(f"Starting mcp-fetch-ux on port {os.getenv('PORT', '5006')}")
    async with transport.connect() as (read_stream, write_stream):
        async with anyio.create_task_group() as tg:
            async def run_server():
                await mcp_server.run(
                    read_stream, write_stream, mcp_server.create_initialization_options(),
                    stateless=True,
                )

            tg.start_soon(run_server)
            yield
            tg.cancel_scope.cancel()
    await handlers.shutdown_client()
    logger.info("Server stopped")


app = Starlette(
    routes=[
        Route("/ping", handle_ping, methods=["GET"]),
        Mount("/mcp", app=transport.handle_request),
        Route("/sse", handle_sse),
        Mount("/messages", app=sse.handle_post_message),
        Route("/shutdown", handle_shutdown, methods=["POST"]),
    ],
    lifespan=lifespan,
)


def main():
    setup_logging()
    port = int(os.getenv("PORT", "5006"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
