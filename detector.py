"""
detector.py — Change-detection layer.

Pure function with no I/O. Compares the newest parsed entry against
previously stored state and returns a classification string.
"""


def detect_change(newest_entry: dict, old_state: dict | None) -> str:
    """Classify the relationship between the newest parsed entry and stored state.

    Returns one of:
        "INITIALIZED"       — no prior state; first run baseline.
        "NEW_DATE"          — newest entry has a different date than stored.
        "UPDATED_SAME_DATE" — same date but content hash differs (edit detected).
        "NO_CHANGE"         — date and content hash both match.
    """
    if old_state is None:
        return "INITIALIZED"

    if newest_entry["date_normalized"] != old_state["latest_date_normalized"]:
        return "NEW_DATE"

    if newest_entry["content_hash"] != old_state["latest_content_hash"]:
        return "UPDATED_SAME_DATE"

    return "NO_CHANGE"
