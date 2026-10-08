"""Exit-IP / geo checks through a proxy.

``check_proxy(endpoint)`` starts a temporary credential-free :class:`LocalRelay` in front of the
upstream proxy (exactly the path Chrome's traffic takes) and asks a chain of public "what is my
IP" services through it. ``check_via_relay(url)`` does the same through an existing relay, e.g.
the live relay of a running profile (``RuntimeInfo.http_proxy_url``).

The provider chain (HTTPS first, then plain HTTP) is the one ShardBrowser uses. Providers that
are over quota often answer HTTP 200 with ``{"success": false}`` / ``{"error": true}`` /
``{"status": "fail"}``; those - and answers without an IP - count as failures and the next
provider is tried.

Error texts never contain proxy credentials: the HTTP client only ever sees the local relay, and
relay/upstream error strings are scrubbed before they are reported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence
from urllib.parse import quote

import httpx

from ..models import ProxyCheck
from .relay import LocalRelay
from .url import ProxyEndpoint

if TYPE_CHECKING:
    from ..store import Store

log = logging.getLogger("profilepilot.proxy.check")

USER_AGENT = "curl/8.9.1"
PER_REQUEST_TIMEOUT = 8.0


@dataclass(frozen=True)
class GeoProvider:
    """A geo/IP lookup service. ``kind`` selects the response parser (defaults to ``name``)."""

    name: str
    url: str
    kind: str = ""

    @property
    def parser(self) -> str:
        return self.kind or self.name


DEFAULT_PROVIDERS: tuple[GeoProvider, ...] = (
    GeoProvider("ipwho.is", "https://ipwho.is/"),
    GeoProvider("ipapi.co", "https://ipapi.co/json/"),
    GeoProvider(
        "ip-api.com",
        "http://ip-api.com/json/?fields=status,message,query,country,countryCode,regionName,city,isp,timezone",
    ),
)


class ProviderError(Exception):
    """One provider failed (network error, quota error, unusable answer)."""


# ---------------------------------------------------------------------- parsing


def _s(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def parse_provider_response(kind: str, data: Any) -> ProxyCheck:
    """Turn one provider's decoded JSON into a successful :class:`ProxyCheck`.

    Raises :class:`ProviderError` for quota errors reported with HTTP 200 and for answers that
    carry no IP address.
    """
    if not isinstance(data, dict):
        raise ProviderError("unexpected response (not a JSON object)")
    if data.get("error") is True or data.get("success") is False or str(data.get("status", "")).lower() == "fail":
        reason = _s(data, "reason") or _s(data, "message") or "request refused"
        raise ProviderError(f"refused: {reason[:200]}")

    if kind == "ip-api.com":
        info = ProxyCheck(
            ok=True, ip=_s(data, "query"), country=_s(data, "country"), country_code=_s(data, "countryCode"),
            region=_s(data, "regionName"), city=_s(data, "city"), isp=_s(data, "isp"), timezone=_s(data, "timezone"),
        )
    elif kind == "ipapi.co":
        info = ProxyCheck(
            ok=True, ip=_s(data, "ip"), country=_s(data, "country_name"), country_code=_s(data, "country_code"),
            region=_s(data, "region"), city=_s(data, "city"), isp=_s(data, "org"), timezone=_s(data, "timezone"),
        )
    elif kind == "ipwho.is":
        connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
        tz = data.get("timezone")
        info = ProxyCheck(
            ok=True, ip=_s(data, "ip"), country=_s(data, "country"), country_code=_s(data, "country_code"),
            region=_s(data, "region"), city=_s(data, "city"),
            isp=_s(connection, "isp") or _s(connection, "org"),
            timezone=_s(tz, "id") if isinstance(tz, dict) else _s(data, "timezone"),
        )
    else:
        info = ProxyCheck(
            ok=True, ip=_s(data, "ip") or _s(data, "query"), country=_s(data, "country"),
            country_code=_s(data, "country_code") or _s(data, "countryCode"), city=_s(data, "city"),
        )
    if not info.ip:
        raise ProviderError("answered without an IP address")
    info.provider = kind
    return info


# ---------------------------------------------------------------------- checks


async def check_proxy(
    endpoint: ProxyEndpoint | None,
    *,
    timeout: float = 12.0,
    providers: Sequence[GeoProvider] | None = None,
) -> ProxyCheck:
    """Check ``endpoint`` (or the direct connection when None) through a temporary local relay.

    Never raises for network problems: failures are reported as ``ProxyCheck(ok=False, error=...)``.
    """
    if endpoint is None:
        return await _run_chain(None, timeout=timeout, providers=providers)
    relay = LocalRelay(endpoint, connect_timeout=min(timeout, PER_REQUEST_TIMEOUT))
    try:
        await relay.start()
    except OSError as exc:
        return ProxyCheck(ok=False, error=f"could not start the local relay: {exc}")
    try:
        result = await _run_chain(relay.http_url, timeout=timeout, providers=providers,
                                  scrub=lambda text: scrub_secrets(text, endpoint))
        if not result.ok and relay.stats.connections_failed and relay.stats.last_error:
            upstream = scrub_secrets(relay.stats.last_error, endpoint)
            result.error = f"upstream proxy {endpoint.redacted()} failed: {upstream}. Details: {result.error}"
        return result
    finally:
        await relay.stop()


async def check_via_relay(
    http_proxy_url: str | None,
    *,
    timeout: float = 12.0,
    providers: Sequence[GeoProvider] | None = None,
) -> ProxyCheck:
    """Check through an existing credential-free HTTP proxy URL (e.g. a running profile's relay,
    ``RuntimeInfo.http_proxy_url``). ``None`` checks the direct connection."""
    return await _run_chain(http_proxy_url, timeout=timeout, providers=providers)


async def check_saved_proxy(store: "Store", ref: str, *, timeout: float = 12.0, save: bool = True,
                            providers: Sequence[GeoProvider] | None = None) -> ProxyCheck:
    """Check a proxy saved in ``store`` and (by default) record the result on its record."""
    record = store.get_proxy(ref)
    endpoint = store.proxy_endpoint(record.id)
    result = await check_proxy(endpoint, timeout=timeout, providers=providers)
    if save:
        store.set_proxy_check(record.id, result)
    return result


async def _run_chain(
    proxy_url: str | None,
    *,
    timeout: float,
    providers: Sequence[GeoProvider] | None,
    scrub: Callable[[str], str] = lambda text: text,
) -> ProxyCheck:
    chain = tuple(providers or DEFAULT_PROVIDERS)
    deadline = time.monotonic() + max(0.5, timeout)
    failures: list[str] = []
    async with httpx.AsyncClient(
        proxy=proxy_url,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        for provider in chain:
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                failures.append(f"{provider.name}: skipped (overall timeout of {timeout:g}s reached)")
                continue
            per_request = min(remaining, PER_REQUEST_TIMEOUT)
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(_query(client, provider, per_request), per_request + 0.5)
            except ProviderError as exc:
                failures.append(f"{provider.name}: {scrub(str(exc))}")
                continue
            except asyncio.TimeoutError:
                failures.append(f"{provider.name}: timed out after {per_request:.0f}s")
                continue
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            result.provider = provider.name
            if failures:
                log.info("geo check: %s answered after failures: %s", provider.name, "; ".join(failures))
            return result
    return ProxyCheck(ok=False, error="every IP-check service failed - " + "; ".join(failures))


async def _query(client: httpx.AsyncClient, provider: GeoProvider, timeout: float) -> ProxyCheck:
    try:
        response = await client.get(provider.url, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError(f"timed out after {timeout:.0f}s") from None
    except httpx.ProxyError as exc:
        raise ProviderError(f"proxy error ({_exc_text(exc)})") from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"{type(exc).__name__} ({_exc_text(exc)})") from None
    if response.status_code != 200:
        raise ProviderError(f"HTTP {response.status_code}")
    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError("answer was not JSON") from None
    return parse_provider_response(provider.parser, data)


def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:200] if text else type(exc).__name__


def scrub_secrets(text: str, endpoint: ProxyEndpoint | None) -> str:
    """Remove the proxy's username/password from ``text`` (defence in depth for error messages)."""
    if not text or endpoint is None:
        return text
    for secret in (endpoint.password, endpoint.username):
        if secret and len(secret) >= 2:
            for variant in {secret, quote(secret, safe="")}:
                text = text.replace(variant, "***")
    return text
