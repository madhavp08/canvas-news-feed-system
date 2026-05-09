"""
main.py — CLI entry point for the Canvas News Feed monitor.

Modes:
    check            Run one check cycle (default).
    poll             Run continuously, checking every --interval seconds.
    send-from-state  Send one email from persisted state (manual resend).

Usage:
    python main.py check --url https://umd.instructure.com/courses/1398395
    python main.py check --html-file page.html
    python main.py poll --url https://umd.instructure.com/courses/1398395
    python main.py poll  (reads CANVAS_COURSE_URL from .env)
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from detector import detect_change
from fetcher import FetchError, fetch_from_file, fetch_from_url
from notifier import NotifyError, react_email_subprocess_available, send_notification
from parser import ParseError, parse_news_feed
from state_store import load_state, save_state

logger = logging.getLogger("canvas_monitor")

_NOTIFY_CHANGE_TYPES = {"NEW_DATE", "UPDATED_SAME_DATE"}

# Keys for which `.env` wins over pre-exported shell variables (API secrets, config).
_DOTENV_OVERRIDE_KEYS = frozenset(
    {
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "SENDGRID_API_KEY",
        "SENDGRID_FROM_EMAIL",
        "NOTIFY_EMAILS",
        "CANVAS_COURSE_URL",
        "BROWSER",
        "POLL_INTERVAL",
        "SKIP_REACT_EMAIL_HTML",
        "EMAIL_PROMO_TEXT",
        "EMAIL_PROMO_LOGO_URL",
        "AD_FEEDBACK_PUBLIC_URL",
        "AD_FEEDBACK_DB_PATH",
    }
)

_STATE_ENTRY_KEYS = (
    "latest_date_raw",
    "latest_date_normalized",
    "latest_items",
    "latest_content_text",
    "latest_content_hash",
)


def _newest_entry_from_saved_state(state: dict) -> dict:
    """Map ``state.json`` fields to the newest-entry dict ``send_notification`` expects."""
    missing = [k for k in _STATE_ENTRY_KEYS if k not in state]
    if missing:
        raise ValueError(
            f"State file is missing required field(s): {', '.join(missing)}."
        )

    return {
        "date_raw": state["latest_date_raw"],
        "date_normalized": state["latest_date_normalized"],
        "items": state["latest_items"],
        "content_text": state["latest_content_text"],
        "content_hash": state["latest_content_hash"],
    }


def notify_from_saved_state(
    state_file: str,
    change_type_override: str | None = None,
    *,
    notify_emails: list[str] | None = None,
) -> str:
    """Load ``state_file``, rebuild the newest entry, send one notification.

    Returns the change_type used.

    Raises:
        ``ValueError`` if state is missing, incomplete, or has no usable
        ``last_change_type`` when *change_type_override* is omitted.
    """
    p = Path(state_file)
    if not p.exists():
        raise ValueError(f"State file not found: {state_file}")

    state = load_state(state_file)
    if state is None:
        raise ValueError(f"No state loaded from {state_file!r}")

    entry = _newest_entry_from_saved_state(state)

    if change_type_override is not None:
        ct = change_type_override
    else:
        ct = state.get("last_change_type")

    if ct not in _NOTIFY_CHANGE_TYPES:
        raise ValueError(
            "State file provides no usable last_change_type for email "
            f"({ct!r}). Use --change-type NEW_DATE or UPDATED_SAME_DATE."
        )

    send_notification(ct, entry, to_emails=notify_emails)
    logger.info("Manual send-from-state succeeded for change_type=%s", ct)
    return ct


def _coerce_notify_email_override(raw: list[str] | None) -> list[str] | None:
    """Validate ``--notify-email`` values: non-empty after strip, dedupe, preserve order.

    Returns ``None`` when *raw* is ``None`` (flag not used). Raises ``ValueError``
    when any entry is blank after stripping.
    """
    if raw is None:
        return None
    cleaned: list[str] = []
    for i, addr in enumerate(raw):
        s = addr.strip()
        if not s:
            raise ValueError(
                f"--notify-email value #{i + 1} is empty or whitespace only"
            )
        cleaned.append(s)
    return list(dict.fromkeys(cleaned))


def _load_env_file(path: str = ".env") -> list[str]:
    """Load key=value pairs from a .env file into os.environ.

    For keys in ``_DOTENV_OVERRIDE_KEYS``, the file value always wins over any
    value already in the process environment. Other keys use ``setdefault``
    (shell/export wins if already set).

    Returns a list of override keys that appeared in the file (may contain
    duplicates if the file repeats a key; log callers may dedupe).
    """
    p = Path(path)
    if not p.exists():
        return []
    overridden: list[str] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k = key.strip()
            v = value.strip()
            if k in _DOTENV_OVERRIDE_KEYS:
                os.environ[k] = v
                overridden.append(k)
            else:
                os.environ.setdefault(k, v)
    return overridden


def check_once(
    state_file: str,
    html_file: str | None = None,
    url: str | None = None,
    browser: str = "comet",
    notify: bool = True,
    *,
    use_cookie_cache: bool = False,
    notify_emails: list[str] | None = None,
) -> dict:
    """Run one fetch -> parse -> detect -> persist -> notify cycle."""
    if html_file:
        html = fetch_from_file(html_file)
    elif url:
        html = fetch_from_url(
            url, browser=browser, use_cookie_cache=use_cookie_cache
        )
    else:
        raise ValueError("Either --html-file or --url must be provided.")

    entries = parse_news_feed(html)

    newest = entries[0]
    old_state = load_state(state_file)
    change_type = detect_change(newest, old_state)

    if change_type == "NO_CHANGE":
        pass
    elif notify and change_type in _NOTIFY_CHANGE_TYPES:
        try:
            send_notification(change_type, newest, to_emails=notify_emails)
            logger.info("Notification sent for %s", change_type)
            save_state(state_file, newest, change_type)
        except NotifyError as exc:
            logger.error("Failed to send notification: %s", exc)
    else:
        save_state(state_file, newest, change_type)

    summary = {
        "change_type": change_type,
        "newest_date": newest["date_raw"],
        "newest_items_count": len(newest["items"]),
        "total_entries_parsed": len(entries),
    }

    logger.info("Check result: %s", json.dumps(summary, indent=2))
    return summary


def poll_loop(
    url: str,
    state_file: str,
    browser: str,
    interval: int,
    notify: bool,
    *,
    notify_emails: list[str] | None = None,
) -> None:
    """Run check_once repeatedly with a sleep interval. Resilient to transient errors."""
    logger.info(
        "Starting poll loop: interval=%ds, url=%s, browser=%s",
        interval, url, browser,
    )
    if os.environ.get("SKIP_REACT_EMAIL_HTML", "").strip() == "1":
        logger.warning(
            "SKIP_REACT_EMAIL_HTML=1 — emails use legacy HTML only (unset for React Email)"
        )
    elif react_email_subprocess_available():
        logger.info(
            "React Email CLI available — notifications will use the styled template "
            "when validation succeeds (see per-send logs)"
        )
    else:
        logger.warning(
            "React Email CLI missing — run: cd email-render && npm ci "
            "(notifications fall back to simple HTML)"
        )

    shutdown = False

    def _handle_signal(signum, frame):
        nonlocal shutdown
        logger.info("Received signal %d, shutting down after current cycle...", signum)
        shutdown = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not shutdown:
        try:
            check_once(
                state_file=state_file,
                url=url,
                browser=browser,
                notify=notify,
                use_cookie_cache=True,
                notify_emails=notify_emails,
            )
        except FetchError as exc:
            msg = str(exc)
            if msg.startswith("Network error after ") and "(DNS/connectivity)" in msg:
                logger.warning("Check skipped — %s (will retry next cycle)", msg)
            else:
                logger.error("Check failed (will retry next cycle): %s", exc)
        except ParseError as exc:
            logger.error("Check failed (will retry next cycle): %s", exc)
        except Exception:
            logger.exception("Unexpected error (will retry next cycle)")

        if shutdown:
            break

        logger.info("Sleeping %d seconds until next check...", interval)
        wake_time = time.monotonic() + interval
        while time.monotonic() < wake_time and not shutdown:
            remaining = wake_time - time.monotonic()
            time.sleep(max(0, min(1, remaining)))

    logger.info("Poll loop stopped.")


def main() -> None:
    if len(sys.argv) > 1:
        fb_cmd = sys.argv[1]
        if fb_cmd == "ad-feedback-serve":
            _load_env_file()
            from ad_feedback_cli import main_serve

            sys.exit(main_serve(sys.argv[2:]))
        if fb_cmd == "ad-feedback-stats":
            _load_env_file()
            from ad_feedback_cli import main_stats

            sys.exit(main_stats())
        if fb_cmd == "ad-feedback-chart":
            _load_env_file()
            from ad_feedback_cli import main_chart

            sys.exit(main_chart(sys.argv[2:]))

    # Allow legacy invocations: `python main.py --url ...` without the `check` subcommand
    if len(sys.argv) > 1 and sys.argv[1] not in (
        "check",
        "poll",
        "send-from-state",
        "help",
        "-h",
        "--help",
    ):
        sys.argv.insert(1, "check")

    dotenv_overrides = _load_env_file()

    ap = argparse.ArgumentParser(
        description="Canvas News Feed monitor — detect and notify on announcements."
    )
    sub = ap.add_subparsers(dest="command")

    # --- check subcommand ---
    check_p = sub.add_parser("check", help="Run one check cycle.")
    check_source = check_p.add_mutually_exclusive_group(required=True)
    check_source.add_argument("--html-file")
    check_source.add_argument("--url")
    check_p.add_argument("--browser", default=os.environ.get("BROWSER", "comet"),
                         choices=["chrome", "firefox", "brave", "chromium", "comet"])
    check_p.add_argument("--state-file", default="state.json")
    check_p.add_argument("--no-notify", action="store_true",
                         help="Skip email notification even on change.")
    check_p.add_argument(
        "--notify-email",
        action="append",
        dest="notify_emails",
        metavar="EMAIL",
        default=None,
        help="Override NOTIFY_EMAILS for this run only; repeat per address. "
        "Ignored when --no-notify is set (no mail is sent).",
    )
    check_p.add_argument("-v", "--verbose", action="store_true")

    # --- poll subcommand ---
    poll_p = sub.add_parser("poll", help="Run continuously on a schedule.")
    poll_p.add_argument("--url", default=os.environ.get("CANVAS_COURSE_URL"))
    poll_p.add_argument("--browser", default=os.environ.get("BROWSER", "comet"),
                        choices=["chrome", "firefox", "brave", "chromium", "comet"])
    poll_p.add_argument("--state-file", default="state.json")
    poll_p.add_argument("--interval", type=int,
                        default=int(os.environ.get("POLL_INTERVAL", "3600")),
                        help="Seconds between checks (default: 3600).")
    poll_p.add_argument("--no-notify", action="store_true")
    poll_p.add_argument(
        "--notify-email",
        action="append",
        dest="notify_emails",
        metavar="EMAIL",
        default=None,
        help="Override NOTIFY_EMAILS for this run only; repeat per address. "
        "Ignored when --no-notify is set (no mail is sent).",
    )
    poll_p.add_argument("-v", "--verbose", action="store_true")

    send_p = sub.add_parser(
        "send-from-state",
        help=(
            "Send one notification from persisted state (same pipeline as poll/check)."
        ),
    )
    send_p.add_argument("--state-file", default="state.json")
    send_p.add_argument(
        "--change-type",
        choices=sorted(_NOTIFY_CHANGE_TYPES),
        default=None,
        help=(
            "Defaults to last_change_type in state when NEW_DATE or UPDATED_SAME_DATE."
        ),
    )
    send_p.add_argument(
        "--notify-email",
        action="append",
        dest="notify_emails",
        metavar="EMAIL",
        default=None,
        help="Override NOTIFY_EMAILS for this send only; repeat per address.",
    )
    send_p.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()

    if args.command is None:
        ap.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if dotenv_overrides:
        logger.info(
            ".env overrides shell for: %s",
            ", ".join(sorted(frozenset(dotenv_overrides))),
        )

    if args.command == "check":
        try:
            try:
                notify_override = _coerce_notify_email_override(
                    args.notify_emails
                )
            except ValueError as exc:
                logger.error("%s", exc)
                sys.exit(2)
            summary = check_once(
                state_file=args.state_file,
                html_file=args.html_file,
                url=args.url,
                browser=args.browser,
                notify=not args.no_notify,
                notify_emails=notify_override,
            )
        except FetchError as exc:
            logger.error("Monitor check failed: %s", exc)
            sys.exit(1)
        except Exception:
            logger.exception("Monitor check failed")
            sys.exit(1)
        print(json.dumps(summary, indent=2))

    elif args.command == "poll":
        if not args.url:
            logger.error(
                "No URL provided. Use --url or set CANVAS_COURSE_URL in .env"
            )
            sys.exit(1)
        try:
            notify_override = _coerce_notify_email_override(args.notify_emails)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)
        poll_loop(
            url=args.url,
            state_file=args.state_file,
            browser=args.browser,
            interval=args.interval,
            notify=not args.no_notify,
            notify_emails=notify_override,
        )

    elif args.command == "send-from-state":
        try:
            notify_override = _coerce_notify_email_override(args.notify_emails)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)
        try:
            ct = notify_from_saved_state(
                args.state_file,
                change_type_override=args.change_type,
                notify_emails=notify_override,
            )
        except NotifyError as exc:
            logger.error("Notification failed: %s", exc)
            sys.exit(1)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)
        print(json.dumps({"change_type": ct, "state_file": args.state_file}, indent=2))


if __name__ == "__main__":
    main()
