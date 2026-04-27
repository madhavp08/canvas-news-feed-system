"""
main.py — CLI entry point for the Canvas News Feed monitor.

Modes:
    check   Run one check cycle (default).
    poll    Run continuously, checking every --interval seconds.

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
from notifier import NotifyError, send_notification
from parser import ParseError, parse_news_feed
from state_store import load_state, save_state

logger = logging.getLogger("canvas_monitor")

_NOTIFY_CHANGE_TYPES = {"NEW_DATE", "UPDATED_SAME_DATE"}


def _load_env_file(path: str = ".env") -> None:
    """Load key=value pairs from a .env file into os.environ.
    Skips blank lines, comments, and missing files."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def check_once(
    state_file: str,
    html_file: str | None = None,
    url: str | None = None,
    browser: str = "comet",
    notify: bool = True,
    *,
    use_cookie_cache: bool = False,
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

    if change_type != "NO_CHANGE":
        save_state(state_file, newest, change_type)

    if notify and change_type in _NOTIFY_CHANGE_TYPES:
        try:
            send_notification(change_type, newest)
            logger.info("Notification sent for %s", change_type)
        except NotifyError as exc:
            logger.error("Failed to send notification: %s", exc)

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
) -> None:
    """Run check_once repeatedly with a sleep interval. Resilient to transient errors."""
    logger.info(
        "Starting poll loop: interval=%ds, url=%s, browser=%s",
        interval, url, browser,
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
            )
        except (FetchError, ParseError) as exc:
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
    # Allow legacy invocations: `python main.py --url ...` without the `check` subcommand
    if len(sys.argv) > 1 and sys.argv[1] not in (
        "check", "poll", "help", "-h", "--help"
    ):
        sys.argv.insert(1, "check")

    _load_env_file()

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
    poll_p.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()

    if args.command is None:
        ap.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "check":
        try:
            summary = check_once(
                state_file=args.state_file,
                html_file=args.html_file,
                url=args.url,
                browser=args.browser,
                notify=not args.no_notify,
            )
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
        poll_loop(
            url=args.url,
            state_file=args.state_file,
            browser=args.browser,
            interval=args.interval,
            notify=not args.no_notify,
        )


if __name__ == "__main__":
    main()
