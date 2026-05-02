"""Tests for main.py — CLI notification overrides (hermetic, no network)."""

import sys
from unittest.mock import patch

import pytest

import main
from main import _coerce_notify_email_override, check_once

_ENTRY = {
    "date_raw": "Sunday, April 26",
    "date_normalized": "sunday, april 26",
    "items": ["Announcement A"],
    "content_text": "Announcement A",
    "content_hash": "hash1",
}

_SUMMARY = {
    "change_type": "NEW_DATE",
    "newest_date": "Sunday, April 26",
    "newest_items_count": 1,
    "total_entries_parsed": 1,
}


def test_coerce_none_means_no_override():
    assert _coerce_notify_email_override(None) is None


def test_coerce_strips_and_dedupes_preserves_order():
    assert _coerce_notify_email_override(
        ["  a@example.com  ", "b@example.com", "a@example.com"]
    ) == ["a@example.com", "b@example.com"]


def test_coerce_rejects_blank_entry():
    with pytest.raises(ValueError, match="empty or whitespace"):
        _coerce_notify_email_override(["ok@example.com", "   "])


@patch("main.detect_change", return_value="NEW_DATE")
@patch("main.send_notification")
@patch("main.save_state")
@patch("main.load_state")
@patch("main.parse_news_feed", return_value=[_ENTRY])
@patch("main.fetch_from_url", return_value="<html/>")
def test_check_once_passes_to_emails_when_overridden(
    _fetch, _parse, _load, _save, mock_send, _dc
):
    check_once(
        state_file="state.json",
        url="http://example.test/course",
        notify_emails=["you@example.com", "other@example.com"],
    )
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_emails"] == [
        "you@example.com",
        "other@example.com",
    ]


@patch("main.detect_change", return_value="NEW_DATE")
@patch("main.send_notification")
@patch("main.save_state")
@patch("main.load_state")
@patch("main.parse_news_feed", return_value=[_ENTRY])
@patch("main.fetch_from_url", return_value="<html/>")
def test_check_once_uses_env_recipients_when_no_override(
    _fetch, _parse, _load, _save, mock_send, _dc
):
    check_once(state_file="state.json", url="http://example.test/course")
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("to_emails") is None


@patch("main.detect_change", return_value="NEW_DATE")
@patch("main.send_notification")
@patch("main.save_state")
@patch("main.load_state")
@patch("main.parse_news_feed", return_value=[_ENTRY])
@patch("main.fetch_from_url", return_value="<html/>")
def test_check_once_notify_false_does_not_call_send(
    _fetch, _parse, _load, _save, mock_send, _dc
):
    check_once(
        state_file="state.json",
        url="http://example.test/course",
        notify=False,
        notify_emails=["you@example.com"],
    )
    mock_send.assert_not_called()


@patch("main.poll_loop")
@patch("main.check_once", return_value=_SUMMARY)
@patch("main._load_env_file")
def test_main_check_passes_coerced_notify_emails(_load, mock_check, _poll):
    with patch.object(
        sys,
        "argv",
        [
            "main",
            "check",
            "--url",
            "http://course.test",
            "--notify-email",
            " first@x.com ",
            "--notify-email",
            "second@x.com",
        ],
    ):
        main.main()
    mock_check.assert_called_once()
    assert mock_check.call_args.kwargs["notify_emails"] == [
        "first@x.com",
        "second@x.com",
    ]


@patch("main.poll_loop")
@patch("main._load_env_file")
def test_main_poll_passes_coerced_notify_emails(_load, mock_poll):
    with patch.object(
        sys,
        "argv",
        [
            "main",
            "poll",
            "--url",
            "http://course.test",
            "--notify-email",
            "solo@example.com",
        ],
    ):
        main.main()
    mock_poll.assert_called_once()
    assert mock_poll.call_args.kwargs["notify_emails"] == ["solo@example.com"]


@patch("main._load_env_file")
def test_main_check_invalid_notify_email_exits(_load):
    with patch.object(
        sys,
        "argv",
        ["main", "check", "--url", "http://course.test", "--notify-email", "   "],
    ):
        with pytest.raises(SystemExit) as exc_info:
            main.main()
    assert exc_info.value.code == 2
