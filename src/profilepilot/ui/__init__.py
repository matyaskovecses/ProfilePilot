"""ProfilePilot Manager: the local web app for managing profiles, proxies and identities by hand.

Run it with ``profilepilot ui`` or ``python -m profilepilot.ui``. The pieces:

* :mod:`.launcher` - single-instance server start, the app window, shortcuts (``--install-shortcut``)
* :mod:`.server` - the Starlette app and its security layer (token, Host / Origin checks, CSP)
* :mod:`.api` - the REST API (``/api/...``) and Server-Sent Events (``/api/events``)
* :mod:`.events` - the file-polling event hub
* :mod:`.cdp` - raw DevTools helpers (thumbnails, tabs, window focus)
* :mod:`.shortcut` - the generated icon and the OS shortcuts
* ``static/`` - the frontend (plain ES modules and CSS; no build step, no CDN)
"""

from __future__ import annotations

from typing import Any


def main(argv: Any = None) -> int:
    """``profilepilot ui`` entry point (see :func:`profilepilot.ui.launcher.main`)."""
    from .launcher import main as _main

    return _main(argv)


__all__ = ["main"]
