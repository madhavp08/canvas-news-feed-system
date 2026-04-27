# Canvas News Feed Monitor

A personal monitoring tool that detects new or edited announcements in a Canvas/ELMS course homepage's News Feed section and emails your classmates about changes.

## Architecture

| Module | Responsibility |
|---|---|
| `fetcher.py` | Retrieves page HTML (local file or live via Canvas API + browser cookies) |
| `parser.py` | Extracts structured `NewsEntry` dicts from the News Feed section only |
| `detector.py` | Pure function that classifies change: `INITIALIZED`, `NEW_DATE`, `UPDATED_SAME_DATE`, or `NO_CHANGE` |
| `state_store.py` | Reads/writes `state.json` with atomic file operations |
| `notifier.py` | Sends email notifications via the SendGrid HTTP API (`requests`) |
| `main.py` | CLI with `check` (one-shot) and `poll` (continuous loop) modes |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

You'll need:
- A **SendGrid account** (free tier works) and an API key from https://app.sendgrid.com/settings/api_keys
- A **verified sender** email address in SendGrid
- The email addresses of your classmates to notify

## Usage

### One-shot check

```bash
# Live check using Comet browser cookies
python main.py check --url https://umd.instructure.com/courses/1398395

# Using a saved HTML file (for testing)
python main.py check --html-file 216HomePage.html

# Skip email notification
python main.py check --url https://umd.instructure.com/courses/1398395 --no-notify
```

### Continuous polling

```bash
# Poll every hour (default, reads config from .env)
python main.py poll

# Custom interval (in seconds)
python main.py poll --url https://umd.instructure.com/courses/1398395 --interval 1800

# Run in background
nohup python main.py poll > monitor.log 2>&1 &
```

Stop polling with Ctrl+C (it finishes the current cycle cleanly).

You can also run `chmod +x scripts/run_poll.sh` once, then `./scripts/run_poll.sh` (extra args pass through, e.g. `--interval 1800`). Handy for cron or a Terminal shortcut.

## Production checklist

- **Config:** Fill `.env` from `.env.example`: `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL` (verified in SendGrid), `NOTIFY_EMAILS` (comma-separated addresses; no spaces around commas is safest), `CANVAS_COURSE_URL`, and optionally `BROWSER` (e.g. `comet`) and `POLL_INTERVAL` (seconds; default `3600` = one hour).
- **Run:** From the project directory with the venv activated: `python main.py poll`.
- **Background:** `nohup python main.py poll >> monitor.log 2>&1 &` keeps polling if you close the terminal. For login-time start, use **launchd** (macOS) or schedule `main.py check` with **cron** if you prefer not to leave a long-running process.
- **First run:** Baseline state is saved with change type `INITIALIZED` — **no email is sent**. Emails only go out on `NEW_DATE` or `UPDATED_SAME_DATE`.
- **Comet / Canvas:** Live fetches use cookies from the browser you set in `BROWSER`. Stay logged into Canvas there; renew the session if checks start failing. On macOS, approve keychain prompts once (e.g. **Always Allow**) so cookie decryption works.

## How it works

1. **Scoped parsing** — Only the "News feed" section is parsed. All surrounding Canvas markup is ignored.
2. **Normalized comparison** — Whitespace, formatting tags, and style attributes are stripped so cosmetic changes don't trigger false alerts.
3. **Two-level change detection** — Checks both the newest date and a content hash, catching new announcements as well as edits to the current day's entry.
4. **Atomic persistence** — State is written to a temp file and renamed, preventing corruption from crashes.
5. **Resilient polling** — Transient fetch/parse errors are logged and retried on the next cycle instead of crashing.

## Running tests

```bash
python -m pytest tests/ -v
```
