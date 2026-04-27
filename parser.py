"""
parser.py — Extraction layer that turns raw Canvas HTML into structured NewsEntry dicts.

Only parses the "News feed" section of the page. Ignores all surrounding
Canvas markup (Lectures, Discussions, Projects, etc.) to avoid noise.
"""

import hashlib
import logging
import re
from typing import TypedDict

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


class NewsEntry(TypedDict):
    date_raw: str
    date_normalized: str
    items: list[str]
    content_text: str
    content_hash: str


class ParseError(Exception):
    """Raised when the parser cannot locate or extract News Feed entries."""


def normalize_text(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip edges."""
    return re.sub(r"\s+", " ", text).strip()


def compute_hash(text: str) -> str:
    """SHA-256 hex digest of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _extract_li_text(li: Tag) -> str:
    """Extract and normalize the full text of a list item, including any
    nested sub-list content flattened into the same string."""
    return normalize_text(li.get_text())


def parse_news_feed(html: str) -> list[NewsEntry]:
    """Parse the News Feed section of a Canvas course page into structured entries.

    Returns entries in page order (newest first).
    Raises ParseError if the section cannot be found or contains no entries.
    """
    soup = BeautifulSoup(html, "lxml")
    heading = _find_news_feed_heading(soup)
    logger.debug("Found News Feed heading: <%s> at line %s", heading.name, heading.sourceline)

    siblings = _collect_section_siblings(heading)
    if not siblings:
        raise ParseError(
            "Found the News Feed heading but no content follows it. "
            "The page structure may have changed."
        )

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
            items = [_extract_li_text(li) for li in elem.find_all("li", recursive=False)]
            items = [item for item in items if item]

            if not items:
                logger.warning("Empty <ol> found under date '%s', skipping", current_date_raw)
                continue

            content_text = "\n".join(items)
            entries.append(
                NewsEntry(
                    date_raw=current_date_raw,
                    date_normalized=normalize_text(current_date_raw).lower(),
                    items=items,
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
