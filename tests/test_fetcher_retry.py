"""Transient API retry behavior in fetcher (mocked ``requests``, no network)."""

from unittest.mock import Mock, patch

import pytest
import requests

import fetcher


def _json_response(body: str = "<div>ok</div>") -> Mock:
    r = Mock()
    r.status_code = 200
    r.headers = {"Content-Type": "application/json"}
    r.json.return_value = {"body": body}
    r.text = ""
    return r


_API = "https://x.test/api/v1/courses/1/front_page"
_COURSE = "https://x.test/courses/1"


@patch("fetcher.time.sleep")
@patch("fetcher.requests.get")
def test_get_retries_connection_errors_then_succeeds(mock_get, _sleep):
    mock_get.side_effect = [
        requests.exceptions.ConnectionError("dns"),
        requests.exceptions.ConnectionError("dns"),
        _json_response("<p>x</p>"),
    ]
    html = fetcher._fetch_front_page_with_cookies(
        _API,
        _COURSE,
        "comet",
        {},
    )
    assert html == "<p>x</p>"
    assert mock_get.call_count == 3


@patch("fetcher.time.sleep")
@patch("fetcher.requests.get")
def test_get_retries_timeout_then_succeeds(mock_get, _sleep):
    mock_get.side_effect = [
        requests.exceptions.Timeout(),
        _json_response(),
    ]
    html = fetcher._fetch_front_page_with_cookies(
        _API,
        _COURSE,
        "comet",
        {},
    )
    assert "<div>ok</div>" in html
    assert mock_get.call_count == 2


@patch("fetcher.time.sleep")
@patch("fetcher.requests.get")
def test_get_exhausts_retries_raises_fetch_error(mock_get, _sleep):
    mock_get.side_effect = requests.exceptions.ConnectionError("unreachable")
    with pytest.raises(fetcher.FetchError, match="Network error after"):
        fetcher._fetch_front_page_with_cookies(
            _API,
            _COURSE,
            "comet",
            {},
        )
    assert mock_get.call_count == fetcher._FETCH_TRANSIENT_ATTEMPTS


@patch("fetcher.requests.get")
def test_http_401_not_transient_retry_loop(mock_get):
    r = Mock()
    r.status_code = 401
    r.text = "nope"
    r.headers = {}
    mock_get.return_value = r
    with pytest.raises(fetcher.FetchError, match="401/403"):
        fetcher._fetch_front_page_with_cookies(
            _API,
            _COURSE,
            "comet",
            {},
        )
    mock_get.assert_called_once()
