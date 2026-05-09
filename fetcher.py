"""
fetcher.py — Transport layer for retrieving the Canvas course page HTML.

Supports:
  - Loading from a local file (for development/testing).
  - Fetching live from the Canvas API using browser session cookies.

Canvas renders wiki page content with JavaScript, so a plain HTTP GET of the
course URL returns an empty shell. Instead, we hit the Canvas REST API
endpoint for the front page, which returns JSON with a `body` field containing
the actual page HTML. This is the same HTML the browser injects into the page.
"""

import json
import logging
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Retries for API GET when DNS/connectivity blips (single poll cycle; hourly loop unchanged).
_FETCH_TRANSIENT_ATTEMPTS = 8
_FETCH_BACKOFF_INITIAL_S = 1.0
_FETCH_BACKOFF_MAX_S = 60.0

# In-process cookie cache for long-running `poll` only (see `fetch_from_url(..., use_cookie_cache=True)`).
_poll_cookie_cache: dict[tuple[str, str], dict[str, str]] = {}

_COMET_COOKIE_DB = Path.home() / "Library/Application Support/Comet/Default/Cookies"
_COMET_KEYCHAIN_SERVICE = "Comet Safe Storage"
_COMET_KEYCHAIN_ACCOUNT = "Comet"


def fetch_from_file(file_path: str) -> str:
    """Load HTML from a local file. Primary method during development."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"HTML file not found: {file_path}")

    start = time.monotonic()
    html = path.read_text(encoding="utf-8")
    elapsed_ms = (time.monotonic() - start) * 1000

    logger.info(
        "Fetched HTML from file %s (%d bytes, %.1f ms)",
        file_path,
        len(html.encode("utf-8")),
        elapsed_ms,
    )
    return html


class FetchError(Exception):
    """Raised when live page fetching fails."""


class _AuthFailure(FetchError):
    """Session/auth failure; `fetch_from_url` may retry once with refreshed cookies when caching."""


def _extract_course_api_url(course_url: str) -> str:
    """Convert a Canvas course homepage URL into its front-page API endpoint.

    e.g. 'https://umd.instructure.com/courses/1398395'
      -> 'https://umd.instructure.com/api/v1/courses/1398395/front_page'
    """
    m = re.match(r"(https?://[^/]+)/courses/(\d+)", course_url.rstrip("/"))
    if not m:
        raise FetchError(
            f"Cannot parse Canvas course URL: {course_url!r}. "
            "Expected format: https://<domain>/courses/<id>"
        )
    base, course_id = m.group(1), m.group(2)
    return f"{base}/api/v1/courses/{course_id}/front_page"


def _get_comet_keychain_password() -> bytes:
    """Get Comet's cookie encryption password from the macOS Keychain.

    On first run, macOS will show a dialog asking you to allow access.
    Click 'Always Allow' so future runs don't prompt.
    """
    cmd = [
        "/usr/bin/security", "-q", "find-generic-password",
        "-w", "-a", _COMET_KEYCHAIN_ACCOUNT, "-s", _COMET_KEYCHAIN_SERVICE,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise FetchError(
            "Keychain access timed out. macOS is likely showing a permission "
            "dialog that you need to approve. Run this command from your "
            "terminal (not from an IDE), and click 'Always Allow' when the "
            "keychain dialog appears."
        )

    if proc.returncode != 0:
        raise FetchError(
            f"Failed to read Comet keychain password (exit {proc.returncode}). "
            f"stderr: {proc.stderr.decode(errors='replace').strip()}"
        )

    return proc.stdout.strip()


def _derive_chromium_key(password: bytes) -> bytes:
    """Derive the AES-128 key from a Chromium keychain password (macOS)."""
    kdf = PBKDF2HMAC(algorithm=SHA1(), iterations=1003, length=16, salt=b"saltysalt")
    return kdf.derive(password)


def _load_comet_cookies(domain: str) -> dict[str, str]:
    """Read and decrypt Canvas cookies from Comet's cookie database.

    Uses pycookiecheat's chrome_decrypt for proven decryption logic,
    but handles the Comet keychain lookup ourselves since pycookiecheat
    only knows about Chrome/Brave/Chromium.
    """
    from pycookiecheat.chrome import chrome_decrypt, generate_host_keys

    if not _COMET_COOKIE_DB.exists():
        raise FetchError(
            f"Comet cookie database not found at {_COMET_COOKIE_DB}. "
            "Is Comet installed?"
        )

    password = _get_comet_keychain_password()
    enc_key = _derive_chromium_key(password)
    init_vector = b" " * 16

    try:
        conn = sqlite3.connect(
            f"file:{_COMET_COOKIE_DB.expanduser()}?mode=ro", uri=True
        )
    except sqlite3.OperationalError as exc:
        raise FetchError(
            f"Cannot open Comet cookie database: {exc}. "
            "Make sure Comet is not open, or try again."
        ) from exc

    conn.row_factory = sqlite3.Row
    conn.text_factory = bytes

    cookie_db_version = 0
    try:
        row = conn.execute("select value from meta where key = 'version'").fetchone()
        if row:
            cookie_db_version = int(row[0])
    except sqlite3.OperationalError:
        pass

    sql = (
        "select name, value, encrypted_value "
        "from cookies where host_key like ?"
    )

    cookies: dict[str, str] = {}
    for host_key in generate_host_keys(domain):
        for db_row in conn.execute(sql, (host_key,)):
            row = dict(db_row)
            name = row["name"]
            if isinstance(name, bytes):
                name = name.decode("utf-8")

            value = row["value"]
            enc_value = row["encrypted_value"]

            if not value and enc_value[:3] in (b"v10", b"v11"):
                value = chrome_decrypt(
                    enc_value,
                    key=enc_key,
                    init_vector=init_vector,
                    cookie_database_version=cookie_db_version,
                )

            if isinstance(value, bytes):
                value = value.decode("utf-8")

            if value:
                cookies[name] = value

    conn.close()

    if not cookies:
        raise FetchError(
            f"No cookies found for {domain} in Comet. "
            "Are you logged into Canvas in Comet?"
        )

    logger.info("Loaded %d cookies for %s from Comet", len(cookies), domain)
    return cookies


def _load_browser_cookies(url: str, browser: str) -> dict[str, str]:
    """Read cookies for the given URL from the specified browser."""
    browser_lower = browser.lower()

    if browser_lower == "comet":
        from pycookiecheat.chrome import get_domain
        domain = get_domain(url)
        return _load_comet_cookies(domain)

    try:
        from pycookiecheat import BrowserType, chrome_cookies
    except ImportError:
        raise FetchError(
            "pycookiecheat is not installed. Run: pip install pycookiecheat"
        )

    browser_map = {
        "chrome": BrowserType.CHROME,
        "brave": BrowserType.BRAVE,
        "chromium": BrowserType.CHROMIUM,
        "firefox": BrowserType.FIREFOX,
    }

    bt = browser_map.get(browser_lower)
    if bt is None:
        raise FetchError(
            f"Unsupported browser: {browser!r}. "
            f"Supported: {', '.join(list(browser_map.keys()) + ['comet'])}"
        )

    try:
        cookies = chrome_cookies(url, browser=bt)
    except Exception as exc:
        raise FetchError(
            f"Failed to read cookies from {browser}. "
            f"Make sure you're logged into Canvas in {browser}. "
            f"Error: {exc}"
        ) from exc

    if not cookies:
        raise FetchError(
            f"No cookies found for {url} in {browser}. "
            "Are you logged into Canvas in that browser?"
        )

    logger.info("Loaded %d cookies from %s", len(cookies), browser)
    return cookies


def _fetch_front_page_with_cookies(
    api_url: str,
    course_url: str,
    browser: str,
    cookies: dict[str, str],
) -> str:
    """GET front_page JSON and return the `body` HTML. Raises _AuthFailure on session issues."""
    resp = None
    for transient_try in range(_FETCH_TRANSIENT_ATTEMPTS):
        start = time.monotonic()
        try:
            resp = requests.get(
                api_url,
                cookies=cookies,
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            n = transient_try + 1
            if n < _FETCH_TRANSIENT_ATTEMPTS:
                delay = min(
                    _FETCH_BACKOFF_INITIAL_S * (2**transient_try),
                    _FETCH_BACKOFF_MAX_S,
                )
                logger.warning(
                    "API GET failed (attempt %d/%d, transient %s): %s; retrying in %.1fs",
                    n,
                    _FETCH_TRANSIENT_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.warning(
                "API GET gave up after %d attempts (transient %s — will retry on next poll): %s",
                _FETCH_TRANSIENT_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
            raise FetchError(
                f"Network error after {_FETCH_TRANSIENT_ATTEMPTS} attempts "
                f"(DNS/connectivity). Last error: {exc}"
            ) from exc
        elapsed_ms = (time.monotonic() - start) * 1000
        break

    assert resp is not None

    if resp.status_code in (401, 403):
        raise _AuthFailure(
            "Canvas returned 401/403 — your session cookies have likely expired. "
            f"Log into Canvas in {browser} and try again."
        )

    if resp.status_code != 200:
        raise FetchError(
            f"Canvas API returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    if "text/html" in resp.headers.get("Content-Type", ""):
        raise _AuthFailure(
            "Canvas returned HTML instead of JSON — this usually means your "
            f"session has expired and Canvas is redirecting to the login page. "
            f"Log into Canvas in {browser} and try again."
        )

    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise FetchError(
            "Canvas API response was not valid JSON. "
            f"First 200 chars: {resp.text[:200]}"
        )

    body = data.get("body")
    if not body:
        raise FetchError(
            "Canvas API response has no 'body' field or it is empty. "
            f"Keys in response: {list(data.keys())}"
        )

    logger.info(
        "Fetched front page via API (%d bytes, %.1f ms)",
        len(body.encode("utf-8")),
        elapsed_ms,
    )
    return body


def fetch_from_url(
    course_url: str,
    browser: str = "comet",
    *,
    use_cookie_cache: bool = False,
) -> str:
    """Fetch the Canvas course front page HTML via the REST API.

    Reads session cookies from the specified browser, calls the Canvas
    front_page API endpoint, and returns the page body HTML.

    When ``use_cookie_cache`` is True (used by ``poll`` mode), cookies are
    loaded once per process and reused until a request indicates an expired
    session; the cache is then invalidated and cookies are re-read from disk.
    """
    api_url = _extract_course_api_url(course_url)
    cache_key = (course_url, browser.lower())

    logger.info("Fetching front page from API: %s", api_url)

    if use_cookie_cache and cache_key in _poll_cookie_cache:
        cookies = _poll_cookie_cache[cache_key]
    else:
        cookies = _load_browser_cookies(course_url, browser)
        if use_cookie_cache:
            _poll_cookie_cache[cache_key] = cookies

    for attempt in range(2):
        try:
            return _fetch_front_page_with_cookies(
                api_url, course_url, browser, cookies
            )
        except _AuthFailure:
            if use_cookie_cache and attempt == 0:
                _poll_cookie_cache.pop(cache_key, None)
                cookies = _load_browser_cookies(course_url, browser)
                _poll_cookie_cache[cache_key] = cookies
                continue
            raise
