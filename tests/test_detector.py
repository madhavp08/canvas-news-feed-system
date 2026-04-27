"""Tests for detector.py — change classification logic."""

from detector import detect_change


def _make_entry(date_normalized: str = "saturday, april 25", content_hash: str = "abc123"):
    return {
        "date_raw": date_normalized.title(),
        "date_normalized": date_normalized,
        "items": ["item 1"],
        "content_text": "item 1",
        "content_hash": content_hash,
    }


def _make_state(date_normalized: str = "saturday, april 25", content_hash: str = "abc123"):
    return {
        "latest_date_raw": date_normalized.title(),
        "latest_date_normalized": date_normalized,
        "latest_items": ["item 1"],
        "latest_content_text": "item 1",
        "latest_content_hash": content_hash,
        "last_checked_at": "2026-04-26T12:00:00-04:00",
        "last_change_type": "NEW_DATE",
    }


class TestDetectChange:
    def test_initialized_when_no_prior_state(self):
        entry = _make_entry()
        assert detect_change(entry, None) == "INITIALIZED"

    def test_no_change_when_identical(self):
        entry = _make_entry(date_normalized="saturday, april 25", content_hash="abc123")
        state = _make_state(date_normalized="saturday, april 25", content_hash="abc123")
        assert detect_change(entry, state) == "NO_CHANGE"

    def test_new_date_when_date_differs(self):
        entry = _make_entry(date_normalized="sunday, april 26", content_hash="def456")
        state = _make_state(date_normalized="saturday, april 25", content_hash="abc123")
        assert detect_change(entry, state) == "NEW_DATE"

    def test_updated_same_date_when_hash_differs(self):
        entry = _make_entry(date_normalized="saturday, april 25", content_hash="changed_hash")
        state = _make_state(date_normalized="saturday, april 25", content_hash="abc123")
        assert detect_change(entry, state) == "UPDATED_SAME_DATE"

    def test_new_date_takes_priority_over_hash_diff(self):
        """When both date and hash differ, the classification should be NEW_DATE
        because a new date is the more semantically significant event."""
        entry = _make_entry(date_normalized="monday, april 28", content_hash="new_hash")
        state = _make_state(date_normalized="saturday, april 25", content_hash="old_hash")
        assert detect_change(entry, state) == "NEW_DATE"
