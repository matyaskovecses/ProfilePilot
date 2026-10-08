"""Optional integrations: the ShardX launcher backend and Scrapling sessions bound to profiles.

Nothing heavy is imported here. ``profilepilot.integrations.shardx`` needs only httpx;
``profilepilot.integrations.scrapling`` needs ``profilepilot[scrapling]`` (Scrapling with its
fetchers) and is imported on first access to one of its names.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY: dict[str, str] = {
    "ShardXClient": "shardx",
    "AsyncShardXClient": "shardx",
    "ShardXError": "shardx",
    "AsyncProfileSession": "scrapling",
    "ProfileSession": "scrapling",
    "ProfileFetcherSession": "scrapling",
    "fetcher_session": "scrapling",
    "fetch": "scrapling",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
