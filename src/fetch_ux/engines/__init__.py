"""Browser-engine port + adapter factory (hexagonal: swap the engine, not the core).

`FetchClient` depends only on the `BrowserEngine` protocol below. Pick the adapter
with `FETCH_UX_ENGINE`:

  - "invisible" (default) — stealth Firefox (invisible_playwright). C++-patched
    fingerprint, passes reCAPTCHA v3 incl. Google SERP. Needs a display (xvfb).
  - "chrome" — real Google Chrome via Patchright + persistent context + optional
    GPU/EGL. Beats Cloudflare/Datadome-class walls; not Google's reCAPTCHA SERP.

Adapters are imported lazily, so a venv only needs the deps for the engine it runs.
"""

import os
from typing import Protocol, runtime_checkable

from fetch_ux.engines.base import BaseEngine

__all__ = ["BrowserEngine", "BaseEngine", "make_engine"]


@runtime_checkable
class BrowserEngine(Protocol):
    """A warm browser the core can open pages on + the browser interactions that
    differ by engine. Adapters own the lifecycle and clipboard read-back; shared
    interactions (overlay dismissal, select+copy) live in BaseEngine."""

    name: str

    async def start(self) -> None:
        """Launch the browser (kept warm for the process)."""
        ...

    async def new_page(self):
        """Return a ready Playwright Page to fetch on. The core closes it."""
        ...

    async def dismiss_overlays(self, page) -> bool:
        """Click through a cookie/consent overlay if one blocks the page; returns
        True if it dismissed one."""
        ...

    async def capture_text(self, page) -> str:
        """Select-all + copy the rendered page (incl. Shadow DOM) and return the
        text. The select/copy is shared (BaseEngine.select_all_and_copy); the
        read-back is engine-specific — Chromium uses clipboard.readText()
        (permission-granted), Firefox pastes into a textarea (its readText is
        gated by user activation)."""
        ...

    async def stop(self) -> None:
        """Close the browser and release resources."""
        ...


def make_engine(timeout_ms: int = 60_000, name: str | None = None) -> "BrowserEngine":
    """Build the engine named by `name` (or FETCH_UX_ENGINE, default 'invisible')."""
    name = (name or os.environ.get("FETCH_UX_ENGINE", "invisible")).strip().lower()
    if name == "invisible":
        from fetch_ux.engines.invisible import InvisibleEngine
        return InvisibleEngine(timeout_ms)
    if name == "chrome":
        from fetch_ux.engines.chrome import ChromeEngine
        return ChromeEngine(timeout_ms)
    raise ValueError(
        f"unknown FETCH_UX_ENGINE: {name!r} (expected 'invisible' or 'chrome')"
    )
