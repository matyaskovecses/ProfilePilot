"""The CDP driver: patchright (default) or Playwright, behind one import point.

ProfilePilot drives the profile's own Chrome over CDP with the Playwright API. Two packages provide
that API, and every module imports the driver's names from here, never from ``playwright.*`` or
``patchright.*`` directly:

* **patchright** (the default when it is installed; Scrapling depends on it): a Playwright fork with
  the same Python API (same version, 1.63). Its driver sends no ``Runtime.enable`` to pages
  (dedicated workers get their context through ``Runtime.evaluate("globalThis")`` instead), and
  ``Page`` / ``Frame`` / ``Locator`` / ``ElementHandle`` ``evaluate`` run in an isolated world
  unless ``isolated_context=False`` is passed. With upstream Playwright attached, a page can tell:
  ``Error.prepareStackTrace`` runs on console and exception formatting, console calls slow down
  ~30x (page and workers), and every evaluate runs in the page's main world, where hooked DOM APIs
  see ``UtilityScript`` stacks (docs/FINGERPRINT-AUDIT.md F1, F3). patchright still auto-attaches
  to workers (``waitForDebuggerOnStart``) and enables ``Network`` there.
* **playwright**: the upstream package, kept as a fallback. It evaluates in the main world.

Selection (:func:`select_driver`): the ``PROFILEPILOT_DRIVER`` environment variable, else the
``automation.driver`` config value, each ``patchright`` | ``playwright`` | ``auto``; ``auto`` (the
default) means patchright when importable, else Playwright. :data:`DRIVER` is the selection at
import time (environment and the default data root's config); :class:`BrowserManager` and the
Python client select again when they start, with their own store's config.

The exception classes differ between the two packages, so :data:`Error` and :data:`TimeoutError`
are *tuples* of every installed package's class: use them in ``except`` and ``isinstance`` only,
and unpack them when combining (``except (*Error, OtherError)``: ``except`` rejects nested tuples).
The type names (``Page``, ``Locator`` ...) are :data:`DRIVER`'s classes, for annotations.

**The patchright driver patches.** Two things patchright 1.63 does would give an attached page away:

* it sends ``Emulation.setFocusEmulationEnabled`` to every page it attaches to, also on the default
  context of ``connect_over_cdp(no_defaults=True)``, where upstream Playwright does not. Focus
  emulation fakes focus and visibility: every attached tab reports ``visibilityState == "visible"``
  and ``document.hasFocus() == true``, background tabs and minimized windows included, which no real
  browser does (and FIX-PLAN rules out faked focus);
* like upstream, it sends every evaluate (``Runtime.callFunctionOn``: ``evaluate``, ``title()``, aria
  snapshots, actionability checks) with ``userGesture: true``, which gives the page sticky user
  activation without any input (``navigator.userActivation.hasBeenActive``, autoplay allowed): every
  reading tool, and already the first ``browser_navigate``, would activate the page
  (docs/FINGERPRINT-AUDIT.md F4). Patched, only real CDP ``Input`` clicks and key presses activate a
  page, as for a person, and ``browser_evaluate`` runs without a user gesture (the popup blocker stops
  its ``window.open``).

The patchright drivers started through :func:`async_playwright` / :func:`sync_playwright` therefore
run with ``patchright_preload.js`` (``node --require`` through ``NODE_OPTIONS``), which applies the
text patches of ``patchright_patches.json`` to the driver bundle in memory. Nothing on disk changes,
and other patchright users in the same process are unaffected: both patches only change the
default context of ``no_defaults`` connections. :func:`check_patchright_patches` verifies before a
driver starts that every patch matches the installed bundle exactly once; a mismatch (another
patchright version) is an error, never a silently unpatched driver. The Playwright fallback has
neither patch: it keeps its user gestures (and has no focus emulation to remove).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from ..errors import ProfilePilotError

if TYPE_CHECKING:
    from ..models import AppConfig

log = logging.getLogger("profilepilot.automation")

DriverName = Literal["patchright", "playwright"]
DRIVERS: tuple[DriverName, ...] = ("patchright", "playwright")
ENV_DRIVER = "PROFILEPILOT_DRIVER"

World = Literal["isolated", "main"]


def installed(name: str) -> bool:
    """Is the driver package ``name`` importable?"""
    try:
        return importlib.util.find_spec(f"{name}.async_api") is not None
    except (ImportError, ValueError):
        return False


def _choice(value: str | None, source: str) -> str | None:
    """A configured driver name (``auto`` / empty = no choice); refuses unknown names."""
    name = (value or "").strip().lower()
    if not name or name == "auto":
        return None
    if name not in DRIVERS:
        raise ProfilePilotError(f"Unknown automation driver {value!r} in {source}; use 'patchright', "
                                "'playwright' or 'auto'.")
    return name


def select_driver(config: "AppConfig | None" = None) -> DriverName:
    """The driver to use: ``PROFILEPILOT_DRIVER``, else ``config.automation.driver``, else patchright
    when it is installed (else Playwright). A driver that is chosen explicitly but not installed is
    an error, not a silent fallback."""
    chosen = _choice(os.environ.get(ENV_DRIVER), ENV_DRIVER)
    if chosen is None and config is not None:
        chosen = _choice(config.automation.driver, "the config (automation.driver)")
    if chosen is None:
        return "patchright" if installed("patchright") else "playwright"
    if not installed(chosen):
        raise ProfilePilotError(f"The automation driver '{chosen}' is not installed (pip install {chosen}).")
    return chosen  # type: ignore[return-value]


def _default_driver() -> DriverName:
    """:func:`select_driver` with the default data root's config, read without side effects."""
    try:
        from ..models import AppConfig
        from ..paths import data_root

        path = data_root() / "config.json"
        raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        return select_driver(AppConfig.model_validate(raw or {}))
    except Exception as exc:  # an unreadable config or a bad env value must not break imports
        log.warning("automation driver selection failed (%s); using the default", exc)
        return "patchright" if installed("patchright") else "playwright"


def _api(name: str, sync: bool = False) -> ModuleType:
    return importlib.import_module(f"{name}.{'sync_api' if sync else 'async_api'}")


DRIVER: DriverName = _default_driver()
"""The driver selected when this module was imported."""

_async = _api(DRIVER)
Browser = _async.Browser
BrowserContext = _async.BrowserContext
CDPSession = _async.CDPSession
Dialog = _async.Dialog
ElementHandle = _async.ElementHandle
Frame = _async.Frame
Locator = _async.Locator
Page = _async.Page
Playwright = _async.Playwright

Error: tuple[type[Exception], ...] = tuple(_api(name).Error for name in DRIVERS if installed(name))
"""The ``Error`` classes of every installed driver (``except Error`` catches either package's)."""
TimeoutError: tuple[type[Exception], ...] = tuple(  # noqa: A001 - the driver's name for it
    _api(name).TimeoutError for name in DRIVERS if installed(name))
"""The ``TimeoutError`` classes of every installed driver (subclasses of :data:`Error`)."""


def async_playwright(driver: str | None = None) -> Any:
    """``async_playwright()`` of ``driver`` (default: :func:`select_driver` without a store
    config, i.e. the environment, then the default data root's config at import time). A
    patchright driver gets the driver patch (see the module docstring)."""
    name = driver or _env_or_default()
    if name == "patchright":
        _install_patchright_preload()
    return _api(name).async_playwright()


def sync_playwright(driver: str | None = None) -> Any:
    """``sync_playwright()`` of ``driver`` (see :func:`async_playwright`)."""
    name = driver or _env_or_default()
    if name == "patchright":
        _install_patchright_preload()
    return _api(name, sync=True).sync_playwright()


def _env_or_default() -> DriverName:
    return select_driver(None) if os.environ.get(ENV_DRIVER, "").strip() else DRIVER


# ---------------------------------------------------------------------- the patchright driver patch

PRELOAD = Path(__file__).with_name("patchright_preload.js")
PATCHES_FILE = Path(__file__).with_name("patchright_patches.json")
_preload_installed = False


def patchright_patches() -> list[dict[str, str]]:
    """The text patches ``patchright_preload.js`` applies (``file``, ``name``, ``find``, ``replace``)."""
    return json.loads(PATCHES_FILE.read_text(encoding="utf-8"))


def patchright_driver_lib() -> Path:
    """``patchright/driver/package/lib`` of the installed patchright."""
    spec = importlib.util.find_spec("patchright")
    if spec is None or not spec.origin:
        raise ProfilePilotError("patchright is not installed.")
    return Path(spec.origin).parent / "driver" / "package" / "lib"


def check_patchright_patches() -> None:
    """Raise unless the text of every patch occurs exactly once in the installed driver bundle."""
    lib = patchright_driver_lib()
    for patch in patchright_patches():
        try:
            text = (lib / patch["file"]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfilePilotError(f"Cannot read patchright's driver bundle ({exc}).") from None
        if text.count(patch["find"]) != 1:
            raise ProfilePilotError(
                f"ProfilePilot's patchright driver patch '{patch['name']}' does not match the installed patchright "
                f"(ProfilePilot needs patchright 1.63). Install patchright==1.63.* or set {ENV_DRIVER}=playwright."
            )


def _install_patchright_preload() -> None:
    """Make patchright start its driver processes with ``patchright_preload.js`` (idempotent)."""
    global _preload_installed
    if _preload_installed:
        return
    check_patchright_patches()
    from patchright._impl import _transport

    original = _transport.get_driver_env
    option = f'--require "{PRELOAD.as_posix()}"'

    def get_driver_env() -> dict:
        env = original()
        if option not in env.get("NODE_OPTIONS", ""):
            env["NODE_OPTIONS"] = f"{env.get('NODE_OPTIONS', '')} {option}".strip()
        return env

    _transport.get_driver_env = get_driver_env
    _preload_installed = True


def driver_of(obj: Any) -> DriverName:
    """Which package an API object (``Page``, ``Frame``, ``Locator`` ...) comes from."""
    return "patchright" if type(obj).__module__.split(".", 1)[0] == "patchright" else "playwright"


def world_kwargs(target: Any, world: World) -> dict[str, Any]:
    """Keyword arguments that make ``target.evaluate`` / ``evaluate_all`` run in ``world``.

    patchright evaluates in an isolated world unless told otherwise; ``main`` adds
    ``isolated_context=False``. Playwright has no such option and always uses the main world, so
    there both worlds give ``{}``."""
    if world not in ("isolated", "main"):
        raise ValueError(f"world must be 'isolated' or 'main', not {world!r}")
    if driver_of(target) == "patchright":
        return {"isolated_context": world == "isolated"}
    return {}


__all__ = [
    "DRIVER", "DRIVERS", "ENV_DRIVER", "Browser", "BrowserContext", "CDPSession", "Dialog", "DriverName",
    "ElementHandle", "Error", "Frame", "Locator", "Page", "Playwright", "TimeoutError", "World",
    "async_playwright", "check_patchright_patches", "driver_of", "installed", "select_driver", "sync_playwright",
    "world_kwargs",
]
