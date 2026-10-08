"""Browser runtime: launching the real Chrome per profile, the host process and its control API.

Modules:

* :mod:`.flags`   - Chrome command line (``build_chrome_args``)
* :mod:`.prefs`   - user-data-dir preparation and "profile in use" probing
* :mod:`.control` - the host's token-protected control API, its client and DevTools helpers
* :mod:`.host`    - the detached per-profile host process (``python -m profilepilot.browser.host``)
* :mod:`.runtime` - ``RuntimeManager``: start / stop / discover running profiles
* :mod:`.winjob`  - Windows kill-on-close job object

Exports are resolved lazily so that importing this package (e.g. by the host process) stays light.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .control import ControlCallError, control_call
    from .flags import build_chrome_args
    from .prefs import prepare_user_data_dir, profile_in_use
    from .runtime import RuntimeManager

__all__ = [
    "ControlCallError",
    "RuntimeManager",
    "build_chrome_args",
    "control_call",
    "prepare_user_data_dir",
    "profile_in_use",
]

_EXPORTS = {
    "ControlCallError": "control",
    "control_call": "control",
    "build_chrome_args": "flags",
    "prepare_user_data_dir": "prefs",
    "profile_in_use": "prefs",
    "RuntimeManager": "runtime",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module}"), name)
