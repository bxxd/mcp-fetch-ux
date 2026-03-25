"""HTTP/SSE MCP server for fetch tool."""

import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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

    # Quiet noisy loggers
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# MCP server
mcp_server = Server("mcp-fetch-ux")
sse = SseServerTransport("/messages")


@mcp_server.list_tools()
async def list_tools():
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await handlers.call_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


# HTTP routes
async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )


async def handle_messages(request: Request):
    await sse.handle_post_message(request.scope, request.receive, request._send)


async def handle_ping(request: Request):
    return JSONResponse({"status": "ok"})


async def handle_shutdown(request: Request):
    await handlers.shutdown_client()
    return JSONResponse({"status": "shutdown"})


async def on_startup():
    logger.info(f"Starting mcp-fetch-ux on port {os.getenv('PORT', '5006')}")


async def on_shutdown():
    await handlers.shutdown_client()
    logger.info("Server stopped")


app = Starlette(
    routes=[
        Route("/ping", handle_ping, methods=["GET"]),
        Route("/sse", handle_sse, methods=["GET"]),
        Route("/messages", handle_messages, methods=["POST"]),
        Route("/shutdown", handle_shutdown, methods=["POST"]),
    ],
    on_startup=[on_startup],
    on_shutdown=[on_shutdown],
)


def main():
    setup_logging()
    port = int(os.getenv("PORT", "5006"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
