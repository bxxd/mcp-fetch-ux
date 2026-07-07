.PHONY: server kill logs ping help setup install chrome

ENV := $(shell pwd | grep -q '/prod/' && echo prod || echo dev)

help:
	@echo "mcp-fetch-ux — Playwright-based web fetch MCP server"
	@echo ""
	@echo "Detected environment: $(ENV)"
	@echo ""
	@echo "  make setup   - Install deps + the invisible engine (stealth Firefox, default)"
	@echo "  make chrome  - Also install the chrome engine (FETCH_UX_ENGINE=chrome)"
	@echo "  make server  - Start server (reads PORT from .env)"
	@echo "  make kill    - Stop server"
	@echo "  make logs    - Tail server logs"
	@echo "  make ping    - Health check"
	@echo "  make install - setup + install 'fetch' CLI to ~/.local/bin/"

setup:
	@echo "→ Installing dependencies..."
	@poetry install
	@echo "→ Installing stealth Firefox (invisible engine, the default; ~100MB)..."
	@poetry run python -m invisible_playwright fetch
	@echo "✓ Setup complete (run under xvfb — the engine is headed)."
	@echo "  Optional chrome engine (FETCH_UX_ENGINE=chrome): run 'make chrome'"

chrome:
	@echo "→ Installing real Google Chrome for the chrome engine..."
	@poetry run patchright install chrome
	@echo "✓ Chrome ready — set FETCH_UX_ENGINE=chrome to use it"

install: setup
	@mkdir -p ~/.local/bin
	@SRC_DIR=$$(pwd) && \
	echo '#!/usr/bin/env bash' > ~/.local/bin/fetch && \
	echo "cd \"$$SRC_DIR\" && exec ./cli \"\$$@\"" >> ~/.local/bin/fetch && \
	chmod +x ~/.local/bin/fetch
	@echo "✓ Installed 'fetch' to ~/.local/bin/fetch"

server:
	@echo "Stopping existing server..."
	@set -a; [ -f .env ] && . ./.env; set +a; \
		curl -sf --max-time 5 -X POST http://127.0.0.1:$${PORT:-5006}/shutdown >/dev/null 2>&1 || true
	@sleep 1
	@if [ -f logs/server.pid ]; then \
		kill $$(cat logs/server.pid) 2>/dev/null || true; \
		sleep 1; \
		kill -9 $$(cat logs/server.pid) 2>/dev/null || true; \
	fi
	@rm -f logs/server.pid
	@VENV=$$(poetry env info --path 2>/dev/null); \
		[ -n "$$VENV" ] && pkill -9 -f "$$VENV" 2>/dev/null || true
	@sleep 1
	@mkdir -p logs
	@set -a; [ -f .env ] && . ./.env; set +a; \
		echo "Starting mcp-fetch-ux on port $${PORT:-5006} (logs/server.log)..."; \
		nohup xvfb-run -a poetry run python -m mcp_fetch_ux.server_http > logs/server.log 2>&1 & \
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
	@set -a; [ -f .env ] && . ./.env; set +a; \
		curl -sf --max-time 5 -X POST http://127.0.0.1:$${PORT:-5006}/shutdown >/dev/null 2>&1 || true
	@sleep 1
	@if [ -f logs/server.pid ]; then \
		kill $$(cat logs/server.pid) 2>/dev/null || true; \
		sleep 1; \
		kill -9 $$(cat logs/server.pid) 2>/dev/null || true; \
		rm -f logs/server.pid; \
	fi
	@VENV=$$(poetry env info --path 2>/dev/null); \
		[ -n "$$VENV" ] && pkill -9 -f "$$VENV" 2>/dev/null || true
	@echo "Server stopped"

logs:
	@tail -f logs/server.log

ping:
	@set -a; [ -f .env ] && . ./.env; set +a; \
		curl -s http://127.0.0.1:$${PORT:-5006}/ping | python3 -m json.tool
