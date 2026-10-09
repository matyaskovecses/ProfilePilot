"""``cookies_*`` and ``http_fetch`` MCP tools.

Cookie *values* are secrets: tool output only shows names, scopes and value lengths, and exports
write values to files (never into the chat). ``http_fetch`` sends requests through the profile's
credential-free local relay (same exit IP as its browser) with the cookies the browser itself
would send for each URL (asked from Chrome per request, so redirects get the right cookies), and
writes ``Set-Cookie`` responses back into the live browser.

Cookie files are confined to the exports folders of the data root (remote mode: writes only to
the profile's own exports folder) unless a local server runs with ``--files-anywhere``; the store's
own files are never written. In remote (HTTP) mode every request / redirect hop goes through the
URL policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..automation import cookies as cookie_utils
from ..automation.manager import ProfileSession, is_shardx_ref
from ..errors import PolicyError, ProfilePilotError
from ..safety import normalize_url
from .app import (
    DEFAULT_MAX_CHARS,
    AppState,
    MaxCharsArg,
    NoneOK,
    OffsetArg,
    ProfileArg,
    add_tool,
    dumps,
    get_state,
    is_blank,
    paginate_text,
    run_sync,
    to_tool_error,
)
from .tools_browser import check_url, relay_hint

log = logging.getLogger("profilepilot.server")

MAX_BODY_BYTES = 5 * 1024 * 1024
COOKIE_FILE_SUFFIXES = (".json", ".txt", ".cookies")
MAX_REDIRECTS = 10
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml", "application/javascript",
               "application/ld+json", "application/rss", "application/atom", "+json", "+xml")


# ---------------------------------------------------------------------- paths


def export_dir(state: AppState, session: ProfileSession) -> Path:
    if session.profile is not None:
        base = state.store.profile_dir(session.profile.id) / "exports"
    else:
        base = state.store.root / "exports" / re.sub(r"[^A-Za-z0-9_.-]+", "_", session.key)
    base.mkdir(parents=True, exist_ok=True)
    return base


def exports_roots(state: AppState) -> list[Path]:
    """Every exports folder of the data root: ``exports/`` and ``profiles/<id>/exports/``."""
    root = Path(state.store.root).resolve()
    roots = [root / "exports"]
    profiles = root / "profiles"
    if profiles.is_dir():
        roots += [d / "exports" for d in profiles.iterdir() if d.is_dir()]
    return roots


def _inside(path: Path, folders: list[Path]) -> bool:
    return any(path == f or f in path.parents for f in folders)


def resolve_user_path(state: AppState, base: Path, path: str | None, default_name: str, *,
                      write: bool = False) -> Path:
    """A user/model supplied cookie-file path. Relative paths are relative to ``base`` (the
    profile's exports folder).

    * remote mode: writes must stay in ``base`` (the session's own exports folder) and reads in an
      exports folder of the data root - a remote client must not touch arbitrary files;
    * local mode: the same exports folders, unless the server runs with ``--files-anywhere``;
    * every mode: a write never targets the store's own files (config, proxies, secrets, profile
      and runtime records), only exports folders inside the data root.
    """
    raw = (path or "").strip()
    candidate = Path(raw).expanduser() if raw else base / default_name
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    root = Path(state.store.root).resolve()
    if state.remote:
        allowed = [base.resolve()] if write else [r.resolve() for r in exports_roots(state)]
        if not _inside(candidate, allowed):
            raise PolicyError(
                "In remote mode cookie files must be in a profile's exports folder; give a file name only "
                f"(it is saved in {base})."
            )
    elif not state.files_anywhere:
        if not _inside(candidate, [r.resolve() for r in exports_roots(state)]):
            raise PolicyError(
                f"Cookie files must be in a profile's exports folder ({base}); give a file name only, or move "
                "the file there. (The user can start the server with --files-anywhere to allow other folders.)"
            )
    if write and (candidate == root or root in candidate.parents) and not _inside(
        candidate, [r.resolve() for r in exports_roots(state)]
    ):
        raise PolicyError(f"Refusing to write {candidate}: ProfilePilot's own data files are off-limits. "
                          "Use a file name in the profile's exports folder.")
    return candidate


def _is_cookie_file(path: Path) -> bool:
    """True if ``path`` already holds a cookie export (JSON list / {cookies: [...]}, or cookies.txt)."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    if cookie_utils.detect_format(text) == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return False
        items = data.get("cookies") if isinstance(data, dict) else data
        return isinstance(items, list) and all(isinstance(c, dict) and "name" in c for c in items)
    try:
        cookies = cookie_utils.parse_netscape(text)
    except cookie_utils.CookieFormatError:
        return False
    return bool(cookies) or text.lstrip().startswith(cookie_utils.NETSCAPE_HEADER)


def _check_cookie_suffix(path: Path) -> None:
    if path.suffix.lower() not in COOKIE_FILE_SUFFIXES:
        raise PolicyError(f"Cookie files must end in {', '.join(COOKIE_FILE_SUFFIXES)} (got {path.name!r}).")


# ---------------------------------------------------------------------- cookies


async def cookies_get(
    ctx: Context,
    profile: ProfileArg,
    url: Annotated[str, NoneOK, Field(description="Only cookies the browser would send to this URL.")] = None,
    domain: Annotated[str, NoneOK, Field(description="Only cookies of this domain (and its subdomains).")] = None,
    names_only: Annotated[bool, Field(description="Only domain + name (shortest output).")] = False,
    max_chars: MaxCharsArg = DEFAULT_MAX_CHARS,
    offset: OffsetArg = 0,
) -> str:
    """Show which cookies a profile has (names, domains, expiry, flags). Values are never shown; use
    cookies_export to save them to a file."""
    state = get_state(ctx)
    target_url = normalize_url(str(url)) if not is_blank(url) else None  # "example.com" -> https://...
    if target_url is not None and not target_url.lower().startswith(("http://", "https://")):
        raise ProfilePilotError("url must be an http(s) URL such as https://example.com/; use domain= to filter "
                                "by domain.")
    session = await state.browsers.session(profile)
    raw = await (session.context.cookies(target_url) if target_url else session.context.cookies())
    selected = cookie_utils.filter_cookies(raw, domain=domain.strip() if not is_blank(domain) else None)
    if not selected:
        return f"[{session.label}] No cookies" + (f" for {url}" if url else "") + (f" on {domain}" if domain else "") + "."
    summary = cookie_utils.cookie_summary(selected, names_only=names_only)
    domains = len({c["domain"] for c in summary})
    head = f"[{session.label}] {len(summary)} cookie(s) on {domains} domain(s) (values hidden):\n"
    return head + paginate_text(dumps(summary), offset, max_chars)


async def cookies_set(
    ctx: Context,
    profile: ProfileArg,
    cookies: Annotated[list[dict[str, Any]] | str, Field(
        description="Cookies to add or replace: a list of {name, value, domain, path?, expires?, secure?, "
                    "httpOnly?, sameSite?} (or 'url' instead of domain), or the text of a JSON / Netscape cookie file.")],
) -> str:
    """Add or replace cookies in a profile's live browser."""
    state = get_state(ctx)
    session = await state.browsers.session(profile)
    if isinstance(cookies, str):
        portable = cookie_utils.parse_cookies_text(cookies)
    else:
        portable = cookie_utils.parse_json_cookies(cookies)
    params = cookie_utils.to_playwright_list(portable)
    if not params:
        raise ProfilePilotError("No cookies given.")
    await session.context.add_cookies(params)  # type: ignore[arg-type]
    names = sorted({f"{c['name']}@{c['domain']}" for c in params})
    shown = ", ".join(names[:20]) + (f" and {len(names) - 20} more" if len(names) > 20 else "")
    return f"[{session.label}] Set {len(params)} cookie(s): {shown}."


async def cookies_clear(
    ctx: Context,
    profile: ProfileArg,
    domain: Annotated[str, NoneOK, Field(description="Only this domain and its subdomains (default: ALL "
                                                    "cookies).")] = None,
    name: Annotated[str, NoneOK, Field(description="Only cookies with this name.")] = None,
) -> str:
    """Delete cookies (all, or one domain's). This logs the profile out of those sites: confirm with
    the user first."""
    state = get_state(ctx)
    session = await state.browsers.session(profile)
    context = session.context
    if is_blank(domain) and is_blank(name):
        count = len(await context.cookies())
        await context.clear_cookies()
        return f"[{session.label}] Deleted all {count} cookie(s)."
    current = await context.cookies()
    targets = cookie_utils.filter_cookies(current, domain=domain.strip() if not is_blank(domain) else None)
    if not is_blank(name):
        targets = [c for c in targets if c.get("name") == str(name).strip()]
    for c in targets:
        await context.clear_cookies(name=c["name"], domain=c["domain"], path=c.get("path") or "/")
    scope = " ".join(x for x in (f"named {name!r}" if name else "", f"on {domain}" if domain else "") if x)
    return f"[{session.label}] Deleted {len(targets)} cookie(s) {scope}."


async def cookies_export(
    ctx: Context,
    profile: ProfileArg,
    path: Annotated[str, NoneOK, Field(description="Target file (default: the profile's exports folder). "
                                                  ".txt = Netscape cookies.txt, otherwise JSON.")] = None,
    format: Annotated[Literal["json", "netscape"] | None, Field(description="File format (default from the "
                                                                             "file extension).")] = None,
    domain: Annotated[str, NoneOK, Field(description="Only cookies of this domain.")] = None,
    overwrite: Annotated[bool, Field(description="Replace an existing cookie export of the same name "
                                                 "(other files are never replaced).")] = False,
) -> str:
    """Save a profile's cookies (with values) to a JSON or Netscape cookies.txt file in the profile's
    exports folder. The values go to the file only, never into the chat. Existing files are only
    replaced with overwrite=true, and only if they are cookie exports."""
    state = get_state(ctx)
    session = await state.browsers.session(profile)
    raw = await session.context.cookies()
    selected = cookie_utils.filter_cookies(raw, domain=domain.strip() if not is_blank(domain) else None)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ext = "txt" if format == "netscape" else "json"
    base = export_dir(state, session)
    target = resolve_user_path(state, base, path, f"cookies-{stamp}.{ext}", write=True)
    _check_cookie_suffix(target)
    if target.is_dir():
        raise PolicyError(f"{target} is a folder; give a file name.")
    if target.exists():
        if not overwrite:
            raise ProfilePilotError(f"{target} already exists. Choose a new file name, or pass overwrite=true to "
                                    "replace an earlier cookie export.")
        if not await run_sync(_is_cookie_file, target):
            raise PolicyError(f"Refusing to overwrite {target}: it is not a cookie export. Choose a new file name.")
    if not target.parent.is_dir():
        if not _inside(target, [r.resolve() for r in exports_roots(state)]):
            raise ProfilePilotError(f"The folder {target.parent} does not exist.")
        target.parent.mkdir(parents=True, exist_ok=True)
    written = await run_sync(partial(cookie_utils.export_cookies, selected, target, format, create_parents=False))
    return f"[{session.label}] Exported {len(selected)} cookie(s) to {written}. The file contains secrets: keep it private."


async def cookies_import(
    ctx: Context,
    profile: ProfileArg,
    path: Annotated[str, Field(description="JSON (list, {cookies:[...]}, Cookie-Editor export) or Netscape "
                                           "cookies.txt file.")],
) -> str:
    """Load cookies from a file in an exports folder into a profile's live browser (e.g. to move a
    login session from another profile's cookies_export)."""
    state = get_state(ctx)
    session = await state.browsers.session(profile)
    source = resolve_user_path(state, export_dir(state, session), path, "cookies.json")
    _check_cookie_suffix(source)
    if not source.is_file():
        raise ProfilePilotError(f"Cookie file not found: {source}")
    params = await run_sync(cookie_utils.load_cookie_file, source)
    if not params:
        return f"[{session.label}] {source} contains no cookies."
    await session.context.add_cookies(params)  # type: ignore[arg-type]
    domains = sorted({str(c.get("domain", "")).lstrip(".") for c in params})
    shown = ", ".join(domains[:15]) + (f" and {len(domains) - 15} more" if len(domains) > 15 else "")
    return f"[{session.label}] Imported {len(params)} cookie(s) for {shown}."


# ---------------------------------------------------------------------- http_fetch helpers


async def browser_user_agent(state: AppState, session: ProfileSession) -> str | None:
    """The browser's genuine user agent (cached per session)."""
    cached = state.user_agents.get(session.key)
    if cached:
        return cached
    try:
        cdp = await session.browser.new_browser_cdp_session()
        try:
            info = await cdp.send("Browser.getVersion")
        finally:
            await cdp.detach()
        ua = str(info.get("userAgent") or "") or None
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("could not read the user agent: %s", exc)
        ua = None
    if ua:
        state.user_agents[session.key] = ua
    return ua


async def browser_accept_language(state: AppState, session: ProfileSession) -> str | None:
    """``Accept-Language`` as the browser sends it, built from ``navigator.languages`` (cached)."""
    cached = state.accept_languages.get(session.key)
    if cached:
        return cached
    pages = [p for p in session.context.pages if not p.is_closed()]
    if not pages:
        return None
    try:
        langs = await asyncio.wait_for(pages[0].evaluate("navigator.languages"), 2.0)
    except Exception as exc:  # busy page / navigation in flight: try again next time
        log.debug("could not read navigator.languages: %s", exc)
        return None
    langs = [str(x) for x in (langs or []) if isinstance(x, str) and x and re.fullmatch(r"[A-Za-z0-9-]+", x)]
    if not langs:
        return None
    # Chrome's format: the first language without a weight, then q=0.9, 0.8, ... (never below 0.1)
    header = ",".join([langs[0]] + [f"{lang};q={max(0.1, 1 - 0.1 * i):.1f}" for i, lang in enumerate(langs[1:], 1)])
    state.accept_languages[session.key] = header
    return header


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    # Chrome orders longer paths first; Playwright already returns the browser's order otherwise.
    ordered = sorted(cookies, key=lambda c: -len(str(c.get("path") or "/")))
    return "; ".join(f"{c['name']}={c['value']}" for c in ordered)


def _parse_set_cookie(header: str) -> tuple[str, dict[str, str]]:
    first, *attrs = header.split(";")
    name = first.split("=", 1)[0].strip()
    parsed: dict[str, str] = {}
    for attr in attrs:
        key, _, value = attr.partition("=")
        parsed[key.strip().lower()] = value.strip()
    return name, parsed


def _is_deletion(attrs: dict[str, str]) -> bool:
    if "max-age" in attrs:
        try:
            return int(attrs["max-age"]) <= 0
        except ValueError:
            return False
    if "expires" in attrs:
        try:
            return parsedate_to_datetime(attrs["expires"]).timestamp() <= time.time()
        except (TypeError, ValueError, IndexError):
            return False
    return False


async def write_back_cookies(session: ProfileSession, response: httpx.Response) -> int:
    """Apply a response's Set-Cookie headers to the live browser context. Returns the count."""
    headers = response.headers.get_list("set-cookie")
    if not headers:
        return 0
    request = response.request
    host = (request.url.host or "").lower()
    context = session.context
    changed = 0
    existing: list[dict[str, Any]] | None = None
    for header in headers:  # deletions (Max-Age<=0 / past Expires) are not kept by cookiejar
        name, attrs = _parse_set_cookie(header)
        if not (name and _is_deletion(attrs)):
            continue
        spec = attrs.get("domain", "").strip().lstrip(".").lower()
        if spec:
            if host != spec and not host.endswith("." + spec):
                continue  # like a browser: a site may only delete its own (or a parent domain's) cookies
            if name.startswith("__Host-"):
                continue  # __Host- cookies never carry a Domain attribute
            candidates = {spec, "." + spec}
        else:
            candidates = {host}  # the host-only cookie of exactly this host
        if request.url.scheme == "http":
            # Chrome never lets an insecure response touch a Secure cookie ("leave secure cookies alone")
            if existing is None:
                existing = await context.cookies()
            if any(c.get("name") == name and c.get("domain") in candidates and c.get("secure") for c in existing):
                continue
        for candidate in candidates:
            await context.clear_cookies(name=name, domain=candidate, path=attrs.get("path") or None)
        changed += 1
    jar = httpx.Cookies()
    jar.extract_cookies(response)  # RFC 6265-ish parsing and domain checks by http.cookiejar
    params = []
    for c in jar.jar:
        domain = c.domain if c.domain_specified else host  # host-only cookie
        rest = {str(k).lower(): v for k, v in getattr(c, "_rest", {}).items()}
        item: dict[str, Any] = {
            "name": c.name, "value": c.value or "", "domain": domain, "path": c.path or "/",
            "secure": bool(c.secure), "httpOnly": "httponly" in rest,
        }
        same_site = rest.get("samesite")
        if same_site:
            item["sameSite"] = same_site
        if c.expires:
            item["expires"] = int(c.expires)
        params.append(item)
    if params:
        await context.add_cookies(cookie_utils.to_playwright_list(params))  # type: ignore[arg-type]
        changed += len(params)
    return changed


def is_textual(content_type: str) -> bool:
    ct = content_type.lower()
    return not ct or any(t in ct for t in _TEXT_TYPES)


_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_NON_CONTENT = ("script", "style", "noscript", "template", "head")
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "table", "td", "th", "tr", "ul",
})


_XML_DECL = re.compile(r"^\ufeff?\s*<\?xml[^>]*\?>", re.IGNORECASE)


def clean_html(text: str, base_url: str = "") -> Any:
    """Parse HTML and drop what a reader would not see without running the page: scripts, styles,
    ``<head>``, ``hidden`` / ``aria-hidden`` elements and inline ``display:none`` /
    ``visibility:hidden`` (prompt-injection hygiene, best effort without CSS). Links are made
    absolute against ``base_url``."""
    import lxml.html

    # lxml refuses str input that starts with an XML encoding declaration (XHTML pages)
    root = lxml.html.fromstring(_XML_DECL.sub("", text, count=1))
    for el in root.xpath("//script|//style|//noscript|//template|//head|//*[@hidden]"
                         "|//*[@aria-hidden='true']|//*[@style]"):
        if el.tag in ("html", "body"):
            continue
        hidden = (
            el.tag in _NON_CONTENT
            or el.get("hidden") is not None
            or el.get("aria-hidden") == "true"
            or bool(_HIDDEN_STYLE.search(el.get("style") or ""))
        )
        if hidden and el.getparent() is not None:
            el.drop_tree()
    if base_url:
        try:
            root.make_links_absolute(base_url, resolve_base_href=True)
        except ValueError:
            pass
    return root


def render_body(body: bytes, content_type: str, encoding: str | None, fmt: str, base_url: str = "") -> str:
    """Response body as model-readable text in the requested format (``raw`` = unchanged)."""
    ct = (content_type or "").lower()
    if not is_textual(ct):
        return f"(binary response: {content_type or 'unknown type'}, {len(body)} bytes; not shown)"
    text = decode_body(body, ct, encoding)
    if "json" in ct and fmt != "raw":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            return text
    if "html" in ct and fmt != "raw" and text.strip():
        import lxml.html

        try:
            root = clean_html(text, base_url)
        except Exception:  # unparsable markup: never hand over the raw page (scripts, hidden text)
            log.debug("could not parse an HTML body", exc_info=True)
            return "(could not parse this HTML; use format='raw' to see the body)"
        if fmt == "html":
            return lxml.html.tostring(root, encoding="unicode")
        if fmt == "markdown":
            from ..automation.content import html_to_markdown

            return html_to_markdown(lxml.html.tostring(root, encoding="unicode"))
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag.lower() in _BLOCK_TAGS:
                el.text = "\n" + (el.text or "")  # keep block boundaries as line breaks
                el.tail = "\n" + (el.tail or "")
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in root.text_content().splitlines()]
        return "\n".join(ln for ln in lines if ln)
    return text


_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_CHARSET_ALIASES = {"utf8mb4": "utf-8", "utf8mb3": "utf-8", "utf8": "utf-8"}


def _charset(content_type: str) -> str | None:
    match = _CHARSET_RE.search(content_type or "")
    return match.group(1) if match else None


def decode_body(body: bytes, content_type: str, encoding: str | None) -> str:
    """Decode a response body like a browser: a UTF-8 BOM, the header charset, then an HTML/XML
    ``<meta charset>`` prescan; unknown charset names fall through to UTF-8."""
    ct = (content_type or "").lower()
    candidates: list[str] = []
    if body.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if encoding:
        candidates.append(encoding)
    if "html" in ct or "xml" in ct:
        match = _META_CHARSET.search(body[:4096])
        if match:
            candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        name = _CHARSET_ALIASES.get(name.strip().lower(), name.strip())
        try:
            return body.decode(name, errors="replace")
        except LookupError:  # unknown / bogus charset name: try the next candidate, then UTF-8
            continue
    return body.decode("utf-8", errors="replace")


_PDF_HINT = "Install profilepilot[pdf] (pypdf) to read PDF text."


def pdf_text(data: bytes) -> str | None:
    """Text of a PDF via the optional ``pypdf`` package (None when it is not installed)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # malformed / encrypted PDF
        log.debug("PDF text extraction failed: %s", exc)
        return ""
    return "\n\n".join(f"[page {i}]\n{text}" for i, text in enumerate(pages, 1) if text)


def download_name(url: str, content_type: str, disposition: str | None) -> str:
    """A safe file name for a downloaded body (Content-Disposition, else the URL's last segment)."""
    name = ""
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename() or ""
    if not name:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")[:100]
    if not name:
        name = "download"
    if name.split(".", 1)[0].upper() in _RESERVED_NAMES:
        name = "_" + name  # CON, NUL, COM1, ... are devices on Windows
    if "." not in name:
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
        name += ext
    return name


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def save_download(folder: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to ``folder/name`` without replacing an existing file (``name-1.ext``, ...)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(10_000):
        target = folder / (name if n == 0 else (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"))
        try:
            with open(target, "xb") as fh:  # exclusive create: concurrent fetches never clobber each other
                fh.write(data)
            return target
        except FileExistsError:
            continue
    raise ProfilePilotError(f"Too many files named {name!r} in {folder}.")


# ---------------------------------------------------------------------- http_fetch


async def http_fetch(
    ctx: Context,
    profile: ProfileArg,
    url: Annotated[str, Field(description="URL to request (https:// is added to bare domains).")],
    method: Annotated[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                      Field(description="HTTP method.")] = "GET",
    headers: Annotated[dict[str, str] | None, Field(description="Extra request headers.")] = None,
    body: Annotated[str, NoneOK, Field(description="Request body (e.g. JSON text) for POST/PUT/PATCH.")] = None,
    format: Annotated[Literal["markdown", "text", "html", "raw"], Field(
        description="markdown/text/html drop scripts and hidden elements of HTML and pretty-print JSON; "
                    "raw returns the body unchanged.")] = "markdown",
    engine: Annotated[Literal["auto", "httpx", "scrapling"], Field(
        description="auto/httpx = plain HTTP client; scrapling = curl_cffi with a browser-like TLS fingerprint.")] = "auto",
    follow_redirects: Annotated[bool, Field(description="Follow redirects.")] = True,
    timeout_s: Annotated[float, Field(description="Timeout in seconds.", ge=1, le=120)] = 30.0,
    max_chars: MaxCharsArg = DEFAULT_MAX_CHARS,
    offset: OffsetArg = 0,
) -> str:
    """Fast HTTP request through the profile's proxy with the profile's cookies (Set-Cookie is
    written back to the browser). Good for APIs, robots.txt and static pages; use browser_* for
    pages that need JavaScript. Ask the user before requests that change data (POST/PUT/DELETE)."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        raise ProfilePilotError("http_fetch is not available for ShardX profiles (ShardX manages their proxy); "
                                "use the browser tools instead.")
    destination = await check_url(state, url)
    if not destination.lower().startswith(("http://", "https://")):
        raise ProfilePilotError("http_fetch needs an http:// or https:// URL.")
    session = await state.browsers.session(profile)
    info = session.runtime
    proxy = info.http_proxy_url if info is not None and info.relay_port else None
    payload = body.encode("utf-8") if body is not None else None

    try:
        if engine == "scrapling":
            result = await _fetch_scrapling(state, session, destination, method, headers, payload, follow_redirects,
                                            timeout_s)
        else:
            result = await _fetch_httpx(state, session, destination, method, headers, payload, follow_redirects,
                                        timeout_s, proxy)
    except (httpx.ProxyError, httpx.TimeoutException) as exc:
        hint = await relay_hint(state, session) if proxy else ""
        if hint:  # the relay knows why the proxy failed: say so instead of a bare 502 / timeout
            raise ToolError(f"{to_tool_error(exc, 'http_fetch')} {hint}") from None
        raise
    status, reason, final_url, content_type, raw, truncated, cookies_written, disposition = result
    if state.policy.restricts_private and final_url != destination:
        await state.policy.acheck(final_url)

    route = f"via the profile's proxy ({info.upstream})" if proxy and info and info.upstream else (
        "via the profile's relay" if proxy else "direct (the profile has no proxy)")
    lines = [
        f"[{session.label}] HTTP {status}{' ' + reason if reason else ''} — {final_url}",
        f"content-type: {content_type or 'unknown'}; {len(raw)} bytes{' (truncated at 5 MB)' if truncated else ''}; "
        f"{route}; engine {engine if engine != 'auto' else 'httpx'}",
    ]
    if cookies_written:
        lines.append(f"Set-Cookie: {cookies_written} cookie change(s) saved to the profile's browser.")
    if method == "HEAD" or not raw:
        return "\n".join(lines + ["(empty body)"])
    if not is_textual(content_type):
        return "\n".join(lines) + "\n\n" + await _binary_body(state, session, raw, content_type, final_url,
                                                               disposition, truncated, offset, max_chars)
    text = render_body(raw, content_type, _charset(content_type), format, final_url)
    return "\n".join(lines) + "\n\n" + paginate_text(text, offset, max_chars)


async def _binary_body(state: AppState, session: ProfileSession, raw: bytes, content_type: str, url: str,
                       disposition: str | None, truncated: bool, offset: int, max_chars: int) -> str:
    """Save a binary body to the profile's downloads folder; extract the text of PDFs."""
    if session.profile is not None:
        folder = await run_sync(state.store.downloads_dir, session.profile.id)
    else:
        folder = export_dir(state, session)
    name = download_name(url, content_type, disposition)
    saved = await run_sync(save_download, folder, name, raw)
    part = " (only the first 5 MB)" if truncated else ""
    note = f"(binary response: {content_type or 'unknown type'}, {len(raw)} bytes; saved to {saved}{part})"
    if "pdf" in content_type.lower() or raw.startswith(b"%PDF"):
        text = None if truncated else await run_sync(pdf_text, raw)
        if text is None:
            return note + ("\nThe PDF is incomplete, so its text cannot be read." if truncated else "\n" + _PDF_HINT)
        if not text.strip():
            return note + "\nThe PDF has no extractable text (it may be scanned images)."
        return note + "\nPDF text:\n" + paginate_text(text, offset, max_chars)
    return note


async def _fetch_httpx(state: AppState, session: ProfileSession, url: str, method: str,
                       headers: dict[str, str] | None, payload: bytes | None, follow_redirects: bool,
                       timeout_s: float, proxy: str | None) -> tuple[int, str, str, str, bytes, bool, int, str | None]:
    user_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    manual_cookie = any(k.lower() == "cookie" for k in user_headers)
    written = 0

    async def on_request(request: httpx.Request) -> None:
        if state.policy.restricts_private:
            await state.policy.acheck(str(request.url))  # every redirect hop
        if manual_cookie:
            return
        jar = await session.context.cookies([str(request.url)])
        header = _cookie_header(jar)
        if header:
            request.headers["Cookie"] = header
        elif "cookie" in request.headers:
            del request.headers["cookie"]

    async def on_response(response: httpx.Response) -> None:
        nonlocal written
        try:
            written += await write_back_cookies(session, response)
        except Exception as exc:  # never fail the request because of a cookie
            log.warning("could not write cookies back: %s", type(exc).__name__)

    default_headers: dict[str, str] = {"Accept": "*/*"}
    ua = await browser_user_agent(state, session)
    if ua:
        default_headers["User-Agent"] = ua
    languages = await browser_accept_language(state, session)
    if languages:
        default_headers["Accept-Language"] = languages
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=False, follow_redirects=follow_redirects, max_redirects=MAX_REDIRECTS,
        timeout=timeout_s, headers=default_headers,
        event_hooks={"request": [on_request], "response": [on_response]},
    ) as client:
        async with client.stream(method, url, headers=user_headers, content=payload) as response:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    truncated = True
                    break
            raw = b"".join(chunks)[:MAX_BODY_BYTES]
            return (response.status_code, response.reason_phrase, str(response.url),
                    response.headers.get("content-type", ""), raw, truncated, written,
                    response.headers.get("content-disposition"))


async def _fetch_scrapling(state: AppState, session: ProfileSession, url: str, method: str,
                           headers: dict[str, str] | None, payload: bytes | None, follow_redirects: bool,
                           timeout_s: float) -> tuple[int, str, str, str, bytes, bool, int, str | None]:
    try:
        from ..client import ProfilePilot
        from ..integrations.scrapling import fetcher_session
    except ImportError:
        raise ProfilePilotError("engine='scrapling' needs Scrapling with its fetchers "
                                "(pip install 'profilepilot[scrapling]'); use engine='httpx'.") from None
    if session.profile is None:
        raise ProfilePilotError("engine='scrapling' needs a ProfilePilot profile.")
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ProfilePilotError("engine='scrapling' supports GET, POST, PUT and DELETE; use engine='httpx'.")
    pilot = ProfilePilot(store=state.store, runtime=state.runtime)
    fetcher = await run_sync(partial(fetcher_session, session.profile.id, pilot=pilot, write_back=True,
                                     autostart=False))
    redirects: Any = ("safe" if state.policy.restricts_private else True) if follow_redirects else False
    kwargs: dict[str, Any] = {"follow_redirects": redirects, "timeout": timeout_s}
    if headers:
        kwargs["headers"] = dict(headers)
    if payload is not None and method in ("POST", "PUT"):
        kwargs["data"] = payload
    async with fetcher as client:
        response = await getattr(client, method.lower())(url, **kwargs)
    resp_headers = {str(k).lower(): str(v) for k, v in dict(response.headers or {}).items()}
    raw = bytes(response.body or b"")
    truncated = len(raw) > MAX_BODY_BYTES
    final_url = str(getattr(response, "url", "") or url)
    return (int(response.status), str(response.reason or ""), final_url, resp_headers.get("content-type", ""),
            raw[:MAX_BODY_BYTES], truncated, 0, resp_headers.get("content-disposition"))


# ---------------------------------------------------------------------- registration


def register(server: MCPServer) -> None:
    add_tool(server, cookies_get, title="Get cookies", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Reading cookies…", invoked="Cookies listed")
    add_tool(server, cookies_set, title="Set cookies", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Setting cookies…", invoked="Cookies set")
    add_tool(server, cookies_clear, title="Clear cookies", read_only=False, destructive=True, idempotent=True,
             open_world=False, invoking="Deleting cookies…", invoked="Cookies deleted")
    add_tool(server, cookies_export, title="Export cookies", read_only=False, destructive=True, idempotent=False,
             open_world=False, invoking="Exporting cookies…", invoked="Cookies exported")
    add_tool(server, cookies_import, title="Import cookies", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Importing cookies…", invoked="Cookies imported")
    add_tool(server, http_fetch, title="HTTP fetch", read_only=False, destructive=False, idempotent=False,
             open_world=True, invoking="Fetching…", invoked="Fetched")


__all__ = ["register", "write_back_cookies", "render_body", "decode_body", "resolve_user_path"]
