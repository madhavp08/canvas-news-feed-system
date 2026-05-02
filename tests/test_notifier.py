"""Tests for notifier.py — SendGrid payload shape and batching."""

from unittest.mock import MagicMock, patch

import pytest

from notifier import (
    NotifyError,
    SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST,
    normalize_tldr,
    send_notification,
)


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
        return "<html><!--react-stub--></html>"

    send_notification(
        "NEW_DATE",
        _sample_entry(),
        api_key="key",
        from_email="from@example.com",
        to_emails=["a@example.com"],
        render_html_fn=fake_render,
    )
    html = _html_from_payload(mock_post.call_args.kwargs["json"])
    assert "<html><!--react-stub--></html>" == html
    assert "<strong>TL;DR" not in html


@patch("notifier._maybe_gemini_tldr", return_value="- Line one\n- Line two")
@patch("notifier.requests.post")
def test_injected_react_html_receives_normalized_tldr(mock_post, _mock_gemini):
    mock_post.return_value = MagicMock(status_code=202, text="")
    seen: dict = {}

    def fake_render(props: dict) -> str:
        seen["tldr"] = props["tldr"]
        return "<html>ok</html>"

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
    assert html == "<html>ok</html>"
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
    assert "<strong>TL;DR" in html
