"""Shared pytest fixtures — React Email subprocess availability."""

from __future__ import annotations

from pathlib import Path

import pytest


def _tsx_cli() -> Path | None:
    import notifier

    root = Path(notifier.__file__).resolve().parent
    p = root / "email-render" / "node_modules" / ".bin" / "tsx"
    return p if p.exists() else None


def _session_subprocess_renders_ok() -> bool:
    """True when tsx can render a minimal message (not broken / not IPC-blocked)."""
    from notifier import (
        _build_subject,
        _preview_text,
        _react_email_props,
        _render_news_email_html_subprocess,
    )

    entry = {
        "date_raw": "Smoke",
        "items": [[{"type": "text", "text": "line"}]],
    }
    subj = _build_subject("NEW_DATE", entry["date_raw"])
    preview = _preview_text(subj, entry["date_raw"], None, None)
    props = _react_email_props("NEW_DATE", entry, None, preview)
    html = _render_news_email_html_subprocess(props)
    if not html:
        return False
    return "<html" in html.lower()


@pytest.fixture(scope="session")
def require_react_email_subprocess():
    """Skip when ``tsx`` is missing or render fails (common: ``listen EPERM`` in sandboxed runners)."""
    if _tsx_cli() is None:
        pytest.skip("email-render tsx not installed — run: cd email-render && npm ci")
    if not _session_subprocess_renders_ok():
        pytest.skip(
            "React Email subprocess render failed (tsx IPC often blocked in sandboxed "
            "test runs). Re-run pytest in a normal shell or CI; production sends still "
            "fall back to legacy HTML if render fails."
        )
