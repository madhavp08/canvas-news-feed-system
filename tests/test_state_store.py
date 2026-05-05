"""Tests for state_store.load_state validation."""

import json

from state_store import load_state


def test_load_state_missing_file_returns_none(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) is None


def test_load_state_invalid_json_returns_none(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_state(str(p)) is None
    assert "not valid JSON" in caplog.text


def test_load_state_missing_required_key_returns_none(tmp_path, caplog):
    p = tmp_path / "state.json"
    partial = {"latest_date_raw": "Monday", "latest_content_hash": "x"}
    p.write_text(json.dumps(partial), encoding="utf-8")
    assert load_state(str(p)) is None
    assert "missing required key" in caplog.text


def test_load_state_accepts_minimal_valid_object(tmp_path):
    p = tmp_path / "state.json"
    obj = {
        "latest_date_raw": "Monday",
        "latest_date_normalized": "monday",
        "latest_items": [["a"]],
        "latest_content_text": "a",
        "latest_content_hash": "abc",
    }
    p.write_text(json.dumps(obj), encoding="utf-8")
    out = load_state(str(p))
    assert out is not None
    assert out["latest_date_raw"] == "Monday"
