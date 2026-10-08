"""Persistent and runtime data models.

Everything here is JSON-serialisable with pydantic. Secrets (proxy passwords, control tokens)
are never part of the ``summary()`` / ``public()`` views that are returned to AI models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProxyScheme = Literal["http", "https", "socks4", "socks5"]
WindowMode = Literal["normal", "offscreen", "headless"]
WebRTCMode = Literal["auto", "proxy_only", "default"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


# --------------------------------------------------------------------------- proxies


class ProxyCheck(_Model):
    """Result of an exit-IP / geo check through a proxy (or direct)."""

    ok: bool
    ip: str | None = None
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    isp: str | None = None
    timezone: str | None = None
    latency_ms: int | None = None
    provider: str | None = None
    error: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


class ProxyRecord(_Model):
    """A saved upstream proxy. The password lives in the secret store, never in this record."""

    id: str
    name: str
    scheme: ProxyScheme
    host: str
    port: int
    username: str | None = None
    has_password: bool = False
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    last_check: ProxyCheck | None = None

    def redacted_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{host}:{self.port}"

    def summary(self) -> dict[str, Any]:
        """Model-safe view (no secrets)."""
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "url": self.redacted_url(),
            "scheme": self.scheme,
            "tags": self.tags,
        }
        if self.notes:
            data["notes"] = self.notes
        if self.last_check:
            c = self.last_check
            data["last_check"] = {
                k: v
                for k, v in {
                    "ok": c.ok,
                    "ip": c.ip,
                    "country": c.country_code or c.country,
                    "city": c.city,
                    "latency_ms": c.latency_ms,
                    "error": c.error,
                    "checked_at": c.checked_at.isoformat(),
                }.items()
                if v is not None
            }
        return data


# --------------------------------------------------------------------------- profiles


class LaunchOptions(_Model):
    """How a profile's browser is launched. Defaults keep Chrome as native as possible."""

    window: WindowMode = "normal"
    """normal = regular visible window (most native). offscreen = real window placed off-screen.
    headless = Chrome headless (NOT native: HeadlessChrome UA and navigator.webdriver=true)."""

    webrtc: WebRTCMode = "auto"
    """auto = proxy_only when a proxy is set, default otherwise. proxy_only = never send UDP outside
    the proxy (prevents real-IP leaks). default = Chrome's normal behaviour."""

    disable_quic: bool | None = None
    """None = auto (disabled when proxied)."""

    lang: str | None = None
    """Optional UI/Accept-Language override, e.g. "de-DE" (opt-in; default uses the OS language)."""

    timezone: str | None = None
    """Optional IANA timezone override applied over CDP (opt-in; this is a spoof, off by default)."""

    restore_session: bool = True
    """Keep tabs and session cookies across restarts (like "Continue where you left off")."""

    start_url: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class Profile(_Model):
    id: str
    name: str
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    color: str | None = None
    proxy_id: str | None = None
    browser: str = "auto"
    """auto | chrome | edge | chromium | brave | absolute path to a Chromium-family executable."""
    launch: LaunchOptions = Field(default_factory=LaunchOptions)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_started_at: datetime | None = None
    total_runtime_s: int = 0
    rev: int = 1

    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "name": self.name, "proxy_id": self.proxy_id, "tags": self.tags}
        if self.notes:
            data["notes"] = self.notes
        if self.browser != "auto":
            data["browser"] = self.browser
        if self.launch.window != "normal":
            data["window"] = self.launch.window
        if self.last_started_at:
            data["last_started_at"] = self.last_started_at.isoformat()
        return data


class TrashEntry(_Model):
    trash_id: str
    profile_id: str
    name: str
    deleted_at: datetime
    size_bytes: int = 0


# --------------------------------------------------------------------------- runtime


class RuntimeInfo(_Model):
    """State of a running profile, written by its host process to ``runtime.json``."""

    profile_id: str
    profile_name: str
    state: Literal["starting", "running", "stopping"] = "starting"
    host_pid: int
    chrome_pid: int | None = None
    chrome_create_time: float | None = None
    browser_path: str | None = None
    browser_version: str | None = None
    cdp_port: int | None = None
    cdp_http_url: str | None = None
    cdp_ws_url: str | None = None
    relay_port: int | None = None
    proxy_id: str | None = None
    upstream: str | None = None
    """Redacted upstream proxy URL, or None for a direct connection."""
    control_port: int | None = None
    control_token: str | None = None
    window: WindowMode = "normal"
    started_at: datetime = Field(default_factory=utcnow)
    error: str | None = None

    @property
    def proxy_url(self) -> str | None:
        """Credential-free SOCKS5 URL of the local relay (what Chrome uses)."""
        return f"socks5://127.0.0.1:{self.relay_port}" if self.relay_port else None

    @property
    def http_proxy_url(self) -> str | None:
        """The same relay spoken as an HTTP proxy (for curl / httpx / Scrapling Fetcher)."""
        return f"http://127.0.0.1:{self.relay_port}" if self.relay_port else None

    def public(self) -> dict[str, Any]:
        """Model-safe view (no control token)."""
        data = self.model_dump(mode="json", exclude={"control_token", "control_port", "chrome_create_time"})
        data["proxy_url"] = self.proxy_url
        data["http_proxy_url"] = self.http_proxy_url
        return {k: v for k, v in data.items() if v is not None}


# --------------------------------------------------------------------------- config


class ShardXConfig(_Model):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:40325"
    token_source: Literal["keyring", "settings"] = "keyring"
    """keyring = token pasted by the user and stored in the OS keyring.
    settings = mint short-lived tokens from ShardX's settings.json api_secret (explicit opt-in)."""


class AppConfig(_Model):
    browser_path: str | None = None
    """Override for the default browser executable (otherwise auto-detected)."""
    default_window: WindowMode = "normal"
    max_running: int = 20
    shardx: ShardXConfig = Field(default_factory=ShardXConfig)
