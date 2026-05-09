"""SQLite ad-feedback store, vote URLs, and HTTP handler."""

import http.server
import threading
import urllib.parse
import urllib.request
from unittest.mock import MagicMock

import pytest

from ad_feedback_cli import _make_handler, render_bar_chart_html
from ad_feedback_store import record_vote, vote_counts, vote_href


def test_vote_href_strips_slash_and_builds_paths():
    assert vote_href("https://example.com/ngrok/", "yes") == (
        "https://example.com/ngrok/vote?choice=yes"
    )


def test_record_vote_bad_choice_raises():
    with pytest.raises(ValueError):
        record_vote("maybe")


def test_record_vote_strips_whitespace_and_case(monkeypatch, tmp_path):
    db = tmp_path / "n.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    record_vote("  YES ")
    record_vote("\tMeh\n")
    c = vote_counts()
    assert c["yes"] == 1 and c["meh"] == 1 and c["no"] == 0


def test_record_vote_counts(tmp_path, monkeypatch):
    db = tmp_path / "adfb.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    record_vote("yes")
    record_vote("yes")
    record_vote("no")
    c = vote_counts()
    assert c == {"yes": 2, "meh": 0, "no": 1}


def test_http_vote_then_redirect_follows(tmp_path, monkeypatch):
    db = tmp_path / "votes.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))

    Handler = _make_handler(MagicMock())
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    _, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r = urllib.request.urlopen(f"{base}/vote?choice=meh", timeout=10)
        assert r.status == 200
        assert r.geturl().rstrip("/").endswith("thanks")
        page = r.read().decode("utf-8")
        assert "Thank you for voting!" in page
        assert vote_counts()["meh"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_get_vote_accepts_choice_casing(monkeypatch, tmp_path):
    db = tmp_path / "c.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    Handler = _make_handler(MagicMock())
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    _, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        urllib.request.urlopen(f"{base}/vote?choice=YES", timeout=10)
        assert vote_counts()["yes"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_post_vote_records(monkeypatch, tmp_path):
    db = tmp_path / "post.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    Handler = _make_handler(MagicMock())
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    _, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        body = urllib.parse.urlencode({"choice": "no"}).encode()
        req = urllib.request.Request(
            f"{base}/vote",
            data=body,
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=10)
        assert r.status == 200
        assert "Thank you for voting!" in r.read().decode("utf-8")
        assert vote_counts()["no"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_render_bar_chart_html_default_hides_vote_counts(monkeypatch, tmp_path):
    db = tmp_path / "chart.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    record_vote("yes")
    record_vote("yes")
    html = render_bar_chart_html()
    assert "ad-feedback-stats" in html
    assert "Total responses:" not in html


def test_render_bar_chart_html_full_shows_totals(monkeypatch, tmp_path):
    db = tmp_path / "chartf.sqlite"
    monkeypatch.setenv("AD_FEEDBACK_DB_PATH", str(db))
    record_vote("yes")
    record_vote("yes")
    record_vote("meh")
    html = render_bar_chart_html(include_counts=True)
    assert "Total responses:" in html
    assert str(db.resolve()).replace("\\", "/") in html.replace("\\", "/")
