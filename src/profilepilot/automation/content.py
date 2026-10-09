"""Page content for models: accessibility snapshots, cleaned page text, extraction, pagination.

* :func:`snapshot` - Playwright's AI accessibility snapshot (``[ref=eN]`` markers that the
  action tools resolve with ``page.locator("aria-ref=eN")``).
* :func:`read_page` - the readable content of a page as Markdown, plain text or HTML. Hidden
  content (``display:none``, ``visibility:hidden``, ``opacity:0``, zero-size or clipped boxes,
  ``aria-hidden``, off-screen positioning, transparent text, ``<template>``/``<script>``/...)
  is dropped **inside the browser, using computed styles**, before anything is serialised. This is
  prompt-injection hygiene: text a human cannot see is not handed to the model. The live DOM is
  never modified - the page is walked read-only and a detached copy is serialised as a string, so
  no page code (custom element constructors, mutation observers) runs because of it.
* :func:`extract` - CSS (with ``::text`` / ``::attr(x)``) and XPath extraction from page HTML via
  Scrapling's parser (lxml fallback).
* :func:`paginate` - stable chunking of long outputs with a ``next_offset`` cursor.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin

from ..errors import NotFoundError, ProfilePilotError

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("profilepilot.content")

ReadFormat = Literal["markdown", "text", "html"]

_REF_RE = re.compile(r"^(?:f\d+)?e\d+$")
_REF_INPUT_RE = re.compile(r"^\[?\s*(?:ref\s*[=:]\s*)?((?:f\d+)?e\d+)\s*\]?$", re.IGNORECASE)
_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_HEADER_REF_RE = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
_NAV_ERRORS = ("Execution context was destroyed", "navigation")


class RefNotFoundError(NotFoundError):
    """An element ref from an earlier snapshot no longer resolves (page changed or navigated)."""


class InvalidTargetError(ProfilePilotError, ValueError):
    """A tool was given neither a valid ref nor a selector."""


def normalize_ref(ref: str) -> str:
    """Accept ``e5``, ``[ref=e5]``, ``ref=e5`` or ``f1e3`` and return the bare ref id."""
    match = _REF_INPUT_RE.match((ref or "").strip())
    if not match:
        raise InvalidTargetError(
            f"'{ref}' is not an element ref. Refs look like 'e12' and come from the [ref=...] markers "
            "of browser_snapshot; use 'selector' for CSS selectors."
        )
    return match.group(1).lower()


def stale_ref_error(ref: str) -> RefNotFoundError:
    return RefNotFoundError(
        f"Element ref '{ref}' is not on the page any more (the page changed or navigated since that "
        "snapshot). Take a new browser_snapshot and use a ref from it."
    )


# ---------------------------------------------------------------------- snapshot


async def snapshot(page: "Page", *, depth: int | None = None, ref: str | None = None, boxes: bool = False) -> str:
    """Accessibility snapshot of ``page`` in Playwright's AI mode (``[ref=eN]`` markers).

    With ``ref`` the output is limited to that element's subtree. The subtree is cut out of a
    full-page snapshot rather than taken with ``locator.aria_snapshot()``: every aria snapshot
    replaces the page's ref table, so a locator-scoped snapshot would invalidate all refs outside
    the scope (verified with Playwright 1.63). ``depth`` then counts levels below that element.
    """
    if depth is not None and depth < 0:
        raise ProfilePilotError("depth must be >= 0.")
    if ref is None:
        return await _aria_snapshot(page, depth=depth, boxes=boxes)
    ref_id = normalize_ref(ref)
    full = await _aria_snapshot(page, depth=None, boxes=boxes)
    sub = subtree(full, ref_id, depth)
    if sub is None:
        raise stale_ref_error(ref_id)
    return sub


async def _aria_snapshot(page: "Page", *, depth: int | None, boxes: bool) -> str:
    kwargs: dict[str, Any] = {"mode": "ai"}
    if depth is not None:
        kwargs["depth"] = depth
    if boxes:
        kwargs["boxes"] = True
    from playwright.async_api import Error as PlaywrightError

    for attempt in range(3):
        try:
            return await page.aria_snapshot(**kwargs)
        except PlaywrightError as exc:
            # A navigation in flight destroys the execution context; wait for the new document.
            if attempt == 2 or page.is_closed() or not any(s in str(exc) for s in _NAV_ERRORS):
                raise
            log.debug("snapshot raced a navigation (%s); retrying", str(exc).splitlines()[0])
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightError:
                await asyncio.sleep(0.25)
    raise AssertionError("unreachable")


def _header(line: str) -> str:
    """The node part of a snapshot line (before the text value), with quoted names blanked."""
    return _QUOTED_RE.sub('""', line).split(": ", 1)[0]


def subtree(snapshot_text: str, ref: str, depth: int | None = None) -> str | None:
    """Cut the subtree of the node carrying ``[ref=<ref>]`` out of an AI snapshot (dedented).

    Returns ``None`` if no node has that ref. Only real node headers are matched, never text that
    merely contains ``[ref=...]`` inside a quoted name or a text value.
    """
    lines = snapshot_text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip(" ")
        if not stripped.startswith("- ") or ref not in _HEADER_REF_RE.findall(_header(stripped)):
            continue
        indent = len(line) - len(stripped)
        out = [stripped]
        for nxt in lines[i + 1:]:
            body = nxt.lstrip(" ")
            ind = len(nxt) - len(body)
            if body and ind <= indent:
                break
            if depth is not None:
                level = (ind - indent) // 2
                is_prop = body.startswith("- /")
                if level > depth + (1 if is_prop else 0):
                    continue
            out.append(nxt[indent:])
        return "\n".join(out)
    return None


def snapshot_refs(snapshot_text: str) -> list[str]:
    """All element refs present in a snapshot, in document order."""
    refs = []
    for line in snapshot_text.splitlines():
        stripped = line.lstrip(" ")
        if stripped.startswith("- "):
            refs.extend(_HEADER_REF_RE.findall(_header(stripped)))
    return refs


# ---------------------------------------------------------------------- read_page

# Shared by the in-page scripts below: visibility rules from computed styles and layout boxes.
_VISIBILITY_JS = r"""
  const ZERO_CLIP = /^rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)$/;
  const sx = window.scrollX, sy = window.scrollY, vw = window.innerWidth, vh = window.innerHeight;
  const escText = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const escAttr = (s) => escText(s).replace(/"/g,'&quot;');
  const transparent = (c) => c === 'transparent' || /^rgba\(.*,\s*0(\.0+)?\)$/.test(c) || /\/\s*0(\.0+)?\)$/.test(c);
  // parent across shadow boundaries (a shadow root's parent is its host element)
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;

  function hidden(el, cs) {
    if (cs.display === 'none') return true;
    if (el.getAttribute('aria-hidden') === 'true') return true;
    if (cs.display === 'contents') return false;            // no own box; children are judged individually
    if (cs.contentVisibility === 'hidden') return true;
    if (parseFloat(cs.opacity) === 0) return true;
    if (typeof el.checkVisibility === 'function' && !el.checkVisibility({opacityProperty: true})) return true;
    // <html>/<body> often have no height of their own (fixed-position app shells) and their overflow
    // applies to the viewport: never judge them by geometry.
    if (el === document.body || el === document.documentElement) return false;
    const rect = el.getBoundingClientRect();
    const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible';
    if (clips && (rect.width < 2 || rect.height < 2)) return true;    // sr-only boxes, collapsed panels
    if (rect.width === 0 && rect.height === 0 && el.getClientRects().length === 0) return true;
    const positioned = cs.position === 'absolute' || cs.position === 'fixed';
    if (positioned && ZERO_CLIP.test(cs.clip)) return true;
    if (/inset\(\s*(50|100)%/.test(cs.clipPath) || /circle\(\s*0(px|%)?\s*(at|\))/.test(cs.clipPath)) return true;
    if (/^matrix\(0, 0, 0, 0,/.test(cs.transform) || /^matrix\(0, 0,.*, 0, 0\)$/.test(cs.transform)) return true;
    if (cs.position === 'absolute' && (rect.right + sx <= 0 || rect.bottom + sy <= 0)) return true;
    if (cs.position === 'fixed' && (rect.right <= 0 || rect.bottom <= 0 || rect.left >= vw || rect.top >= vh)) return true;
    return false;
  }

  function textHidden(cs) {
    if (cs.visibility !== 'visible') return true;
    if (parseFloat(cs.fontSize) === 0) return true;
    const clipText = (cs.backgroundClip || cs.webkitBackgroundClip || '') === 'text' || cs.webkitBackgroundClip === 'text';
    if (transparent(cs.color) && !clipText) return true;
    if (transparent(cs.webkitTextFillColor || '') && !clipText) return true;
    if (parseFloat(cs.textIndent) <= -999) return true;
    return false;
  }
"""

# Runs in the page. Walks the live (composed) tree read-only, decides visibility from computed
# styles and layout boxes, and serialises the visible part straight to strings - no DOM nodes are
# created, cloned or modified, so no page code is triggered. ``opts.roots`` (elements resolved by
# Playwright's selector engine, which pierces open shadow roots) limits the output to them.
_READ_JS = r"""
(opts) => {
  const DROP = new Set(['script','style','noscript','template','svg','canvas','frameset',
    'object','embed','video','audio','source','track','link','meta','head','title','base','map','area',
    'input','textarea','datalist','param']);
  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','source','track','wbr']);
  const KEEP_ATTRS = ['href','src','alt','title','id','class','role','aria-label','colspan','rowspan','datetime',
    'lang','dir','name','headers','scope','start','type'];
  const BLOCK = new Set(['block','flex','grid','list-item','table','flow-root','table-caption','table-row',
    'table-row-group','table-header-group','table-footer-group','-webkit-box','block flow','block flow-root']);
""" + _VISIBILITY_JS + r"""
  let removed = 0, frames = 0, deferred = 0;

  // Text that is hidden only until the page reveals it on scroll (opacity 0 / visibility hidden
  // below the viewport, e.g. AOS / ScrollTrigger animations): counted, never included.
  function notRevealedYet(el, cs) {
    if (!(parseFloat(cs.opacity) === 0 || cs.visibility !== 'visible')) return false;
    if (!(el.textContent || '').trim()) return false;
    return el.getBoundingClientRect().top >= vh;
  }

  function absUrl(el, attr) {
    let v = '';
    try {
      if (attr === 'href' && typeof el.href === 'string') v = el.href;
      else if (attr === 'src') v = el.currentSrc || el.src || el.getAttribute('src') || '';
      else v = el.getAttribute(attr) || '';
    } catch (e) { v = el.getAttribute(attr) || ''; }
    if (/^\s*javascript:/i.test(v)) return null;
    if (/^data:/i.test(v)) return v.length > 120 ? null : v;
    return v;
  }

  // Build a light tree: {t, a, c, d, ws} for elements, {x, ws, pre} for text.
  function build(node, parentWs, parentTextHidden) {
    if (node.nodeType === 3) {
      if (parentTextHidden || !node.data) return null;
      return {x: node.data, ws: parentWs};
    }
    if (node.nodeType !== 1) return null;            // comments, processing instructions
    const el = node;
    const tag = el.localName;
    if (tag === 'iframe' || tag === 'frame') {   // frame content is not part of this document
      if (!hidden(el, getComputedStyle(el))) frames++;
      removed++;
      return null;
    }
    if (DROP.has(tag) || el.namespaceURI === 'http://www.w3.org/2000/svg') { removed++; return null; }
    const cs = getComputedStyle(el);
    if (hidden(el, cs)) {
      if (!parentTextHidden && notRevealedYet(el, cs)) deferred++;
      removed++;
      return null;
    }
    const out = {t: tag, a: [], c: [], d: cs.display, ws: cs.whiteSpace || 'normal'};
    if (cs.whiteSpaceCollapse === 'preserve' || cs.whiteSpaceCollapse === 'preserve-breaks') out.ws = cs.whiteSpaceCollapse === 'preserve' ? 'pre' : 'pre-line';
    for (const name of KEEP_ATTRS) {
      if (!el.hasAttribute(name)) continue;
      const v = (name === 'href' || name === 'src') ? absUrl(el, name) : el.getAttribute(name);
      if (v !== null && v !== '') out.a.push([name, v]);
    }
    if (tag === 'select') {
      const chosen = Array.from(el.selectedOptions || []).map(o => (o.label || o.text || '').trim()).filter(Boolean);
      if (chosen.length) out.c.push({x: chosen.join(', '), ws: 'normal'});
      out.t = 'span';
      return out;
    }
    const tHidden = textHidden(cs);
    if (tHidden && !parentTextHidden && cs.visibility !== 'visible' && notRevealedYet(el, cs)) deferred++;
    let kids;
    if (tag === 'details' && !el.open) kids = Array.from(el.children).filter(c => c.localName === 'summary').slice(0, 1);
    else if (el.shadowRoot) kids = el.shadowRoot.childNodes;
    else if (tag === 'slot') {
      kids = el.assignedNodes({flatten: true});
      if (!kids.length) kids = el.childNodes;
    } else kids = el.childNodes;
    for (const k of kids) {
      const b = build(k, out.ws, tHidden);
      if (b) out.c.push(b);
    }
    if (tag === 'slot') out.t = 'span-contents';
    return out;
  }

  function toHtml(n) {
    if (n.x !== undefined) return escText(n.x);
    const inner = n.c.map(toHtml).join('');
    if (n.t === 'span-contents') return inner;
    const attrs = n.a.map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('');
    if (VOID.has(n.t)) return `<${n.t}${attrs}>`;
    return `<${n.t}${attrs}>${inner}</${n.t}>`;
  }

  // innerText-like serialisation driven by the computed display values captured above.
  function collect(n, items) {
    if (n.x !== undefined) {
      if (/^(pre|pre-wrap|break-spaces)$/.test(n.ws)) items.push({pre: n.x});
      else if (n.ws === 'pre-line') items.push({pre: n.x.replace(/[ \t]+/g, ' ')});
      else items.push(n.x.replace(/[\t\n\r\f ]+/g, ' '));
      return;
    }
    if (n.t === 'br') { items.push({pre: '\n'}); return; }
    if (n.t === 'img') { return; }
    let gap = 0;
    if (n.t === 'p') gap = 2;
    else if (BLOCK.has(n.d || '')) gap = 1;
    if (gap) items.push(gap);
    for (const c of n.c) collect(c, items);
    if (n.d === 'table-cell') items.push('\t');
    if (gap) items.push(gap);
  }

  function toText(nodes) {
    const items = [];
    for (const n of nodes) { collect(n, items); items.push(1); }
    let res = '', pending = 0;
    for (const it of items) {
      if (typeof it === 'number') { pending = Math.max(pending, it); continue; }
      let s = typeof it === 'string' ? it : it.pre;
      if (!s) continue;
      if (typeof it === 'string' && s.trim() === '' && (pending || res === '' || res.endsWith('\n'))) continue;
      if (pending && res) { res = res.replace(/[ \t]+$/, '') + '\n'.repeat(pending); }
      pending = 0;
      if (typeof it === 'string') {
        if (res === '' || res.endsWith('\n')) s = s.replace(/^ +/, '');
        else if (/[ \t]$/.test(res) && s.startsWith(' ')) s = s.slice(1);
      }
      res += s;
    }
    return res.split('\n').map(l => l.replace(/[ \t]+$/, '')).join('\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  const deepContains = (a, b) => { for (let n = b; n; n = up(n)) if (n === a) return true; return false; };
  let roots;
  if (opts.roots) {
    roots = Array.from(opts.roots);
    if (!roots.length) return {error: 'no_match'};
    // keep only outermost matches (also across shadow boundaries)
    roots = roots.filter(r => !roots.some(o => o !== r && deepContains(o, r)));
  } else if (opts.mainOnly) {
    const main = Array.from(document.querySelectorAll('main, [role="main"]')).find(e => getComputedStyle(e).display !== 'none');
    const articles = Array.from(document.querySelectorAll('article')).filter(e => getComputedStyle(e).display !== 'none');
    roots = [main || (articles.length === 1 ? articles[0] : null) || document.body || document.documentElement];
  } else {
    roots = [document.body || document.documentElement];
  }
  const built = [];
  for (const r of roots) {
    // Hidden ancestors hide the root too (e.g. a selector that matches inside a display:none box).
    let anc = up(r), ancHidden = false;
    while (anc && anc !== document.documentElement) {
      const cs = getComputedStyle(anc);
      if (cs.display === 'none' || anc.getAttribute('aria-hidden') === 'true' || parseFloat(cs.opacity) === 0) { ancHidden = true; break; }
      anc = up(anc);
    }
    if (ancHidden) { removed++; continue; }
    const b = build(r, 'normal', false);
    if (b) built.push(b);
  }
  const res = {roots: roots.length, kept: built.length, removed, frames, deferred};
  if (opts.fmt === 'text') res.text = toText(built);
  else res.html = built.map(toHtml).join('\n');
  return res;
}
"""


# Runs in the page (or a frame). Serialises the document for extraction without what a reader cannot
# see: hidden elements and hidden text in <body> are skipped using the same rules as _READ_JS, every
# attribute is kept, <head> stays as it is (meta / JSON-LD), and open shadow roots are inlined as
# <template shadowrootmode="open"> so CSS selectors match web-component content. Read-only.
_VISIBLE_HTML_JS = r"""
() => {
  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','source','track','wbr']);
  const VERBATIM = new Set(['script','style','link','meta','title','base']);   // not rendered: kept as is
  const WHOLE = new Set(['select','datalist']);    // options have no boxes of their own
""" + _VISIBILITY_JS + r"""
  const attrs = (el) => Array.from(el.attributes).map(a => ` ${a.name}="${escAttr(a.value)}"`).join('');
  function ser(node, textHid, check) {
    if (node.nodeType === 3) return textHid ? '' : escText(node.data);
    if (node.nodeType !== 1) return '';
    const el = node, tag = el.localName;
    if (VERBATIM.has(tag)) return el.outerHTML;
    let tHid = textHid;
    if (check) {
      const cs = getComputedStyle(el);
      if (hidden(el, cs)) return '';
      tHid = textHidden(cs);
    }
    const open = `<${tag}${attrs(el)}>`;
    if (VOID.has(tag)) return open;
    if (WHOLE.has(tag)) return open + el.innerHTML + `</${tag}>`;
    if (tag === 'template') return '';
    const parts = [];
    if (el.shadowRoot) {
      const shadow = Array.from(el.shadowRoot.childNodes).map(n => ser(n, tHid, true)).join('');
      parts.push('<template shadowrootmode="open">' + shadow + '</template>');
    }
    for (const c of el.childNodes) parts.push(ser(c, tHid, true));
    return open + parts.join('') + `</${tag}>`;
  }
  const doc = document.documentElement;
  const head = document.head ? document.head.outerHTML : '';
  const body = document.body ? ser(document.body, false, false) : '';
  return `<!DOCTYPE html><html${attrs(doc)}>${head}${body}</html>`;
}
"""

# Full document including open shadow roots (Chrome 125+ getHTML); null when there are none.
_SHADOW_HTML_JS = r"""
() => {
  const roots = [];
  const walk = (r) => { for (const el of r.querySelectorAll('*')) if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); } };
  walk(document);
  if (!roots.length || typeof Element.prototype.getHTML !== 'function') return null;
  return '<!DOCTYPE html>' + document.documentElement.getHTML({shadowRoots: roots});
}
"""


async def _evaluate(target: Any, script: str, arg: Any = None) -> Any:
    """``target.evaluate`` (page or frame), retried when a navigation destroys the context."""
    from playwright.async_api import Error as PlaywrightError

    for attempt in range(3):
        try:
            return await target.evaluate(script, arg)
        except PlaywrightError as exc:
            closed = target.is_closed() if hasattr(target, "is_closed") else target.is_detached()
            if attempt == 2 or closed or not any(s in str(exc) for s in _NAV_ERRORS):
                raise
            try:
                await target.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightError:
                await asyncio.sleep(0.25)
    raise AssertionError("unreachable")


async def visible_html(target: Any) -> str:
    """HTML of a page or frame without hidden elements / text (open shadow roots inlined)."""
    return await _evaluate(target, _VISIBLE_HTML_JS)


async def full_html(target: Any) -> str:
    """HTML of a page or frame including hidden elements (open shadow roots inlined when present)."""
    html = await _evaluate(target, _SHADOW_HTML_JS)
    return html if isinstance(html, str) else await target.content()


async def read_page(
    page: "Page",
    *,
    fmt: ReadFormat = "markdown",
    selector: str | None = None,
    main_only: bool = False,
) -> str:
    """Readable content of ``page`` as ``markdown``, ``text`` or ``html`` without hidden content.

    ``selector`` (CSS, or any Playwright selector such as ``text=...``; it pierces open shadow
    roots like the action tools do) limits the output to the matching elements (all outermost
    matches, in document order). ``main_only`` picks ``<main>`` / ``[role=main]`` (or a single
    ``<article>``) when present, else ``<body>``. Shadow DOM (open roots) is flattened. Iframe
    content is not included: a note says how many visible iframes were skipped, and how many hidden
    text blocks below the viewport are waiting to be revealed on scroll.
    """
    if fmt not in ("markdown", "text", "html"):
        raise ProfilePilotError(f"Unknown format {fmt!r}; use 'markdown', 'text' or 'html'.")
    sel = (selector or "").strip() or None
    opts = {"fmt": "text" if fmt == "text" else "html", "mainOnly": bool(main_only)}
    from playwright.async_api import Error as PlaywrightError

    for attempt in range(3):
        try:
            if sel is None:
                result = await page.evaluate(_READ_JS, opts)
            else:
                # Playwright's selector engine (the one browser_click uses): pierces open shadow roots
                result = await page.locator(sel).evaluate_all(
                    "(els, o) => (" + _READ_JS + ")(Object.assign({}, o, {roots: els}))", opts
                )
            break
        except PlaywrightError as exc:
            message = str(exc)
            if sel is not None and not any(s in message for s in _NAV_ERRORS) and (
                "selector" in message.lower() or "parsing" in message.lower()
            ):
                raise InvalidTargetError(f"Invalid selector {selector!r}: {message.splitlines()[0][:200]}") from None
            if attempt == 2 or page.is_closed() or not any(s in message for s in _NAV_ERRORS):
                raise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightError:
                await asyncio.sleep(0.25)
    if result.get("error") == "no_match":
        raise NotFoundError(
            f"No element matches the selector {selector!r}. (Content inside iframes is not searched; use "
            "browser_snapshot for it.)"
        )
    log.debug("read_page: kept %s/%s roots, removed %s hidden/non-content elements",
              result.get("kept"), result.get("roots"), result.get("removed"))
    if fmt == "text":
        body = result.get("text") or ""
    else:
        html = result.get("html") or ""
        body = html if fmt == "html" else html_to_markdown(html)
    notes = read_notes(result, fmt)
    if not notes:
        return body
    return (body if body.strip() else "(no visible content)") + notes


def read_notes(result: dict[str, Any], fmt: str) -> str:
    """Fixed notes (never page text) about content :func:`read_page` could not include."""
    notes = ""
    deferred = int(result.get("deferred") or 0)
    if deferred and fmt in ("markdown", "text"):
        notes += (f"\n\n({deferred} hidden text block(s) below the visible area were omitted - pages often "
                  "reveal these on scroll; call browser_scroll, then browser_read again, to include them.)")
    frames = int(result.get("frames") or 0)
    if frames:
        notes += (f"\n\n[{frames} iframe(s) not included: use browser_snapshot to see their content "
                  "(refs like f1eN)]")
    return notes


def html_to_markdown(html: str) -> str:
    """Convert (already cleaned) HTML to compact Markdown with ``markdownify``."""
    if not html.strip():
        return ""
    from markdownify import markdownify

    text = markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
    )
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------- extract

_PSEUDO_RE = re.compile(r"::(text|attr\(\s*([^)]+?)\s*\))\s*$")


def extract(
    html: str,
    url: str = "",
    *,
    css: str | None = None,
    xpath: str | None = None,
    attr: str | None = None,
    limit: int = 50,
) -> list[str]:
    """Extract strings from ``html`` with a CSS selector or an XPath expression.

    CSS goes through Scrapling's parser and supports Scrapy-style ``::text`` and ``::attr(name)``
    pseudo-elements; XPath is evaluated with lxml directly, so expressions returning numbers or
    strings (``count(...)``, ``string(...)``) work too. For element matches
    ``attr`` picks an attribute (``href``/``src`` are made absolute against ``url``); ``attr="html"``
    returns the element's outer HTML; otherwise the element's whitespace-normalised text is
    returned. URL attributes (``href``, ``src``, ...) are always resolved against ``url``. At most
    ``limit`` non-empty results are returned.
    """
    if bool(css) == bool(xpath):
        raise InvalidTargetError("Give exactly one of 'css' or 'xpath'.")
    limit = max(1, int(limit))
    if xpath:
        # Scrapling cannot wrap scalar XPath results (count(), string()); lxml handles everything.
        return _extract_lxml(html, url, css=None, xpath=xpath, attr=attr, limit=limit)
    try:
        from scrapling.parser import Selector
    except Exception:  # scrapling missing or broken: plain lxml + cssselect
        log.debug("scrapling parser unavailable; using lxml", exc_info=True)
        return _extract_lxml(html, url, css=css, xpath=None, attr=attr, limit=limit)

    page = Selector(content=html or "<html></html>", url=url or "")
    try:
        matches = page.css(css)
    except Exception as exc:
        raise InvalidTargetError(f"Invalid CSS selector: {_first_line(exc)}") from None

    pseudo = _PSEUDO_RE.search(css)
    pseudo_attr = pseudo.group(2).strip("'\"") if pseudo and pseudo.group(2) else None
    out: list[str] = []
    for item in matches:
        value = _selector_value(item, url, attr)
        if value and pseudo_attr:
            value = _abs_url(url, pseudo_attr, value)
        if value:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _selector_value(item: Any, url: str, attr: str | None) -> str | None:
    if getattr(item, "tag", None) == "#text":
        text = str(item.get())
        return _norm_ws(text) if text.strip() else None
    if attr:
        if attr.lower() in ("html", "outerhtml"):
            return str(item.html_content)
        value = item.attrib.get(attr)
        if value is None:
            return None
        return _abs_url(url, attr, str(value))
    text = item.get_all_text(separator=" ", strip=True)
    return _norm_ws(str(text)) or None


def _extract_lxml(html: str, url: str, *, css: str | None, xpath: str | None, attr: str | None, limit: int) -> list[str]:
    import lxml.html

    root = lxml.html.fromstring(html or "<html></html>")
    pseudo_attr: str | None = None
    want_text = False
    if css:
        match = _PSEUDO_RE.search(css)
        if match:
            css = css[: match.start()]
            if match.group(1) == "text":
                want_text = True
            else:
                pseudo_attr = match.group(2).strip("'\"")
        try:
            from cssselect import GenericTranslator
            from cssselect.parser import SelectorError
        except ImportError:
            raise ProfilePilotError("CSS extraction needs the 'scrapling' or 'cssselect' package; use xpath instead.") from None
        try:
            expr = GenericTranslator().css_to_xpath(css or "*")
        except SelectorError as exc:
            raise InvalidTargetError(f"Invalid CSS selector: {exc}") from None
        if want_text:
            expr += "/text()"
        elif pseudo_attr:
            expr += f"/@{pseudo_attr}"
    else:
        expr = xpath or ""
    try:
        result = root.xpath(expr)
    except Exception as exc:
        raise InvalidTargetError(f"Invalid XPath: {_first_line(exc)}") from None
    if not isinstance(result, list):
        if isinstance(result, bool):
            return ["true" if result else "false"]
        if isinstance(result, float) and result.is_integer():
            return [str(int(result))]
        return [str(result)]
    out: list[str] = []
    for node in result:
        if isinstance(node, str):  # text() / @attr results (lxml "smart strings")
            name = getattr(node, "attrname", None) or pseudo_attr
            value = _abs_url(url, name, str(node)) if name else _norm_ws(str(node))
        elif attr:
            if attr.lower() in ("html", "outerhtml"):
                value = lxml.html.tostring(node, encoding="unicode", with_tail=False)
            else:
                raw = node.get(attr)
                value = _abs_url(url, attr, raw) if raw is not None else None
        else:
            value = _norm_ws(node.text_content()) if hasattr(node, "text_content") else _norm_ws(str(node))
        if value:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _abs_url(url: str, attr: str, value: str) -> str:
    if attr.lower() in ("href", "src", "action", "data-src", "poster") and url and value.strip():
        return urljoin(url, value.strip())
    return value


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:300] if text else type(exc).__name__


# ---------------------------------------------------------------------- pagination


def paginate(text: str, *, offset: int = 0, max_chars: int = 12000) -> tuple[str, int | None]:
    """Return ``(chunk, next_offset)`` for long tool output; ``next_offset`` is None at the end.

    Chunks are deterministic for the same text/offset/max_chars. A chunk ends at a line break (or
    else a space) in the last fifth of the window when one exists, so lines are rarely cut.
    """
    if max_chars < 1:
        raise ProfilePilotError("max_chars must be at least 1.")
    if offset < 0:
        raise ProfilePilotError("offset must be >= 0.")
    total = len(text)
    if offset >= total:
        return "", None
    end = offset + max_chars
    if end >= total:
        return text[offset:], None
    window_start = offset + (max_chars * 4) // 5
    cut = text.rfind("\n", window_start, end)
    if cut != -1:
        end = cut + 1
    else:
        cut = text.rfind(" ", window_start, end)
        if cut != -1:
            end = cut + 1
    return text[offset:end], end
