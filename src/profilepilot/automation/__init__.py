"""Browser automation over CDP (Playwright), page content for models, and cookie conversions.

Submodules are imported lazily so that ``profilepilot.automation.cookies`` does not pull in
Playwright.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .content import (
        InvalidTargetError,
        RefNotFoundError,
        extract,
        html_to_markdown,
        normalize_ref,
        paginate,
        read_page,
        snapshot,
    )
    from .manager import BrowserManager, ProfileSession

_EXPORTS = {
    "BrowserManager": "manager",
    "ProfileSession": "manager",
    "InvalidTargetError": "content",
    "RefNotFoundError": "content",
    "extract": "content",
    "html_to_markdown": "content",
    "normalize_ref": "content",
    "paginate": "content",
    "read_page": "content",
    "snapshot": "content",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module}"), name)
