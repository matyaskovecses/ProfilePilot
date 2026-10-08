"""ProfilePilot: multi-profile native Chrome for AI agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

if TYPE_CHECKING:
    from .client import ProfilePilot

__all__ = ["ProfilePilot", "__version__"]


def __getattr__(name: str) -> Any:
    # Lazy so that `import profilepilot` (e.g. by the browser host process) stays light.
    if name == "ProfilePilot":
        from .client import ProfilePilot

        return ProfilePilot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
