.PHONY: server kill logs ping help setup

ENV := $(shell pwd | grep -q '/prod/' && echo prod || echo dev)

help:
	@echo "mcp-fetch-ux — Playwright-based web fetch MCP server"
	@echo ""
	@echo "Detected environment: $(ENV)"
	@echo ""
	@echo "  make setup   - Install dependencies + Playwright browsers"
	@echo "  make server  - Start server (reads PORT from .env)"
	@echo "  make kill    - Stop server"
	@echo "  make logs    - Tail server logs"
	@echo "  make ping    - Health check"

setup:
	@echo "→ Installing dependencies..."
	@poetry install
	@echo "→ Installing Playwright browsers..."
	@poetry run playwright install chromium
	@echo "✓ Setup complete"

server:
	@echo "Stopping existing server..."
	@if [ -f logs/server.pid ]; then \
		kill $$(cat logs/server.pid) 2>/dev/null; \
		sleep 1; \
		kill -9 $$(cat logs/server.pid) 2>/dev/null; \
	fi
	@rm -f logs/server.pid
	@mkdir -p logs
	@set -a; [ -f .env ] && . ./.env; set +a; \
		echo "Starting mcp-fetch-ux on port $${PORT:-5006} (logs/server.log)..."; \
		nohup poetry run python -m mcp_fetch_ux.server_http > logs/server.log 2>&1 & \
		echo $$! > logs/server.pid
	@sleep 3
	@set -a; [ -f .env ] && . ./.env; set +a; \
		PORT=$${PORT:-5006}; \
		for i in 1 2 3 4 5; do \
			if curl -s http://127.0.0.1:$$PORT/ping > /dev/null 2>&1; then \
				echo "Server ready on port $$PORT (PID $$(cat logs/server.pid))"; \
				exit 0; \
			fi; \
			sleep 2; \
		done; \
		echo "Server failed to start — check logs/server.log"; \
		exit 1

kill:
	@if [ -f logs/server.pid ]; then \
		kill $$(cat logs/server.pid) 2>/dev/null && rm logs/server.pid && echo "Server stopped" || echo "No server running"; \
	else \
		echo "No server running"; \
	fi

logs:
	@tail -f logs/server.log

ping:
	@set -a; [ -f .env ] && . ./.env; set +a; \
		curl -s http://127.0.0.1:$${PORT:-5006}/ping | python3 -m json.tool
