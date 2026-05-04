"""
parser.py — Extraction layer that turns raw Canvas HTML into structured NewsEntry dicts.

Only parses the "News feed" section of the page. Ignores all surrounding
Canvas markup (Lectures, Discussions, Projects, etc.) to avoid noise.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Literal, TypedDict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)


class TextSegment(TypedDict):
    type: Literal["text"]
    text: str


class LinkSegment(TypedDict):
    type: Literal["link"]
    href: str
    label: str


ItemSegment = TextSegment | LinkSegment


class NewsEntry(TypedDict):
    date_raw: str
    date_normalized: str
    items: list[list[ItemSegment]]
    content_text: str
    content_hash: str


class ParseError(Exception):
    """Raised when the parser cannot locate or extract the News Feed section."""


def normalize_text(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip edges."""
    return re.sub(r"\s+", " ", text).strip()


def compute_hash(text: str) -> str:
    """SHA-256 hex digest of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_line_plain(segments: list[ItemSegment]) -> str:
    """Stable one-line plaintext for hashing and Gemini (segments → words + URLs)."""
    parts: list[str] = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(seg["text"])
        else:
            parts.append(f'{seg["label"]} {seg["href"]}')
    return normalize_text(" ".join(parts))


def _guess_canvas_origin(soup: BeautifulSoup) -> str:
    """Pick https origin for resolving relative URLs; prefer instructure.com."""
    instruct: list[str] = []
    other: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href.startswith(("http://", "https://")):
            continue
        p = urlparse(href)
        if p.scheme in ("http", "https") and p.netloc:
            base = f"{p.scheme}://{p.netloc}"
            if "instructure.com" in p.netloc.lower():
                instruct.append(base)
            else:
                other.append(base)
    return instruct[0] if instruct else (other[0] if other else "")


def _document_base_url(soup: BeautifulSoup) -> str | None:
    b = soup.find("base", href=True)
    if not b:
        return None
    raw = str(b["href"]).strip()
    return raw if raw else None


def _safe_http_href(href_raw: str, soup: BeautifulSoup, origin_fallback: str) -> str | None:
    """Return absolute http(s) URL or None when unsafe / unresolvable."""
    href = (href_raw or "").strip()
    if not href or href.startswith("#"):
        return None
    lower = href.lower()
    if (
        lower.startswith("javascript:")
        or lower.startswith("mailto:")
        or lower.startswith("data:")
        or lower.startswith("vbscript:")
    ):
        return None

    base = _document_base_url(soup)

    if lower.startswith(("http://", "https://")):
        return href

    if href.startswith("//"):
        return "https:" + href

    if href.startswith("/"):
        if base:
            return urljoin(base, href)
        if origin_fallback:
            return urljoin(origin_fallback.rstrip("/") + "/", href.lstrip("/"))
        return None

    return None


def _find_news_feed_heading(soup: BeautifulSoup) -> Tag:
    """Locate the heading element whose text is 'News feed' (case-insensitive).

    Searches all heading levels and also generic tags that Canvas might use
    as section titles.
    """
    for tag in soup.find_all(re.compile(r"^h[1-6]$", re.IGNORECASE)):
        if normalize_text(tag.get_text()).lower() == "news feed":
            return tag

    for tag in soup.find_all(["strong", "b", "span", "div", "p"]):
        if normalize_text(tag.get_text()).lower() == "news feed":
            return tag

    raise ParseError(
        "Could not find a 'News feed' heading on the page. "
        "The page structure may have changed."
    )


def _collect_section_siblings(heading: Tag) -> list[Tag]:
    """Collect all sibling elements after the News Feed heading until we hit
    another section boundary (a same-level or higher heading that is NOT a
    date-like h3 inside the feed).

    The heuristic: stop when we encounter an element that looks like a new
    top-level section title (h1/h2 or an h-tag whose text doesn't look like
    a date).
    """
    heading_level = int(heading.name[1]) if heading.name and heading.name[0] == "h" else 2
    siblings: list[Tag] = []

    for sibling in heading.find_next_siblings():
        if not isinstance(sibling, Tag):
            continue
        if sibling.name and re.match(r"^h[1-6]$", sibling.name, re.IGNORECASE):
            sib_level = int(sibling.name[1])
            if sib_level <= heading_level:
                break
            siblings.append(sibling)
        else:
            siblings.append(sibling)

    return siblings


_DATE_PATTERN = re.compile(
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s",
    re.IGNORECASE,
)


def _looks_like_date(text: str) -> bool:
    """Return True if the text contains a day-of-week name, matching the
    Canvas News Feed date heading pattern. Uses search (not match) to handle
    prefixed headings like 'Later update on Monday, March 23'."""
    return bool(_DATE_PATTERN.search(text.strip()))


def _merge_adjacent_text(segments: list[ItemSegment]) -> list[ItemSegment]:
    out: list[ItemSegment] = []
    for seg in segments:
        if seg["type"] != "text":
            out.append(seg)
            continue
        if out and out[-1]["type"] == "text":
            prev_t = out[-1]["text"]
            merged = normalize_text(prev_t + " " + seg["text"])
            out[-1] = TextSegment(type="text", text=merged)
        else:
            out.append(TextSegment(type="text", text=seg["text"]))
    return out


def _segments_from_children(container: Tag, soup: BeautifulSoup, origin_fallback: str) -> list[ItemSegment]:
    segs: list[ItemSegment] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            t = normalize_text("".join(buf))
            if t:
                segs.append(TextSegment(type="text", text=t))
            buf = []

    for c in container.children:
        if isinstance(c, NavigableString):
            buf.append(str(c))
            continue
        if not isinstance(c, Tag):
            continue
        if c.name in ("script", "style"):
            continue
        flush()
        if c.name == "br":
            buf.append(" ")
        elif c.name == "a" and c.get("href"):
            h = _safe_http_href(str(c["href"]), soup, origin_fallback)
            lab = normalize_text(c.get_text()) or (h or "")
            if h and lab:
                segs.append(LinkSegment(type="link", href=h, label=lab))
            elif lab:
                segs.append(TextSegment(type="text", text=lab))
        elif c.name in ("ul", "ol"):
            for nli in c.find_all("li", recursive=False):
                segs.extend(_segments_from_children(nli, soup, origin_fallback))
        else:
            segs.extend(_segments_from_children(c, soup, origin_fallback))
    flush()
    return _merge_adjacent_text(segs)


def _extract_li_segments(li: Tag, soup: BeautifulSoup, origin_fallback: str) -> list[ItemSegment]:
    return _merge_adjacent_text(_segments_from_children(li, soup, origin_fallback))


def _line_nonempty(segments: list[ItemSegment]) -> bool:
    for s in segments:
        if s["type"] == "link":
            return True
        if s["text"]:
            return True
    return False


def parse_news_feed(html: str) -> list[NewsEntry]:
    """Parse the News Feed section of a Canvas course page into structured entries.

    Returns entries in page order (newest first).
    Raises ParseError if the section cannot be found or contains no entries.
    """
    soup = BeautifulSoup(html, "lxml")
    heading = _find_news_feed_heading(soup)
    logger.debug(
        "Found News Feed heading: <%s> at line %s", heading.name, heading.sourceline
    )

    siblings = _collect_section_siblings(heading)
    if not siblings:
        raise ParseError(
            "Found the News Feed heading but no content follows it. "
            "The page structure may have changed."
        )

    origin_fallback = _guess_canvas_origin(soup)

    entries: list[NewsEntry] = []
    current_date_raw: str | None = None

    for elem in siblings:
        text = normalize_text(elem.get_text())

        if elem.name and re.match(r"^h[1-6]$", elem.name, re.IGNORECASE):
            if _looks_like_date(text):
                current_date_raw = text
            else:
                logger.warning("Skipping non-date heading in News Feed: '%s'", text[:80])
            continue

        if elem.name == "ol" and current_date_raw is not None:
            item_segments = [
                _extract_li_segments(li, soup, origin_fallback)
                for li in elem.find_all("li", recursive=False)
            ]
            item_segments = [ln for ln in item_segments if _line_nonempty(ln)]

            if not item_segments:
                logger.warning(
                    "Empty <ol> found under date '%s', skipping",
                    current_date_raw,
                )
                continue

            content_text = "\n".join(
                canonical_line_plain(line) for line in item_segments
            )
            entries.append(
                NewsEntry(
                    date_raw=current_date_raw,
                    date_normalized=normalize_text(current_date_raw).lower(),
                    items=item_segments,
                    content_text=content_text,
                    content_hash=compute_hash(content_text),
                )
            )
            current_date_raw = None

    if not entries:
        raise ParseError(
            "Parsed the News Feed section but found zero date/list entry pairs. "
            "Expected repeated <h3> date + <ol> list blocks."
        )

    first = entries[0]
    if not first["date_raw"] or not first["items"]:
        raise ParseError(
            f"First parsed entry is malformed: date_raw={first['date_raw']!r}, "
            f"items count={len(first['items'])}"
        )

    logger.info(
        "Parsed %d News Feed entries; newest date: '%s' (%d items)",
        len(entries),
        first["date_raw"],
        len(first["items"]),
    )
    return entries
