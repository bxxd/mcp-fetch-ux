"""MCP tool definitions."""

TOOLS = [
    {
        "name": "pithy-fetch",
        "description": (
            "pithy-fetch - retrieve text from a URL\n\n"
            "SYNOPSIS\n"
            "  pithy-fetch(url, [actions], [max_length], [start_index], [raw])\n\n"
            "USAGE\n"
            "  1. Call with a URL. Get back page text and available actions.\n"
            "  2. To interact, pass actions from the response back in the actions parameter.\n"
            "  3. Chain calls to navigate multi-page workflows (search, paginate, download).\n"
            "  4. If response is truncated, call again with start_index to continue reading.\n"
            "  5. If the site blocks you (403), switch to grok_blocked_pithy-fetch.\n\n"
            "RETURNS\n"
            "  Page text + list of discovered actions (buttons, links, forms).\n"
            "  File downloads (CSV, PDF, XLSX) return file content directly.\n\n"
            "LIMITS\n"
            "  30-second timeout. No cookie persistence between calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch.",
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Ordered page interactions to execute before content capture. "
                        "Use any combination to navigate multi-step workflows — "
                        "the response includes discovered actions you can pass back on the next call.\n\n"
                        "Available actions: click, fill, wait, select, scroll. "
                        "Selectors follow CSS syntax or text= prefix for visible text matching.\n\n"
                        "Examples:\n"
                        '  {"action": "click", "selector": "text=Download CSV"}\n'
                        '  {"action": "fill", "selector": "input[name=q]", "value": "AAPL"}\n'
                        '  {"action": "wait", "selector": ".results"}\n'
                        '  {"action": "wait", "timeout": 2000}\n'
                        '  {"action": "select", "selector": "select#period", "value": "annual"}\n'
                        '  {"action": "scroll", "direction": "bottom"}'
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
                    "description": "Maximum characters to return. Default 50000. Truncated on line boundary.",
                    "default": 50000,
                },
                "start_index": {
                    "type": "integer",
                    "description": "Character offset to resume from after truncation.",
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
