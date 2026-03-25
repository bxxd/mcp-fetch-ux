"""MCP tool definitions."""

TOOLS = [
    {
        "name": "fetch",
        "description": (
            "Fetch a URL using a real browser (Playwright). Renders JavaScript, "
            "captures Shadow DOM content via clipboard API.\n\n"
            "Optionally interact with the page before capturing: click buttons, "
            "fill inputs, wait for elements. Interactions run in order.\n\n"
            "If an interaction triggers a file download (CSV, PDF, etc.), the "
            "file content is returned instead of the page text.\n\n"
            "For large pages, content is paginated. Use start_index to read more.\n\n"
            "30-second timeout — never hangs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Actions to perform on the page before capturing content. "
                        "Each action is an object with 'action' and parameters. "
                        "Actions run in order. Supported actions:\n"
                        '- {"action": "click", "selector": "text=Download CSV"}\n'
                        '- {"action": "click", "selector": "#submit-button"}\n'
                        '- {"action": "fill", "selector": "input[name=search]", "value": "query"}\n'
                        '- {"action": "wait", "selector": ".results-table"}\n'
                        '- {"action": "wait", "timeout": 2000}\n'
                        '- {"action": "select", "selector": "select#country", "value": "US"}\n'
                        '- {"action": "scroll", "direction": "bottom"}'
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["click", "fill", "wait", "select", "scroll"],
                            },
                            "selector": {"type": "string"},
                            "value": {"type": "string"},
                            "timeout": {"type": "integer"},
                            "direction": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum characters to return. Default 50000. Truncates on line boundary.",
                    "default": 50000,
                },
                "start_index": {
                    "type": "integer",
                    "description": "Start content at this character index (for pagination after truncation).",
                    "default": 0,
                },
                "raw": {
                    "type": "boolean",
                    "description": "Return raw HTML instead of extracted text.",
                    "default": False,
                },
            },
            "required": ["url"],
        },
    }
]
