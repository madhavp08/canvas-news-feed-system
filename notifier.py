"""
notifier.py — Email notification layer using the SendGrid Web API (HTTP, no extra crypto deps).
"""

from __future__ import annotations

import logging
import os

import requests
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
    items = newest_entry["items"]

    if change_type == "NEW_DATE":
        header = f"New announcement posted for {date}:"
    else:
        header = f"The announcement for {date} was edited. Current content:"

    bullet_list = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(items))
    return f"{header}\n\n{bullet_list}\n"


def _build_html_body(change_type: str, newest_entry: dict) -> str:
    date = newest_entry["date_raw"]
    items = newest_entry["items"]

    if change_type == "NEW_DATE":
        header = f"New announcement posted for <strong>{date}</strong>:"
    else:
        header = f"The announcement for <strong>{date}</strong> was edited. Current content:"

    li_items = "\n".join(f"<li>{_escape_html(item)}</li>" for item in items)
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
    numbered = []
    for i, item in enumerate(newest_entry["items"], 1):
        trimmed = _trim_text(item, _GEMINI_MAX_ITEM_CHARS)
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
    trimmed = tldr.strip()
    if not trimmed:
        return plain_body, html_body

    plain_with = f"TL;DR:\n{trimmed}\n\n{plain_body}"
    esc = _escape_html(trimmed).replace("\n", "<br>\n")
    html_with = (
        f"<p><strong>TL;DR:</strong><br>{esc}</p>"
        f"{html_body}"
    )
    return plain_with, html_with


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
) -> None:
    """Send an email via SendGrid's HTTP API.

    Environment: SENDGRID_API_KEY, SENDGRID_FROM_EMAIL, NOTIFY_EMAILS
    (comma-separated recipients). Optionally GEMINI_API_KEY (see .env.example) and
    GEMINI_MODEL prepend a Gemini TL;DR; if Gemini is unavailable, the email sends
    without it.
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
    plain = _build_body(change_type, newest_entry)
    html = _build_html_body(change_type, newest_entry)

    tldr = _maybe_gemini_tldr(change_type, newest_entry)
    if tldr:
        plain, html = _prepend_tldr(plain, html, tldr)

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
