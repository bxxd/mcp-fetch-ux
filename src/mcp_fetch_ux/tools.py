"""MCP tool definitions."""

TOOLS = [
    {
        "name": "fetch",
        "description": (
            "Fetch a URL, render JavaScript, and return the content as markdown. "
            "Uses a real browser (Playwright) so JS-heavy sites work. "
            "Content is extracted via Readability (same as Firefox Reader Mode) "
            "and converted to clean markdown. "
            "30-second timeout — never hangs.\n\n"
            "For large pages, content is paginated. Use start_index to read more."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum characters to return. Default 5000.",
                    "default": 5000,
                },
                "start_index": {
                    "type": "integer",
                    "description": "Start content at this character index (for pagination after truncation).",
                    "default": 0,
                },
                "raw": {
                    "type": "boolean",
                    "description": "Return raw HTML instead of extracted markdown.",
                    "default": False,
                },
            },
            "required": ["url"],
        },
    }
]
