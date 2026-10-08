"""Chrome command-line construction.

ProfilePilot launches the user's real Chrome with as few switches as possible, so pages see a
genuine browser. Verified on Chrome 154 (Windows 11):

* A fixed, non-zero ``--remote-debugging-port`` keeps ``navigator.webdriver`` false; port 0,
  ``--remote-debugging-pipe`` and ``--enable-automation`` do not. (``--headless=new`` with a fixed
  port also reports ``webdriver == false`` on 154, but the UA says ``HeadlessChrome``.)
* An off-screen window (``--window-position=-32000,-32000``) - and equally a *normal* window that
  is fully covered by other windows - is treated as occluded: the page reports
  ``visibilityState == "hidden"``, ``requestAnimationFrame`` never fires, timers are throttled to
  ~1/s and Playwright actions hang waiting for the element to be "stable".
  ``--disable-backgrounding-occluded-windows`` keeps it ``"visible"`` with normal rAF/timer
  behaviour, so it is passed in every window mode (it is not a "bad flag": no infobar, and the
  page cannot read it - it only stops occlusion-based throttling).
* ``--webrtc-ip-handling-policy=disable_non_proxied_udp`` makes a local
  ``RTCPeerConnection`` + ``createDataChannel`` + ``createOffer`` gather no candidates at all,
  whereas without it a UDP host (mDNS) candidate appears. ``--force-webrtc-ip-handling-policy``
  has no effect on branded Chrome.
* Seeding ``session.restore_on_startup`` in ``Default/Preferences`` is ignored (tracked pref
  without a valid MAC), while ``--restore-last-session`` restores tabs *and* session cookies. A
  URL on the command line is opened *in addition* to restored tabs, so callers pass no start URL
  when a saved session exists (see ``session_exists``).
* With the back/forward cache on, a page restored by Back gets snapshot refs that Playwright 1.63
  cannot resolve ("Invalid frame in aria-ref selector") and ``go_back(wait_until=
  "domcontentloaded")`` times out; ``--disable-back-forward-cache`` (as Playwright uses for the
  browsers it launches) avoids both.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import ProfilePilotError
from ..models import LaunchOptions
from ..paths import BrowserInfo

#: Switches ProfilePilot manages itself (or that would break isolation / leak the real IP).
MANAGED_SWITCHES = frozenset({
    "user-data-dir",
    "profile-directory",
    "proxy-server",
    "proxy-pac-url",
    "proxy-auto-detect",
    "no-proxy-server",
    "winhttp-proxy-resolver",
    "headless",
    "enable-automation",
})
#: Switches that are never emitted: they make Chrome detectably non-native or unsafe.
FORBIDDEN_SWITCHES = frozenset({
    "remote-allow-origins",
    "disable-blink-features",
    "no-sandbox",
    "user-agent",
    "test-type",
})
_MANAGED_PREFIXES = ("remote-debugging-",)

OFFSCREEN_POSITION = "-32000,-32000"
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def switch_name(arg: str) -> str:
    """``--foo-bar=1`` -> ``foo-bar`` (lower-cased)."""
    return arg[2:].split("=", 1)[0].strip().lower()


def validate_extra_args(args: list[str] | tuple[str, ...]) -> list[str]:
    """Validate user-supplied extra switches; returns them unchanged or raises ProfilePilotError."""
    out: list[str] = []
    for index, raw in enumerate(args or (), 1):
        arg = str(raw).strip()
        if not arg.startswith("--") or len(arg) <= 2 or arg[2] in "-=":
            # The value is not echoed: it could contain credentials.
            raise ProfilePilotError(f"Extra Chrome argument #{index} must be a switch of the form --name[=value].")
        if any(ch in arg for ch in "\x00\r\n"):
            raise ProfilePilotError("Extra Chrome arguments must not contain control characters.")
        name = switch_name(arg)
        if name in MANAGED_SWITCHES or name.startswith(_MANAGED_PREFIXES):
            raise ProfilePilotError(f"--{name} is managed by ProfilePilot and cannot be passed as an extra argument.")
        if name in FORBIDDEN_SWITCHES:
            raise ProfilePilotError(
                f"--{name} is refused: it makes the browser detectably automated or unsafe."
            )
        out.append(arg)
    return out


def accept_languages(lang: str) -> str:
    """``de-DE`` -> ``de-DE,de``; ``fr`` -> ``fr``."""
    primary = lang.split("-", 1)[0]
    return lang if primary == lang else f"{lang},{primary}"


def _validate_url(url: str) -> str:
    url = str(url).strip()
    if not url or url[0] in "-/" or ":" not in url or any(ch in url for ch in "\x00\r\n"):
        raise ProfilePilotError("Invalid start URL: expected an absolute URL such as https://example.com/.")
    return url


def build_chrome_args(
    *,
    browser: BrowserInfo,
    user_data_dir: Path,
    cdp_port: int,
    launch: LaunchOptions,
    relay_port: int | None,
    start_urls: list[str],
    session_exists: bool = False,
) -> list[str]:
    """Return Chrome's argv (without the executable).

    ``start_urls`` are appended last; when empty, ``about:blank`` is opened - except when
    ``launch.restore_session`` is on and ``session_exists`` (the previous session is restored by
    ``--restore-last-session`` and an extra blank tab would accumulate on every restart).
    """
    if not 0 < int(cdp_port) < 65536:
        raise ProfilePilotError(f"Invalid CDP port {cdp_port}; it must be a fixed non-zero port.")
    args = [
        f"--user-data-dir={Path(user_data_dir).resolve()}",
        "--profile-directory=Default",
        f"--remote-debugging-port={int(cdp_port)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--hide-crash-restore-bubble",
        # Pages restored from the back/forward cache break Playwright's element refs ("Invalid
        # frame in aria-ref selector") and never fire load events again, so history navigations
        # wait forever. Playwright disables the cache in browsers it launches for the same reason.
        "--disable-back-forward-cache",
        # An AI-driven window is usually behind other windows; without this Chrome treats it as
        # occluded, stops rendering frames and every Playwright action hangs.
        "--disable-backgrounding-occluded-windows",
    ]
    if browser.kind == "edge":
        args.append("--edge-skip-compat-layer-relaunch")  # keeps the PID we spawned alive

    proxied = relay_port is not None
    if proxied:
        args.append(f"--proxy-server=socks5://127.0.0.1:{int(relay_port)}")
    webrtc_policy = launch.webrtc == "proxy_only" or (launch.webrtc == "auto" and proxied)
    if webrtc_policy:
        args.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    if launch.disable_quic is True or (launch.disable_quic is None and proxied):
        args.append("--disable-quic")

    if launch.lang:
        lang = launch.lang.strip()
        if not _LANG_RE.match(lang):
            raise ProfilePilotError(f"Invalid language tag {lang!r}; expected e.g. 'de-DE' or 'fr'.")
        args += [f"--lang={lang}", f"--accept-lang={accept_languages(lang)}"]

    if launch.window == "offscreen":
        args.append(f"--window-position={OFFSCREEN_POSITION}")
    elif launch.window == "headless":
        args.append("--headless=new")  # NOT native: HeadlessChrome user agent

    if launch.restore_session:
        args.append("--restore-last-session")

    args += validate_extra_args(launch.extra_args)

    urls = [_validate_url(u) for u in start_urls or () if str(u).strip()]
    if urls:
        args += urls
    elif not (launch.restore_session and session_exists):
        args.append("about:blank")
    return args
