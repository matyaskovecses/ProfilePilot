"""ProfilePilot's MCP server (stdio for Claude / Codex / Cursor, Streamable HTTP for ChatGPT).

* :mod:`.app`            - server construction, lifespan state, shared tool helpers
* :mod:`.tools_profiles` - ``profile_*`` and ``proxy_*`` tools
* :mod:`.tools_browser`  - ``browser_*`` tools (Playwright over CDP)
* :mod:`.tools_data`     - ``cookies_*`` and ``http_fetch``
* :mod:`.tools_shardx`   - ``shardx_*`` tools (registered only when ShardX is enabled)
* :mod:`.http`           - remote mode (``serve --http``) with secret-path / bearer-token auth

Names are resolved lazily so that ``import profilepilot.server`` stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import AppState, create_server, serve_stdio
    from .http import build_http_app, serve_http

__all__ = ["AppState", "build_http_app", "create_server", "serve_http", "serve_stdio"]

_EXPORTS = {
    "AppState": "app",
    "create_server": "app",
    "serve_stdio": "app",
    "build_http_app": "http",
    "serve_http": "http",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module}"), name)
