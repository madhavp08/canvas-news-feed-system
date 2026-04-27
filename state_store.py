"""
state_store.py — Persistence layer for the monitor's operational and audit state.

Reads and writes state.json. Uses atomic writes (temp file + rename) to
prevent corruption if the process crashes mid-write.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def load_state(path: str) -> dict | None:
    """Load previously saved state from a JSON file.

    Returns None if the file does not exist (first run).
    Raises on malformed JSON so corruption is never silent.
    """
    p = Path(path)
    if not p.exists():
        logger.info("No existing state file at %s (first run)", path)
        return None

    data = json.loads(p.read_text(encoding="utf-8"))
    logger.info(
        "Loaded state: last_change_type=%s, latest_date='%s', checked_at=%s",
        data.get("last_change_type"),
        data.get("latest_date_raw"),
        data.get("last_checked_at"),
    )
    return data


def save_state(path: str, newest_entry: dict, change_type: str) -> None:
    """Persist the newest entry as the new baseline state.

    Writes to a temporary file in the same directory, then atomically
    renames it to the target path.
    """
    state = {
        "latest_date_raw": newest_entry["date_raw"],
        "latest_date_normalized": newest_entry["date_normalized"],
        "latest_items": newest_entry["items"],
        "latest_content_text": newest_entry["content_text"],
        "latest_content_hash": newest_entry["content_hash"],
        "last_checked_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "last_change_type": change_type,
    }

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".state_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info(
        "Saved state: change_type=%s, date='%s', hash=%s…",
        change_type,
        state["latest_date_raw"],
        state["latest_content_hash"][:12],
    )
