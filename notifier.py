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
from pathlib import Path
from typing import Any

import requests
from parser import ItemSegment, canonical_line_plain
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


def _render_news_email_html_subprocess(props: dict) -> str | None:
    root = Path(__file__).resolve().parent
    email_dir = root / "email-render"
    tsx = _tsx_executable(email_dir)
    if tsx is None:
        logger.debug(
            "React Email skipped: tsx not found under %s (run npm ci in email-render/)",
            email_dir,
        )
        return None

    cli = email_dir / "src" / "render-cli.tsx"
    cmd = [str(tsx), str(cli)]
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(props, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=_REACT_EMAIL_RENDER_TIMEOUT,
            cwd=str(email_dir),
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

    html = (proc.stdout or "").strip()
    if not html:
        logger.warning("React Email returned empty HTML; using legacy HTML")
        return None
    return html


def _render_news_email_html(
    props: dict,
    render_html_fn: Callable[[dict], str | None] | None,
) -> str | None:
    if os.environ.get("SKIP_REACT_EMAIL_HTML", "").strip() == "1":
        return None
    if render_html_fn is not None:
        try:
            out = render_html_fn(props)
            if out and out.strip():
                return out.strip()
            logger.warning(
                "React Email injectable renderer returned empty output; using legacy HTML"
            )
            return None
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
    return {
        "changeType": template_ct,
        "dateRaw": newest_entry["date_raw"],
        "items": normalized,
        "tldr": tldr_norm,
        "previewText": preview_text,
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
    return f"<p><strong>TL;DR:</strong><br>{esc}</p>{html_body}"


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
    return (
        f"<p>{header}</p><ol>{li_items}</ol>"
        f"<hr><p style='color:#888;font-size:12px;'>Sent by Madhav's Canvas News Feed Monitor</p>"
    )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
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
    (comma-separated recipients). Optionally GEMINI_API_KEY (see .env.example) and
    GEMINI_MODEL prepend a Gemini TL;DR; if Gemini is unavailable, the email sends
    without it.

    HTML body is rendered with React Email (``email-render/``) when the CLI is
    available. Set ``SKIP_REACT_EMAIL_HTML=1`` to force legacy HTML, or pass
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

    tldr_norm = normalize_tldr(tldr_raw)
    preview = _preview_text(
        subject, newest_entry["date_raw"], tldr_norm, tldr_raw
    )
    props = _react_email_props(change_type, send_entry, tldr_norm, preview)
    html = _render_news_email_html(props, render_html_fn)
    if html is None:
        html = _build_html_body(change_type, send_entry)
        if tldr_raw:
            html = _prepend_tldr_html(html, tldr_raw)

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
