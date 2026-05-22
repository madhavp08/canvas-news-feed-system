"""CLI helpers: TCP server that records votes, and ASCII bar-chart stats."""

from __future__ import annotations

import argparse
from html import escape
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ad_feedback_store import VALID_CHOICES, db_path, record_vote, vote_counts


def _parsed_request_path(raw_path: str) -> tuple[str, dict[str, list[str]]]:
    """Path + query dict from HTTP request-target (may include fragment in edge cases)."""
    u = urlparse("http://_" + raw_path)
    return u.path or "/", parse_qs(u.query)


_LABELS = {
    "yes": "Yes 😃",
    "meh": "Meh 😐",
    "no": "No 😔",
}


def render_bar_chart(width: int = 36) -> str:
    counts = vote_counts()
    total = sum(counts.values())
    lines: list[str] = []
    title = "Was this ad helpful?"
    lines.append(title)
    if total == 0:
        lines.append("  (no votes yet)")
        return "\n".join(lines)

    lines.append(f"  Total votes: {total}")
    ordered = ["yes", "meh", "no"]
    max_n = max(counts[c] for c in ordered) or 1
    bar_w = max(1, width)
    for c in ordered:
        n = counts[c]
        label = _LABELS[c]
        frac = n / total if total else 0
        filled = int(round(bar_w * n / max_n)) if max_n else 0
        bar = "█" * filled + "░" * (bar_w - filled)
        pct = 100.0 * frac
        lines.append(f"  {label:8} │{bar}│ {n:4} ({pct:4.1f}%)")
    return "\n".join(lines)


_COLORS_HTML = {"yes": "#1b5e20", "meh": "#f9a825", "no": "#b71c1c"}


def render_bar_chart_html(*, include_counts: bool = False) -> str:
    """Standalone HTML from SQLite summaries.

    By default **no totals** (privacy-first if someone opens or shares the file).
    Pass ``include_counts=True`` for the proportional bar breakdown (operators only ---
    do not publish). ``/vote`` and ``/thanks`` never expose tallies regardless.
    """
    if include_counts:
        return _render_bar_chart_html_with_counts()

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad feedback</title>
<style>
  body {{
    font-family: system-ui, -apple-system, Segoe UI, sans-serif;
    margin: 0; padding: 24px; background: #fafafa; color: #1a1a1a;
    line-height: 1.55;
  }}
  .panel {{
    background: #fff; border-radius: 10px;
    padding: 22px 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    max-width: 520px;
  }}
  h1 {{ font-size: 1.2rem; margin: 0 0 14px; }}
  p {{ margin: 10px 0; color: #333; }}
  pre {{
    background: #f0f0f0; padding: 12px 14px;
    border-radius: 8px; overflow-x: auto; font-size: 0.88rem;
    margin: 14px 0;
  }}
  code {{ font-size: 0.88em; }}
</style>
</head>
<body>
<div class="panel">
  <h1>Ad feedback</h1>
  <p>Vote totals are not shown here. The thank-you page after someone taps Yes, Meh,
  or No stays generic too (no counts).</p>
  <p>To see tallies privately on your own machine, run:</p>
  <pre><code>python main.py ad-feedback-stats</code></pre>
  <p>Optional breakdown chart (operators only - do not publish):<br/>
  <code>python main.py ad-feedback-chart --full</code></p>
</div>
</body>
</html>
"""


def _render_bar_chart_html_with_counts() -> str:
    """HTML/CSS bar chart with numeric breakdown (operators only)."""
    counts = vote_counts()
    ordered_keys = ["yes", "meh", "no"]
    total = sum(counts[c] for c in ordered_keys)
    p = escape(str(db_path().resolve()))

    rows: list[str] = []
    for c in ordered_keys:
        n = counts[c]
        label_esc = escape(_LABELS[c])
        col = _COLORS_HTML[c]
        pct_val = (100.0 * n / total) if total else 0.0
        width = pct_val if total else 0.0
        rows.append(
            f'<div class="row">'
            f'<span class="label">{label_esc}</span>'
            f'<div class="track" aria-label="{label_esc}"><div class="fill" '
            f'style="width:{width:.2f}%;background:{col}"></div></div>'
            f'<span class="num">{n} <span class="pct">({pct_val:.1f}%)</span></span>'
            f"</div>"
        )

    empty_note = ""
    if total == 0:
        empty_note = (
            '<p class="note">No votes yet. Tallies appear after taps on email links '
            "(with <code>ad-feedback-serve</code> running).</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad feedback</title>
<style>
  body {{
    font-family: system-ui, -apple-system, Segoe UI, sans-serif;
    margin: 0; padding: 24px; background: #fafafa; color: #1a1a1a;
  }}
  h1 {{ font-size: 1.25rem; margin: 0 0 8px; }}
  .subtitle {{ font-size: 0.85rem; color: #555; margin: 0 0 20px; }}
  code {{ font-size: 0.8em; background: #eee; padding: 2px 6px; border-radius: 4px; }}
  .panel {{
    background: #fff; border-radius: 10px;
    padding: 20px 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    max-width: 620px;
  }}
  .row {{
    display: grid;
    grid-template-columns: minmax(6em,8em) 1fr minmax(5.5em,7em);
    gap: 10px;
    align-items: center;
    margin-bottom: 14px;
  }}
  .label {{ font-weight: 600; }}
  .track {{
    height: 22px;
    border-radius: 6px;
    background: #e8e8e8;
    overflow: hidden;
  }}
  .fill {{ height: 100%; border-radius: 6px;
    min-width: 0;
    transition: width 0.25s ease;
  }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pct {{ color: #666; font-size: 0.92em; }}
  .footer {{ margin-top: 18px; font-size: 0.75rem; color: #888;
    word-break: break-all; }}
  .note {{ margin: 12px 0 0; color: #444; line-height: 1.45; }}
  .total {{
    margin-top: 8px;
    padding-top: 12px;
    border-top: 1px solid #eee;
    font-size: 0.95rem;
  }}
</style>
</head>
<body>
<div class="panel">
  <h1>Was this ad helpful?</h1>
  <p class="subtitle"><strong>Private</strong> - do not publish or forward this HTML.</p>
  {empty_note}
  {"".join(rows)}
  <p class="total"><strong>Total responses:</strong> {total}</p>
  <p class="footer">Source: <code>{p}</code></p>
</div>
</body>
</html>
"""


_THANKS_BYTES = """<!DOCTYPE html><html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voted</title>
</head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#fff;font-family:system-ui,sans-serif">
<p style="margin:24px;font-size:1.125rem;color:#222;line-height:1.5;text-align:center">
Thank you for voting!
</p>
</body>
</html>
""".encode(
    "utf-8",
)

_MAX_BODY_READ = 4096


def _first_vote_choice(q: dict[str, list[str]]) -> str | None:
    lst = q.get("choice") or []
    raw = lst[0] if lst else None
    if raw is None:
        return None
    c = raw.strip().lower()
    return c if c in VALID_CHOICES else None


def _send_no_store_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")


def _serve_thanks(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(_THANKS_BYTES)))
    _send_no_store_headers(handler)
    handler.end_headers()
    handler.wfile.write(_THANKS_BYTES)


def _serve_vote_bad(handler: BaseHTTPRequestHandler, message: bytes) -> None:
    handler.send_response(400)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(message)))
    _send_no_store_headers(handler)
    handler.end_headers()
    handler.wfile.write(message)


def _vote_redirect_thanks(handler: BaseHTTPRequestHandler) -> None:
    """Persisted vote OK — redirect GET /thanks."""
    handler.send_response(303)
    handler.send_header("Location", "/thanks")
    _send_no_store_headers(handler)
    handler.end_headers()


def _make_handler(logger: logging.Logger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _run_vote_flow(self, q: dict[str, list[str]]) -> None:
            choice = _first_vote_choice(q)
            if choice is None:
                _serve_vote_bad(
                    self,
                    b"Bad vote: use choice=yes | meh | no (GET query or POST body).\n",
                )
                return
            try:
                record_vote(choice)
            except ValueError:
                _serve_vote_bad(self, b"Invalid choice\n")
                return
            except OSError as exc:
                logger.error("SQLite write failed: %s", exc)
                msg = b"Temporary error storing vote - try again later.\n"
                self.send_response(503)
                self.send_header(
                    "Content-Type", "text/plain; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(msg)))
                _send_no_store_headers(self)
                self.end_headers()
                self.wfile.write(msg)
                return
            _vote_redirect_thanks(self)

        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            req_path, q = _parsed_request_path(self.path)
            if req_path == "/vote":
                self._run_vote_flow(q)
                return
            if req_path.rstrip("/") == "/thanks":
                _serve_thanks(self)
                return
            body = b"Not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            _send_no_store_headers(self)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 — stdlib API
            req_path, _ = _parsed_request_path(self.path)
            if req_path != "/vote":
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD, OPTIONS, POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw_len = self.headers.get("Content-Length")
            try:
                n = int(raw_len) if raw_len is not None else 0
            except ValueError:
                _serve_vote_bad(self, b"Bad Content-Length\n")
                return
            if n > _MAX_BODY_READ:
                _serve_vote_bad(self, b"Request body too large\n")
                return
            try:
                body = self.rfile.read(n).decode("utf-8", errors="replace") if n else ""
            except OSError:
                self.send_response(400)
                self.end_headers()
                return
            self._run_vote_flow(parse_qs(body))

    return Handler


def run_server(host: str, port: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("ad_feedback_server")
    handler = _make_handler(log)
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info("Ad feedback server on http://%s:%s (DB: %s)", host, port, db_path())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        httpd.server_close()


def main_stats() -> int:
    print(render_bar_chart())
    return 0


def main_chart(argv: list[str] | None = None) -> int:
    """Write HTML about feedback (default: no public vote counts); see ``--full``."""
    ap = argparse.ArgumentParser(
        description=(
            "Write HTML for ad feedback (default hides vote counts). "
            "Use --full for a bar-chart breakdown (operators only)."
        ),
    )
    ap.add_argument(
        "-o",
        "--output",
        default="ad-feedback-chart.html",
        metavar="PATH",
        help="Output file (default: ./ad-feedback-chart.html)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Include vote counts and bar chart (never publish this file).",
    )
    args = ap.parse_args(argv)
    dest = Path(args.output).expanduser().resolve()
    dest.write_text(
        render_bar_chart_html(include_counts=args.full),
        encoding="utf-8",
    )
    print(str(dest))
    return 0


def main_serve(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record email ad-feedback clicks to SQLite.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main_serve())
