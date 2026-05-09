"""SQLite persistence for optional email ad-feedback clicks (see ad_feedback_server)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

VALID_CHOICES = frozenset({"yes", "meh", "no"})


def vote_href(public_base_url: str, choice: str) -> str:
    """Build ``/vote?choice=…`` URL (``AD_FEEDBACK_PUBLIC_URL`` without trailing slash)."""
    if choice not in VALID_CHOICES:
        raise ValueError(choice)
    base = public_base_url.rstrip("/").strip()
    return f"{base}/vote?choice={choice}"

_DEFAULT_DB = Path(__file__).resolve().parent / "data" / "ad_feedback.sqlite"


def db_path() -> Path:
    raw = (os.environ.get("AD_FEEDBACK_DB_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_DB


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_feedback_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    init_db(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def record_vote(choice: str) -> None:
    c = choice.strip().lower()
    if c not in VALID_CHOICES:
        raise ValueError(f"invalid choice: {choice!r}")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO ad_feedback_votes (choice) VALUES (?)",
            (c,),
        )
        conn.commit()
    finally:
        conn.close()


def vote_counts() -> dict[str, int]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT choice, COUNT(*) FROM ad_feedback_votes GROUP BY choice"
        ).fetchall()
    finally:
        conn.close()
    out = {c: 0 for c in sorted(VALID_CHOICES)}
    for choice, n in rows:
        if choice in VALID_CHOICES:
            out[choice] = int(n)
    return out


def total_votes() -> int:
    return sum(vote_counts().values())
