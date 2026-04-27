"""
notifier.py — Email notification layer using the SendGrid Web API (HTTP, no extra crypto deps).
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
# https://docs.sendgrid.com/api-reference/mail-send/mail-send — max personalizations per request
SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST = 1000


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
    (comma-separated recipients).
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
