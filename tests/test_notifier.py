"""Tests for notifier.py — SendGrid payload shape and batching."""

from unittest.mock import MagicMock, patch

import pytest

from notifier import (
    NotifyError,
    SENDGRID_MAX_PERSONALIZATIONS_PER_REQUEST,
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
