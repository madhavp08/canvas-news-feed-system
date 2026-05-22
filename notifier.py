"""
notifier.py — Email notification layer using the SendGrid Web API (HTTP, no extra crypto deps).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from html import unescape as _html_unescape
from pathlib import Path
from typing import Any

import requests
from ad_feedback_store import vote_href
from parser import ItemSegment, canonical_line_plain, normalize_text
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
# https://docs.sendgrid.com/api-reference/mail-send/mail-send — max personalizations per request
SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST = 1000

# Bound Gemini input length (character budget for serialized announcement scan).
_GEMINI_MAX_PAYLOAD_CHARS = 12_000
_GEMINI_MAX_ITEM_CHARS = 2_000
# Default fastest Flash-tier model for latency (override with GEMINI_MODEL).
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
_GEMINI_HTTP_TIMEOUT_MS = 20_000

_GEMINI_SYSTEM_INSTRUCTION = """You summarize Canvas course news-feed announcements for busy students.
Produce 2–4 short bullets or plain sentences—informal wording is OK; perfect grammar not required as long as it is clear.
Stay concrete: deadlines, grading, policies, readings, labs, logistics. No fluff, no greetings.
Summarize only what classmates need to act on or remember. Do NOT restate every line of the bulletin verbatim."""

# React Email CLI (see email-render/). Subprocess timeout in seconds.
_REACT_EMAIL_RENDER_TIMEOUT = 25
_PREVIEW_MAX_LEN = 140

# HTML from subprocess/render must be at least this long unless plain is tiny.
_MIN_EMAIL_HTML_CHARS = 80
# When plain is long but HTML is tiny, treat React output as broken.
_HTML_SUSPECT_PLAIN_MIN = 400
_HTML_SUSPECT_MAX = 150

# Promo line in notification emails (http/https linkified; no raw HTML from .env).
_PROMO_HTTP_URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

# Default Thinkex mark for the promo callout (override with EMAIL_PROMO_LOGO_URL).
_DEFAULT_EMAIL_PROMO_LOGO_URL = (
    "https://www.thinkex.app/newlogothinkex-light.svg"
    "?dpl=dpl_5pcR8SVvBXLpbshQqofsNskgGwtd"
)


def _strip_bom(s: str) -> str:
    if s.startswith("\ufeff"):
        return s.lstrip("\ufeff")
    return s


def _normalize_rendered_html_fragment(raw: str | None) -> str | None:
    """Return HTML starting with ``<`` or ``None`` if *raw* is not usable.

    Strips BOM/edges, drops leading stderr-style noise before ``<!DOCTYPE`` / ``<html``.
    """
    if raw is None:
        return None
    s = _strip_bom(str(raw).strip())
    if not s:
        return None
    cand = s.lstrip()
    if cand.startswith("<"):
        return s
    low = s.lower()
    for marker in ("<!doctype", "<html"):
        idx = low.find(marker)
        if idx >= 0:
            sliced = s[idx:].strip()
            if sliced.lstrip().startswith("<"):
                if idx > 0:
                    logger.warning(
                        "Stripped %d leading non-HTML bytes from renderer stdout",
                        idx,
                    )
                return sliced
    return None


def _first_item_content_needles(items: list[list[ItemSegment]]) -> list[str]:
    """Substrings that should appear in HTML for the first bulletin line."""
    if not items:
        return []
    line = items[0]
    needles: list[str] = []
    lower_seen: set[str] = set()

    def add_one(x: str) -> None:
        t = normalize_text(x)
        if len(t) >= 3:
            k = t.lower()
            if k not in lower_seen:
                lower_seen.add(k)
                needles.append(t)

    plain = canonical_line_plain(line)
    if plain:
        add_one(plain)
    for seg in line:
        if seg["type"] == "link":
            add_one(str(seg.get("label") or ""))
        else:
            add_one(str(seg.get("text") or ""))
    return needles


def _html_size_suspicious(html: str, plain: str) -> bool:
    if len(html) < _MIN_EMAIL_HTML_CHARS and len(plain) > 40:
        return True
    if len(plain) >= _HTML_SUSPECT_PLAIN_MIN and len(html) <= _HTML_SUSPECT_MAX:
        return True
    return False


def _html_text_layer_for_match(html: str) -> str:
    """Lowercased, entity-decoded view of HTML for substring checks.

    React Email escapes ``&``, ``<``, ``>`` in text; comparing raw needles to
    the HTML string false-triggers legacy fallback.
    """
    return _html_unescape(html).lower()


def _email_html_passes_send_checks(
    html: str,
    *,
    date_raw: str,
    items: list[list[ItemSegment]],
    plain: str,
) -> bool:
    if not html or not html.lstrip().startswith("<"):
        return False
    if _html_size_suspicious(html, plain):
        logger.warning(
            "Email HTML looks too small (%d chars) vs plain (%d chars); rejecting",
            len(html),
            len(plain),
        )
        return False
    hl = _html_text_layer_for_match(html)
    if date_raw.lower() not in hl:
        logger.error(
            "Email HTML missing announcement date %r; rejecting rendered HTML",
            date_raw,
        )
        return False
    if items:
        needles = _first_item_content_needles(items)
        if needles:
            if not any(n.lower() in hl for n in needles):
                logger.error(
                    "Email HTML missing expected content from first bulletin line; "
                    "rejecting rendered HTML"
                )
                return False
    return True


def _ensure_sendgrid_html(
    html_raw: str | None,
    change_type: str,
    send_entry: dict,
    tldr_raw: str | None,
    plain: str,
) -> str:
    """Prefer React HTML when it normalizes and validates; otherwise legacy + TL;DR."""
    normalized = _normalize_rendered_html_fragment(html_raw)
    items: list[list[ItemSegment]] = send_entry["items"]
    date_raw = str(send_entry["date_raw"])
    if normalized is not None and _email_html_passes_send_checks(
        normalized,
        date_raw=date_raw,
        items=items,
        plain=plain,
    ):
        logger.info("Email HTML: using React Email layout (validated)")
        return normalized

    if html_raw and str(html_raw).strip():
        if normalized is None:
            logger.warning(
                "Renderer output was not valid HTML; falling back to legacy template"
            )
        else:
            logger.error(
                "Rendered HTML failed validation; falling back to legacy template"
            )

    out = _build_html_body(change_type, send_entry)
    if tldr_raw:
        out = _prepend_tldr_html(out, tldr_raw)
    logger.info(
        "Email HTML: using legacy template (React render skipped or validation failed)"
    )
    return out


def normalize_tldr(raw: str | None) -> dict | None:
    """Normalize Gemini (or other) TL;DR text for the React Email template.

    Returns a JSON-serializable dict matching ``email-render`` types, or ``None``
    when there is nothing to show.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    lines: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s:
            lines.append(s)
    if not lines:
        return None

    classified: list[tuple[str, str]] = []
    for ln in lines:
        stripped = _strip_bullet_prefix(ln)
        if stripped is not None:
            classified.append(("b", stripped))
        else:
            classified.append(("t", ln))

    kinds = [k for k, _ in classified]
    if not any(k == "b" for k in kinds):
        return {"kind": "paragraph", "text": "\n".join(t for _, t in classified)}

    if all(k == "b" for k in kinds):
        return {
            "kind": "bullets",
            "lead": None,
            "bullets": [t for _, t in classified],
        }

    if "b" in kinds:
        idx_first_b = kinds.index("b")
        head_ok = all(k == "t" for k in kinds[:idx_first_b])
        tail_ok = all(k == "b" for k in kinds[idx_first_b:])
        if head_ok and tail_ok:
            lead_part = " ".join(t for k, t in classified[:idx_first_b]).strip()
            bullets = [t for k, t in classified[idx_first_b:] if k == "b"]
            return {
                "kind": "bullets",
                "lead": lead_part or None,
                "bullets": bullets,
            }

    return {"kind": "paragraph", "text": "\n".join(lines)}


def _strip_bullet_prefix(line: str) -> str | None:
    """If ``line`` looks like a bullet/numbered list row, return the text after the marker."""
    s = line.strip()
    if not s:
        return None
    for prefix in ("- ", "* ", "• ", "– ", "— "):
        if s.startswith(prefix):
            inner = s[len(prefix) :].strip()
            return inner if inner else None
    m = re.match(r"^\d{1,3}[.)]\s+(.*)$", s)
    if m:
        inner = m.group(1).strip()
        return inner if inner else None
    return None


def _preview_text(
    subject: str,
    date_raw: str,
    tldr_norm: dict | None,
    tldr_raw: str | None,
) -> str:
    if tldr_norm:
        if tldr_norm.get("kind") == "paragraph":
            t = str(tldr_norm.get("text", "")).replace("\n", " ").strip()
            if t:
                return (t[:_PREVIEW_MAX_LEN] + "…") if len(t) > _PREVIEW_MAX_LEN else t
        elif tldr_norm.get("kind") == "bullets":
            lead = tldr_norm.get("lead")
            if lead and str(lead).strip():
                t = str(lead).strip()
                return (t[:_PREVIEW_MAX_LEN] + "…") if len(t) > _PREVIEW_MAX_LEN else t
            bullets = tldr_norm.get("bullets") or []
            if bullets:
                t = str(bullets[0]).strip()
                return (t[:_PREVIEW_MAX_LEN] + "…") if len(t) > _PREVIEW_MAX_LEN else t
    if tldr_raw and tldr_raw.strip():
        first = tldr_raw.strip().split("\n", 1)[0].strip()
        if first:
            return (
                (first[:_PREVIEW_MAX_LEN] + "…")
                if len(first) > _PREVIEW_MAX_LEN
                else first
            )
    base = subject.strip() or f"CMSC216 News — {date_raw}"
    return base[:_PREVIEW_MAX_LEN]


def _tsx_executable(email_render_dir: Path) -> Path | None:
    bin_dir = email_render_dir / "node_modules" / ".bin"
    name = "tsx.cmd" if sys.platform == "win32" else "tsx"
    p = bin_dir / name
    return p if p.exists() else None


def react_email_subprocess_available() -> bool:
    """True when ``email-render`` has a local ``tsx`` CLI (React Email HTML path)."""
    email_dir = Path(__file__).resolve().parent / "email-render"
    return _tsx_executable(email_dir) is not None


def _render_news_email_html_subprocess(props: dict) -> str | None:
    root = Path(__file__).resolve().parent
    email_dir = root / "email-render"
    tsx = _tsx_executable(email_dir)
    if tsx is None:
        logger.warning(
            "React Email skipped: tsx not found under %s — run: cd email-render && npm ci",
            email_dir,
        )
        return None

    cli = email_dir / "src" / "render-cli.tsx"
    cmd = [str(tsx), str(cli)]
    # tsx uses a pipe under $TMPDIR; use a project-local dir so sandboxed/locked-down
    # environments (and CI) don't hit EPERM on OS temp listen.
    tsx_tmp = email_dir / ".tsx-tmp"
    child_env = os.environ.copy()
    try:
        tsx_tmp.mkdir(parents=True, exist_ok=True)
        tmp_s = str(tsx_tmp)
        child_env["TMPDIR"] = tmp_s
        child_env["TMP"] = tmp_s
        child_env["TEMP"] = tmp_s
    except OSError:
        pass
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(props, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=_REACT_EMAIL_RENDER_TIMEOUT,
            cwd=str(email_dir),
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "React Email render timed out after %ss; using legacy HTML",
            _REACT_EMAIL_RENDER_TIMEOUT,
        )
        return None
    except OSError as exc:
        logger.warning("React Email render failed: %s; using legacy HTML", exc)
        return None

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:500]
        logger.warning(
            "React Email CLI exited %s: %s; using legacy HTML",
            proc.returncode,
            err,
        )
        return None

    html = _normalize_rendered_html_fragment(proc.stdout)
    if not html:
        logger.warning("React Email stdout is not valid HTML; using legacy HTML")
    return html


def _render_news_email_html(
    props: dict,
    render_html_fn: Callable[[dict], str | None] | None,
) -> str | None:
    if os.environ.get("SKIP_REACT_EMAIL_HTML", "").strip() == "1":
        logger.info(
            "SKIP_REACT_EMAIL_HTML=1 — using legacy HTML (unset to use React Email)"
        )
        return None
    if render_html_fn is not None:
        try:
            raw_out = render_html_fn(props)
            if not raw_out or not str(raw_out).strip():
                logger.warning(
                    "React Email injectable renderer returned empty output; using legacy HTML"
                )
                return None
            norm = _normalize_rendered_html_fragment(str(raw_out))
            if norm is None:
                logger.warning(
                    "React Email injectable output could not be coerced to HTML; "
                    "using legacy HTML"
                )
                return None
            return norm
        except Exception as exc:  # noqa: BLE001 — boundary: custom renderer may raise
            logger.warning(
                "React Email injectable renderer failed: %s; using legacy HTML", exc
            )
            return None
    return _render_news_email_html_subprocess(props)


def _normalize_entry_items(raw: object) -> list[list[ItemSegment]]:
    """Convert legacy flat string lines into single text segments."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise TypeError("newest_entry['items'] must be a list")
    first_row = raw[0]
    if isinstance(first_row, str):
        return [[{"type": "text", "text": str(line)}] for line in raw]
    if isinstance(first_row, list):
        return raw  # type: ignore[return-value]
    raise TypeError(
        "newest_entry['items'] must be list[str] or list[list] of segments"
    )


def _segments_to_li_inner_html(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for s in segments:
        typ = str(s.get("type", ""))
        if typ == "link":
            href = str(s.get("href") or "")
            lab = str(s.get("label") or "")
            if _legacy_link_href_ok(href):
                parts.append(
                    f'<a href="{_escape_href_attr(href)}">{_escape_html(lab)}</a>'
                )
            else:
                parts.append(_escape_html(lab))
        else:
            parts.append(_escape_html(str(s.get("text", ""))))
    return "".join(parts)


def _legacy_link_href_ok(href: str) -> bool:
    lc = href.strip().lower()
    return lc.startswith("https://") or lc.startswith("http://")


def _escape_href_attr(href: str) -> str:
    # Avoid injecting attribute breaks; URIs rarely need more than quotes and &.
    return href.replace("&", "&amp;").replace('"', "&quot;")


def _ad_feedback_public_url() -> str | None:
    """Public HTTP(S) base for ``/vote?choice=…`` links; requires ``EMAIL_PROMO_TEXT``."""
    if not _email_promo_text():
        return None
    raw = (os.environ.get("AD_FEEDBACK_PUBLIC_URL") or "").strip()
    if not raw:
        return None
    if not _legacy_link_href_ok(raw):
        logger.warning(
            "AD_FEEDBACK_PUBLIC_URL is not http(s); ignoring (%s)",
            raw[:48],
        )
        return None
    return raw.rstrip("/")


def _react_email_props(
    change_type: str,
    newest_entry: dict,
    tldr_norm: dict | None,
    preview_text: str,
) -> dict:
    template_ct = (
        "NEW_DATE" if change_type == "NEW_DATE" else "UPDATED_SAME_DATE"
    )
    normalized = _normalize_entry_items(newest_entry["items"])
    promo_text = _email_promo_text()
    promo_logo = _email_promo_logo_url()
    promo_link = _first_promo_http_url(promo_text) if promo_text else None
    fb_base = _ad_feedback_public_url()
    return {
        "changeType": template_ct,
        "dateRaw": newest_entry["date_raw"],
        "items": normalized,
        "tldr": tldr_norm,
        "previewText": preview_text,
        "promoText": promo_text,
        "promoLogoUrl": promo_logo,
        "promoLinkHref": promo_link,
        "adFeedbackBaseUrl": fb_base,
    }


def _prepend_tldr_plain(plain_body: str, tldr: str) -> str:
    trimmed = tldr.strip()
    if not trimmed:
        return plain_body
    return f"TL;DR:\n{trimmed}\n\n{plain_body}"


def _prepend_tldr_html(html_body: str, tldr: str) -> str:
    trimmed = tldr.strip()
    if not trimmed:
        return html_body
    esc = _escape_html(trimmed).replace("\n", "<br>\n")
    card = (
        "<div style='background-color:#FFB8B8;border:1px solid #E85C5C;"
        "border-radius:8px;padding:16px 18px;margin:0 0 24px;"
        "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:21px;color:#1a1d24;'>"
        "<p style='margin:0 0 8px;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;"
        "color:#5a1f1f;font-weight:600;'>TL;DR</p>"
        f"<p style='margin:0;'>{esc}</p></div>"
    )
    return f"{card}{html_body}"


class NotifyError(Exception):
    """Raised when email sending fails."""


def _build_subject(change_type: str, newest_date: str) -> str:
    if change_type == "NEW_DATE":
        return f"CMSC216 News Feed update — {newest_date}"
    elif change_type == "UPDATED_SAME_DATE":
        return f"CMSC216 News Feed edited — {newest_date}"
    return f"CMSC216 News Feed — {newest_date}"


def _build_body(change_type: str, newest_entry: dict) -> str:
    date = newest_entry["date_raw"]
    items = _normalize_entry_items(newest_entry["items"])

    if change_type == "NEW_DATE":
        header = f"New announcement posted for {date}:"
    else:
        header = f"The announcement for {date} was edited. Current content:"

    bullet_list = "\n".join(
        f"  {i + 1}. {canonical_line_plain(line)}"
        for i, line in enumerate(items)
    )
    return f"{header}\n\n{bullet_list}\n"


def _build_html_body(change_type: str, newest_entry: dict) -> str:
    date = newest_entry["date_raw"]
    items = _normalize_entry_items(newest_entry["items"])

    if change_type == "NEW_DATE":
        header = f"New announcement posted for <strong>{date}</strong>:"
    else:
        header = f"The announcement for <strong>{date}</strong> was edited. Current content:"

    li_items = "\n".join(f"<li>{_segments_to_li_inner_html(line)}</li>" for line in items)
    promo = _legacy_promo_callout_html()
    feedback = _legacy_ad_feedback_html()
    return (
        f"<p>{header}</p><ol>{li_items}</ol>"
        f"{promo}{feedback}"
        f"<hr><p style='color:#888;font-size:12px;'>Sent by Madhav's Canvas News Feed Monitor</p>"
    )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _email_promo_text() -> str | None:
    t = (os.environ.get("EMAIL_PROMO_TEXT") or "").strip()
    return t if t else None


def _email_promo_logo_url() -> str | None:
    """Return promo logo image URL when ``EMAIL_PROMO_TEXT`` is set; else ``None``."""
    if not _email_promo_text():
        return None
    custom = (os.environ.get("EMAIL_PROMO_LOGO_URL") or "").strip()
    if custom:
        return custom
    return _DEFAULT_EMAIL_PROMO_LOGO_URL


def _first_promo_http_url(raw: str) -> str | None:
    m = _PROMO_HTTP_URL_RE.search(raw)
    if not m:
        return None
    u = m.group(1)
    return u if _legacy_link_href_ok(u) else None


def _append_promo_to_plain(body: str) -> str:
    promo = _email_promo_text()
    if not promo:
        return body
    return f"{body.rstrip()}\n\n---\n{promo}\n"


def _append_ad_feedback_plain(body: str) -> str:
    base = _ad_feedback_public_url()
    if not base:
        return body
    try:
        yes_u = vote_href(base, "yes")
        meh_u = vote_href(base, "meh")
        no_u = vote_href(base, "no")
    except ValueError:
        return body
    return (
        f"{body.rstrip()}\n\nWas this ad helpful?\n"
        f"Yes😃 — {yes_u}\nMeh😐 — {meh_u}\nNo😔 — {no_u}\n"
    )


def _linkify_promo_to_html(raw: str, cta_href: str | None = None) -> str:
    """Linkify http(s) spans. When *cta_href* is safe, every anchor uses it (single CTA).

    If *raw* lists several URLs, *cta_href* should be the first match from
    `_first_promo_http_url` so every visible link shares one destination.
    """
    parts: list[str] = []
    pos = 0
    unified = cta_href if cta_href and _legacy_link_href_ok(cta_href) else None
    for m in _PROMO_HTTP_URL_RE.finditer(raw):
        parts.append(_escape_html(raw[pos : m.start()]))
        url = m.group(1)
        if _legacy_link_href_ok(url):
            target = unified if unified is not None else url
            parts.append(
                f'<a href="{_escape_href_attr(target)}">{_escape_html(url)}</a>'
            )
        else:
            parts.append(_escape_html(url))
        pos = m.end()
    parts.append(_escape_html(raw[pos:]))
    return "".join(parts)


def _legacy_promo_callout_html() -> str:
    raw = _email_promo_text()
    if not raw:
        return ""
    promo_link = _first_promo_http_url(raw)
    inner = _linkify_promo_to_html(raw, promo_link)
    logo_url = _email_promo_logo_url()
    logo_cell = ""
    if logo_url:
        esc_src = _escape_href_attr(logo_url)
        img = (
            f"<img src='{esc_src}' alt='Thinkex' width='54' height='54' "
            "style='display:block;width:54px;height:54px;max-width:79px;"
            "max-height:54px;object-fit:contain;border:0;outline:none;'/>"
        )
        if promo_link:
            esc_href = _escape_href_attr(promo_link)
            wrap = (
                f"<a href='{esc_href}' style='text-decoration:none;"
                "border:none;line-height:0;display:block;'>" + img + "</a>"
            )
        else:
            wrap = img
        logo_cell = (
            "<td class='promo-logo-cell' valign='middle'"
            " style='padding:0 10px 0 0;width:89px;line-height:0;'>"
            f"{wrap}</td>"
        )
    inner_cell = (
        "<td valign='middle' style='padding:0;font-size:14px;line-height:21px;"
        "color:#2b303a;font-family:Arial,Helvetica,sans-serif;'>" + inner + "</td>"
    )
    table = (
        "<table role='presentation' cellpadding='0' cellspacing='0' border='0' "
        "width='100%' style='border-collapse:collapse;'><tr>"
        f"{logo_cell}{inner_cell}</tr></table>"
    )
    return (
        "<div class='promo-callout' style='border:1px solid #e2e6ef;border-radius:10px;"
        "padding:18px 18px;margin:20px 0 0;background-color:#f9fafb;font-family:Arial,"
        "Helvetica,sans-serif;'>"
        f"{table}</div>"
    )


def _legacy_ad_feedback_html() -> str:
    base = _ad_feedback_public_url()
    if not base:
        return ""
    pill = (
        "display:inline-block;margin:6px 8px 0 0;padding:8px 12px;"
        "font-size:14px;line-height:20px;font-weight:600;color:#1a1d24;"
        "background-color:rgba(255,255,255,0.45);border:1px solid #cf4a4a;"
        "border-radius:999px;text-decoration:none;font-family:Arial,Helvetica,sans-serif;"
    )
    try:
        hy = _escape_href_attr(vote_href(base, "yes"))
        hm = _escape_href_attr(vote_href(base, "meh"))
        hn = _escape_href_attr(vote_href(base, "no"))
    except ValueError:
        return ""
    buttons = "".join(
        [
            f"<a href='{hy}' style='{pill}'>Yes 😃</a>",
            f"<a href='{hm}' style='{pill}'>Meh 😐</a>",
            f"<a href='{hn}' style='{pill}'>No 😔</a>",
        ]
    )
    return (
        "<div class='ad-feedback-card' style='background-color:#FFB8B8;"
        "border:1px solid #E85C5C;border-radius:8px;padding:16px 18px;"
        "margin:12px 0 0;font-family:Arial,Helvetica,sans-serif;'>"
        "<p style='margin:0 0 10px;font-size:14px;line-height:21px;font-weight:600;color:#5a1f1f;'>"
        "Was this ad helpful?</p>"
        f"{buttons}</div>"
    )


def _trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _serialized_announcements_for_gemini(newest_entry: dict) -> str:
    items = _normalize_entry_items(newest_entry["items"])
    numbered = []
    for i, line in enumerate(items, 1):
        plain_line = canonical_line_plain(line)
        trimmed = _trim_text(plain_line, _GEMINI_MAX_ITEM_CHARS)
        numbered.append(f"{i}. {trimmed}")

    body = "\n".join(numbered)
    if len(body) > _GEMINI_MAX_PAYLOAD_CHARS:
        return _trim_text(body, _GEMINI_MAX_PAYLOAD_CHARS)
    return body


def _maybe_gemini_tldr(change_type: str, newest_entry: dict) -> str | None:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None

    model_id = (
        (os.environ.get("GEMINI_MODEL") or "").strip() or _DEFAULT_GEMINI_MODEL
    )

    payload = _serialized_announcements_for_gemini(newest_entry)
    user_block = (
        f"Feed change classification: {change_type}\n"
        f"Announcement date string: {newest_entry['date_raw']}\n"
        "Bullet/lines extracted from this date's news-feed block follow.\n---\n"
        f"{payload}"
    )

    try:
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=_GEMINI_HTTP_TIMEOUT_MS),
        )
        response = client.models.generate_content(
            model=model_id,
            contents=user_block,
            config=genai_types.GenerateContentConfig(
                system_instruction=_GEMINI_SYSTEM_INSTRUCTION,
                max_output_tokens=256,
                temperature=0.35,
            ),
        )
    except Exception as exc:
        logger.warning("Gemini TL;DR skipped: %s", exc)
        return None

    try:
        raw = response.text
    except ValueError:
        logger.warning(
            "Gemini TL;DR skipped: model returned empty or blocked text (finish reason)"
        )
        return None

    text = raw.strip()

    if not text:
        logger.warning("Gemini TL;DR skipped: empty response text")
        return None

    return text


def _prepend_tldr(plain_body: str, html_body: str, tldr: str) -> tuple[str, str]:
    return (
        _prepend_tldr_plain(plain_body, tldr),
        _prepend_tldr_html(html_body, tldr),
    )


def _post_sendgrid_mail(
    api_key: str,
    from_email: str,
    subject: str,
    plain: str,
    html: str,
    to_emails_batch: list[str],
) -> int:
    """POST one v3 mail/send payload. Each address gets its own personalization (private To)."""
    personalizations = [
        {"to": [{"email": e}], "subject": subject} for e in to_emails_batch
    ]
    payload = {
        "personalizations": personalizations,
        "from": {"email": from_email},
        "content": [
            {"type": "text/plain", "value": plain},
            {"type": "text/html", "value": html},
        ],
    }
    try:
        resp = requests.post(
            SENDGRID_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise NotifyError(f"SendGrid request failed: {exc}") from exc

    if resp.status_code not in (200, 201, 202):
        err_text = (resp.text or "")[:500]
        raise NotifyError(
            f"SendGrid returned HTTP {resp.status_code}: {err_text}"
        )
    return resp.status_code


def send_notification(
    change_type: str,
    newest_entry: dict,
    api_key: str | None = None,
    from_email: str | None = None,
    to_emails: list[str] | None = None,
    *,
    render_html_fn: Callable[[dict], str | None] | None = None,
) -> None:
    """Send an email via SendGrid's HTTP API.

    Environment: SENDGRID_API_KEY, SENDGRID_FROM_EMAIL, NOTIFY_EMAILS
    (comma-separated recipients).     Optionally GEMINI_API_KEY (see .env.example) and
    GEMINI_MODEL prepend a Gemini TL;DR; if Gemini is unavailable, the email sends
    without it. Optional EMAIL_PROMO_TEXT adds a promo callout after the bulletin in
    both MIME parts (http/https snippets are linked; otherwise text is escaped).
    When ``AD_FEEDBACK_PUBLIC_URL`` is also set (https recommended), emails include a
    “Was this ad helpful?” strip with tap targets; clicks are recorded by
    ``python main.py ad-feedback-serve`` in SQLite (``AD_FEEDBACK_DB_PATH``).

    HTML body is rendered with React Email (``email-render/``) when the CLI is
    available. Rendered HTML is normalized (leading non-HTML noise stripped) and
    validated (date and first bulletin line must appear); failing that, the
    legacy HTML template is used so clients still receive a readable body.

    Set ``SKIP_REACT_EMAIL_HTML=1`` to force legacy HTML, or pass
    ``render_html_fn`` for tests / custom rendering.
    """
    api_key = api_key or os.environ.get("SENDGRID_API_KEY")
    from_email = from_email or os.environ.get("SENDGRID_FROM_EMAIL")
    to_emails_str = os.environ.get("NOTIFY_EMAILS", "")
    to_emails = to_emails or [e.strip() for e in to_emails_str.split(",") if e.strip()]

    if not api_key:
        raise NotifyError(
            "No SendGrid API key. Set SENDGRID_API_KEY in your .env file."
        )
    if not from_email:
        raise NotifyError(
            "No sender email. Set SENDGRID_FROM_EMAIL in your .env file."
        )
    if not to_emails:
        raise NotifyError(
            "No recipient emails. Set NOTIFY_EMAILS in your .env file "
            "(comma-separated list)."
        )

    subject = _build_subject(change_type, newest_entry["date_raw"])
    send_entry = dict(newest_entry)
    send_entry["items"] = _normalize_entry_items(send_entry["items"])

    plain = _build_body(change_type, send_entry)
    tldr_raw = _maybe_gemini_tldr(change_type, newest_entry)
    if tldr_raw:
        plain = _prepend_tldr_plain(plain, tldr_raw)
    plain = _append_promo_to_plain(plain)
    plain = _append_ad_feedback_plain(plain)

    tldr_norm = normalize_tldr(tldr_raw)
    preview = _preview_text(
        subject, newest_entry["date_raw"], tldr_norm, tldr_raw
    )
    props = _react_email_props(change_type, send_entry, tldr_norm, preview)
    html_raw = _render_news_email_html(props, render_html_fn)
    html = _ensure_sendgrid_html(
        html_raw, change_type, send_entry, tldr_raw, plain
    )

    logger.info(
        "Email body sizes: text/plain=%d chars, text/html=%d chars",
        len(plain),
        len(html),
    )

    limit = SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST
    batches = [to_emails[i : i + limit] for i in range(0, len(to_emails), limit)]
    last_status: int | None = None
    for batch in batches:
        last_status = _post_sendgrid_mail(
            api_key, from_email, subject, plain, html, batch
        )

    logger.info(
        "Email sent to %d recipient(s) in %d SendGrid request(s) (HTTP %s): %s",
        len(to_emails),
        len(batches),
        last_status,
        subject,
    )
