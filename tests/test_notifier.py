"""Tests for notifier.py — SendGrid payload shape and batching."""

from unittest.mock import MagicMock, patch

import pytest
import re

from notifier import (
    NotifyError,
    SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST,
    _append_promo_to_plain,
    _build_body,
    _build_html_body,
    _build_subject,
    _email_html_passes_send_checks,
    _ensure_sendgrid_html,
    _normalize_rendered_html_fragment,
    _preview_text,
    _react_email_props,
    _render_news_email_html_subprocess,
    _linkify_promo_to_html,
    normalize_tldr,
    send_notification,
)


@pytest.fixture(autouse=True)
def _clear_promo_email_env(monkeypatch):
    monkeypatch.delenv("EMAIL_PROMO_TEXT", raising=False)
    monkeypatch.delenv("EMAIL_PROMO_LOGO_URL", raising=False)


def _sample_entry():
    return {
        "date_raw": "Monday, April 27",
        "items": ["Announcement A"],
    }


@patch("notifier.requests.post")
def test_one_personalization_per_recipient(mock_post):
    mock_post.return_value = MagicMock(status_code=202, text="")

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com", "b@example.com", "c@example.com"],
    )

    assert mock_post.call_count == 1
    payload = mock_post.call_args.kwargs["json"]
    pers = payload["personalizations"]
    assert len(pers) == 3
    for p, expected in zip(
        pers, ["a@example.com", "b@example.com", "c@example.com"], strict=True
    ):
        assert p["to"] == [{"email": expected}]
        assert "subject" in p
    assert payload["from"] == {"email": "from@example.com"}


@patch("notifier.requests.post")
def test_batches_when_over_sendgrid_limit(mock_post):
    mock_post.return_value = MagicMock(status_code=202, text="")
    n = SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST + 1
    emails = [f"u{i}@example.com" for i in range(n)]

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=emails,
    )

    assert mock_post.call_count == 2
    first = mock_post.call_args_list[0].kwargs["json"]
    second = mock_post.call_args_list[1].kwargs["json"]
    assert len(first["personalizations"]) == SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST
    assert len(second["personalizations"]) == 1


@patch("notifier.requests.post")
def test_sendgrid_error_raises_notify_error(mock_post):
    mock_post.return_value = MagicMock(status_code=400, text="bad request")

    with pytest.raises(NotifyError, match="400"):
        send_notification(
            "NEW_DATE",
            _sample_entry(),
            api_key="key",
            from_email="from@example.com",
            to_emails=["a@example.com"],
        )


def _html_from_payload(payload: dict) -> str:
    for block in payload["content"]:
        if block["type"] == "text/html":
            return block["value"]
    raise AssertionError("no HTML part")


def test_normalize_tldr_none_and_empty():
    assert normalize_tldr(None) is None
    assert normalize_tldr("") is None
    assert normalize_tldr("  \n\t ") is None


def test_normalize_tldr_paragraph():
    assert normalize_tldr("One line only") == {
        "kind": "paragraph",
        "text": "One line only",
    }
    assert normalize_tldr("First line.\nSecond line.") == {
        "kind": "paragraph",
        "text": "First line.\nSecond line.",
    }


def test_normalize_tldr_bullets_only():
    assert normalize_tldr("- alpha\n- beta") == {
        "kind": "bullets",
        "lead": None,
        "bullets": ["alpha", "beta"],
    }
    assert normalize_tldr("* dash\n• bullet") == {
        "kind": "bullets",
        "lead": None,
        "bullets": ["dash", "bullet"],
    }
    assert normalize_tldr("1. one\n2. two") == {
        "kind": "bullets",
        "lead": None,
        "bullets": ["one", "two"],
    }


def test_normalize_tldr_lead_then_bullets():
    assert normalize_tldr("Do this first.\n- a\n- b") == {
        "kind": "bullets",
        "lead": "Do this first.",
        "bullets": ["a", "b"],
    }


def test_normalize_tldr_mixed_becomes_paragraph():
    out = normalize_tldr("- a\nInterrupted\n- b")
    assert out == {"kind": "paragraph", "text": "- a\nInterrupted\n- b"}


@patch("notifier.requests.post")
def test_injected_react_html_used_without_legacy_tldr_prefix(mock_post):
    mock_post.return_value = MagicMock(status_code=202, text="")

    def fake_render(props: dict) -> str:
        assert props["changeType"] == "NEW_DATE"
        assert props["tldr"] is None
        assert props["promoText"] is None
        assert props["promoLogoUrl"] is None
        assert props["promoLinkHref"] is None
        d = props["dateRaw"]
        return f"<html><body>{d} Announcement A</body></html>"

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com"],
        render_html_fn=fake_render,
    )
    html = _html_from_payload(mock_post.call_args.kwargs["json"])
    assert "Monday, April 27" in html
    assert "Announcement A" in html
    assert "<strong>TL;DR" not in html


@patch("notifier._maybe_gemini_tldr", return_value="- Line one\n- Line two")
@patch("notifier.requests.post")
def test_injected_react_html_receives_normalized_tldr(mock_post, _mock_gemini):
    mock_post.return_value = MagicMock(status_code=202, text="")
    seen: dict = {}

    def fake_render(props: dict) -> str:
        seen["tldr"] = props["tldr"]
        d = props["dateRaw"]
        return f"<html><body>{d} Announcement A</body></html>"

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com"],
        render_html_fn=fake_render,
    )
    assert seen["tldr"] == {
        "kind": "bullets",
        "lead": None,
        "bullets": ["Line one", "Line two"],
    }
    html = _html_from_payload(mock_post.call_args.kwargs["json"])
    assert "Monday, April 27" in html
    assert "Announcement A" in html
    plain = next(
        b["value"] for b in mock_post.call_args.kwargs["json"]["content"] if b["type"] == "text/plain"
    )
    assert "TL;DR:" in plain


@patch("notifier._maybe_gemini_tldr", return_value="Summary here")
@patch("notifier.requests.post")
def test_legacy_html_when_render_returns_none(mock_post, _mock_gemini):
    mock_post.return_value = MagicMock(status_code=202, text="")

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com"],
        render_html_fn=lambda _props: None,
    )
    html = _html_from_payload(mock_post.call_args.kwargs["json"])
    assert "#FFB8B8" in html
    assert "Summary here" in html


def test_normalize_rendered_html_fragment_strips_leading_noise():
    raw = "some stderr line\n<!DOCTYPE html><html><body>x</body></html>"
    out = _normalize_rendered_html_fragment(raw)
    assert out is not None
    assert out.lower().startswith("<!doctype")


def test_normalize_rendered_html_fragment_finds_lowercase_html_tag():
    raw = "noise\n<html><body>x</body></html>"
    out = _normalize_rendered_html_fragment(raw)
    assert out is not None
    assert out.startswith("<html>")


def test_normalize_rendered_html_fragment_strips_bom():
    raw = "\ufeff<html><body>x</body></html>"
    out = _normalize_rendered_html_fragment(raw)
    assert out is not None
    assert out.startswith("<html>")


def test_normalize_rendered_html_fragment_garbage_returns_none():
    assert _normalize_rendered_html_fragment("not html at all") is None


def test_react_email_props_promo_null(monkeypatch):
    monkeypatch.delenv("EMAIL_PROMO_TEXT", raising=False)
    entry = {"date_raw": "Monday", "items": ["x"]}
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", entry, None, preview)
    assert props["promoText"] is None
    assert props["promoLogoUrl"] is None
    assert props["promoLinkHref"] is None


def test_react_email_props_promo_from_env(monkeypatch):
    monkeypatch.setenv(
        "EMAIL_PROMO_TEXT", "Try https://www.thinkex.app/home "
    )
    entry = {"date_raw": "Monday", "items": ["x"]}
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", entry, None, preview)
    assert props["promoText"] == "Try https://www.thinkex.app/home"
    assert props["promoLogoUrl"] is not None
    assert props["promoLogoUrl"].endswith("skgGwtd")
    assert "thinkex.app/newlogothinkex-light.svg" in props["promoLogoUrl"]
    assert props["promoLinkHref"] == "https://www.thinkex.app/home"


def test_react_email_props_promo_logo_override(monkeypatch):
    monkeypatch.setenv("EMAIL_PROMO_TEXT", "Go https://example.com/promo")
    monkeypatch.setenv("EMAIL_PROMO_LOGO_URL", "https://cdn.example.com/logo.png")
    entry = {"date_raw": "Monday", "items": ["x"]}
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", entry, None, preview)
    assert props["promoLogoUrl"] == "https://cdn.example.com/logo.png"
    assert props["promoLinkHref"] == "https://example.com/promo"


def test_react_email_props_promo_text_without_url(monkeypatch):
    monkeypatch.setenv("EMAIL_PROMO_TEXT", "  Plain promo, no hyperlink  ")
    entry = {"date_raw": "Monday", "items": ["x"]}
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", entry, None, preview)
    assert props["promoText"] == "Plain promo, no hyperlink"
    assert props["promoLogoUrl"] is not None
    assert props["promoLinkHref"] is None
    out = _build_html_body("NEW_DATE", {**entry, "items": [[{"type": "text", "text": "y"}]]})
    idx = out.find("promo-callout")
    assert idx >= 0
    chunk = out[idx : idx + 1200]
    assert "<a " not in chunk


def test_legacy_html_promo_escaped_and_linkified(monkeypatch):
    monkeypatch.setenv(
        "EMAIL_PROMO_TEXT", "See literal angle brackets &lt;x&gt;: https://example.com/foo ok"
    )
    entry = {
        "date_raw": "Tuesday, May 5",
        "items": [[{"type": "text", "text": "one"}]],
    }
    html = _build_html_body("NEW_DATE", entry)
    assert "Sent by Madhav's Canvas News Feed Monitor" in html
    assert "&amp;lt;x&amp;gt;" in html
    assert 'href="https://example.com/foo"' in html
    assert "newlogothinkex-light.svg" in html
    assert "promo-logo-chip" in html


def test_linkify_promo_unified_cta_multiple_urls():
    raw = "See https://a.example/first and https://b.example/second end"
    cta = "https://a.example/first"
    out = _linkify_promo_to_html(raw, cta)
    assert 'href="https://a.example/first"' in out
    assert 'href="https://b.example/second"' not in out
    assert "https://b.example/second" in out


def test_linkify_promo_no_unified_keeps_per_url_hrefs():
    raw = "See https://a.example/first and https://b.example/second end"
    out = _linkify_promo_to_html(raw, None)
    assert 'href="https://a.example/first"' in out
    assert 'href="https://b.example/second"' in out


def test_legacy_html_promo_two_urls_single_cta(monkeypatch):
    monkeypatch.setenv(
        "EMAIL_PROMO_TEXT",
        "Try https://a.example/first then https://b.example/second done",
    )
    monkeypatch.setenv(
        "EMAIL_PROMO_LOGO_URL",
        "https://cdn.example.com/only-logo.png",
    )
    entry = {
        "date_raw": "Tuesday, May 5",
        "items": [[{"type": "text", "text": "one"}]],
    }
    html = _build_html_body("NEW_DATE", entry)
    assert html.count('href="https://a.example/first"') == 2
    assert 'href="https://b.example/second"' not in html
    assert "https://b.example/second" in html
    assert "only-logo.png" in html


def test_append_promo_to_plain(monkeypatch):
    monkeypatch.setenv("EMAIL_PROMO_TEXT", "Promo line")
    body = _build_body("NEW_DATE", _sample_entry())
    out = _append_promo_to_plain(body)
    assert "\n---\nPromo line\n" in out


def test_email_html_passes_send_checks_decodes_entities_in_body():
    """Avoid false legacy fallback when React escapes & < > in text."""
    html = (
        "<html><body><p>New announcement posted for Tuesday, May 5:</p>"
        "<p>Lab A &amp; B see 1 &lt; 2</p></body></html>"
    )
    items = [[{"type": "text", "text": "Lab A & B see 1 < 2"}]]
    plain = "x" * 80
    assert _email_html_passes_send_checks(
        html,
        date_raw="Tuesday, May 5",
        items=items,
        plain=plain,
    )


def test_ensure_sendgrid_falls_back_when_date_missing_from_html():
    entry = {
        "date_raw": "Tuesday, May 5",
        "items": [[{"type": "text", "text": "Hello course"}]],
    }
    long_plain = "TL;DR:\n" + ("word " * 200)
    html = _ensure_sendgrid_html(
        "<html><body>Hello course but wrong date header</body></html>",
        "NEW_DATE",
        entry,
        None,
        long_plain,
    )
    assert "Tuesday, May 5" in html
    assert "Hello course" in html
    assert "<ol>" in html


def test_ensure_sendgrid_falls_back_when_first_line_missing():
    entry = {
        "date_raw": "Tuesday, May 5",
        "items": [[{"type": "text", "text": "Expected line"}]],
    }
    long_plain = "body " * 120
    html = _ensure_sendgrid_html(
        "<html><body><p>Tuesday, May 5</p><p>other</p></body></html>",
        "NEW_DATE",
        entry,
        None,
        long_plain,
    )
    assert "Expected line" in html


@patch("notifier._maybe_gemini_tldr", return_value=None)
@patch("notifier.requests.post")
def test_send_notification_falls_back_when_react_stub_incomplete(
    mock_post, _mock_gemini
):
    mock_post.return_value = MagicMock(status_code=202, text="")

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com"],
        render_html_fn=lambda _props: "<html><body>empty</body></html>",
    )
    html = _html_from_payload(mock_post.call_args.kwargs["json"])
    assert "Monday, April 27" in html
    assert "Announcement A" in html


def test_subprocess_react_email_passes_content_checks(require_react_email_subprocess):
    entry = {
        "date_raw": "Tuesday, May 5",
        "items": [
            [{"type": "text", "text": "First bulletin"}],
            [{"type": "text", "text": "Unicode τέστ"}],
            [
                {
                    "type": "link",
                    "href": "https://example.com/a",
                    "label": "Example link",
                }
            ],
        ],
    }
    send_entry = dict(entry)
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", send_entry, None, preview)
    html = _render_news_email_html_subprocess(props)
    assert html is not None
    plain = _build_body("NEW_DATE", send_entry)
    assert _email_html_passes_send_checks(
        html,
        date_raw=entry["date_raw"],
        items=send_entry["items"],
        plain=plain,
    )
    assert "Tuesday, May 5" in html
    assert "First bulletin" in html
    assert "τέστ" in html
    assert "Example link" in html


def test_subprocess_react_email_promo_before_footer(
    monkeypatch, require_react_email_subprocess
):
    """Smoke: TL;DR card (#FFB8B8), bulletin, promo callout with link, divider, attribution."""
    monkeypatch.setenv(
        "EMAIL_PROMO_TEXT",
        "SMOKE_PROMO_UNIQUE study finals https://www.thinkex.app/home end",
    )
    entry = {
        "date_raw": "Wednesday, January 14",
        "items": [[{"type": "text", "text": "Bulletin line for smoke test"}]],
    }
    send_entry = dict(entry)
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    tldr_norm = normalize_tldr("- first tldr point\n- second tldr point")
    assert tldr_norm is not None
    preview = _preview_text(subj, entry["date_raw"], tldr_norm, None)
    props = _react_email_props("NEW_DATE", send_entry, tldr_norm, preview)
    assert props["promoText"] is not None
    html = _render_news_email_html_subprocess(props)
    assert html is not None
    low = html.lower()
    assert "ffb8b8" in low
    assert "smoke_promo_unique" in low
    assert "thinkex.app" in low
    assert "newlogothinkex-light" in low
    assert "sent by madhav" in low
    assert re.search(
        r">tl;dr</p>.*?bulletin line for smoke test.*?smoke_promo_unique"
        r".*?thinkex\.app/home.*?sent by madhav",
        low,
        re.DOTALL,
    )
