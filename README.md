# Canvas News Feed Monitor

A personal monitoring tool that detects new or edited announcements in a Canvas/ELMS course homepage's News Feed section and emails your classmates about changes.

## Architecture

| Module | Responsibility |
|---|---|
| `fetcher.py` | Retrieves page HTML (local file or live via Canvas API + browser cookies) |
| `parser.py` | Extracts structured `NewsEntry` dicts from the News Feed section only |
| `detector.py` | Pure function that classifies change: `INITIALIZED`, `NEW_DATE`, `UPDATED_SAME_DATE`, or `NO_CHANGE` |
| `state_store.py` | Reads/writes `state.json` with atomic file operations |
| `notifier.py` | Sends email via SendGrid (`requests`); **HTML** is rendered with [React Email](https://react.email) in `email-render/` when Node deps are installed |
| `email-render/` | Small Node + TypeScript package: React Email template + CLI that prints email-safe HTML to stdout (invoked by `notifier.py`) |
| `main.py` | CLI: `check`, `poll`, `send-from-state` (resend from `state.json`), `ad-feedback-serve` / `ad-feedback-stats` / `ad-feedback-chart` (promo taps) |
| `ad_feedback_store.py` | SQLite persistence for optional “Was this ad helpful?” taps |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**HTML rendering (recommended):** Polished multipart HTML is produced by React Email. Install Node 18+ once, then:

```bash
cd email-render && npm ci && cd ..
```

If `email-render/node_modules` is missing, or rendering fails/timeouts, `notifier.py` automatically falls back to the previous simple HTML builder (SendGrid delivery is unchanged). To force legacy HTML only, set `SKIP_REACT_EMAIL_HTML=1` in the environment.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

You'll need:
- A **SendGrid account** (free tier works) and an API key from https://app.sendgrid.com/settings/api_keys
- A **verified sender** email address in SendGrid
- The email addresses of your classmates to notify

**Precedence:** For keys including API secrets, notify list/course/browser/poll, `SKIP_REACT_EMAIL_HTML`, `EMAIL_PROMO_*`, `AD_FEEDBACK_PUBLIC_URL`, `AD_FEEDBACK_DB_PATH`, and model keys, **values in `.env` override** shell exports once the Python process loads the file at startup. Other `.env` lines use “set if missing” (shell wins if already set).

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

**Smoke-test notifications to yourself only** (overrides `NOTIFY_EMAILS` for that run; repeat `--notify-email` for multiple addresses). With `--no-notify`, no mail is sent and this flag is ignored.

```bash
python main.py check --url https://umd.instructure.com/courses/1398395 \
  --notify-email you@example.com
```

### Continuous polling

```bash
# Poll every hour (default, reads config from .env)
python main.py poll

# Custom interval (in seconds)
python main.py poll --url https://umd.instructure.com/courses/1398395 --interval 1800

# Override recipients for every poll cycle (same semantics as `check --notify-email`)
python main.py poll --url https://umd.instructure.com/courses/1398395 --notify-email you@example.com

# Run in background
nohup python main.py poll > monitor.log 2>&1 &
```

Stop polling with Ctrl+C (it finishes the current cycle cleanly).

Environment is read once at startup. After changing `.env`, **restart** a long‑running `python main.py poll` (or redeploy whatever runs the notifier) so new values apply.

You can also run `chmod +x scripts/run_poll.sh` once, then `./scripts/run_poll.sh` (extra args pass through, e.g. `--interval 1800`). Handy for cron or a Terminal shortcut.

### Resend mail from disk state

To send **one more** notification with the **currently saved bulletin** in `state.json` (does not fetch Canvas that run)—useful after tweaking promo/feedback URLs or testing layout:

```bash
python main.py send-from-state
```

Uses `NOTIFY_EMAILS` from `.env`. Override recipients **for that send only**:

```bash
python main.py send-from-state --notify-email you@school.edu --notify-email friend@school.edu
```

If `last_change_type` is missing from state, pass `--change-type NEW_DATE` or `UPDATED_SAME_DATE`.

### Optional: promo + ad feedback taps

Optional **`EMAIL_PROMO_TEXT`** in `.env` appends a Thinkex-style promo strip with inline logo links (see `.env.example`). When **`AD_FEEDBACK_PUBLIC_URL`** is also set to an **HTTPS** base you control (typically **ngrok** forwarding **`python main.py ad-feedback-serve`** on port **8765**), the email includes a neutral “Was this ad helpful?” block (Yes / Meh / No links). **`AD_FEEDBACK_DB_PATH`** defaults to **`data/ad_feedback.sqlite`** relative to cwd.

- **`python main.py ad-feedback-stats`** — ASCII tally in terminal (run after votes).
- **`python main.py ad-feedback-chart`** — default HTML file avoids publishing vote totals; **`--full`** adds numeric bar breakdown (operators only).

Thank-you **`/thanks`** after a tap is intentionally generic (**no totals** exposed to respondents). Restart **`poll`** whenever **`AD_FEEDBACK_PUBLIC_URL`** changes (free ngrok hostnames rotate when the tunnel restarts).

## Production checklist

- **Config:** Fill `.env` from `.env.example`: `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL` (verified in SendGrid), `NOTIFY_EMAILS` (comma-separated addresses; no spaces around commas is safest), `CANVAS_COURSE_URL`, and optionally `BROWSER` (e.g. `comet`) and `POLL_INTERVAL` (seconds; default `3600` = one hour).
- **Run:** From the project directory with the venv activated: `python main.py poll`.
- **Background:** `nohup python main.py poll >> monitor.log 2>&1 &` keeps polling if you close the terminal. For login-time start, use **launchd** (macOS) or schedule `main.py check` with **cron** if you prefer not to leave a long-running process.
- **First run:** Baseline state is saved with change type `INITIALIZED` — **no email is sent**. Emails only go out on `NEW_DATE` or `UPDATED_SAME_DATE`.
- **React Email + TL;DR:** Run `cd email-render && npm ci` so sends use the styled template (TL;DR appears in the light-red summary card when `GEMINI_API_KEY` is set). Optional **`EMAIL_PROMO_TEXT`** adds a promo callout after the bulletin (see `.env.example`); the **first** `http(s)` URL in that line is the single CTA for the logo and every promo link. Optional **`EMAIL_PROMO_LOGO_URL`** sets the **logo image** only (not the link). Optional **`AD_FEEDBACK_PUBLIC_URL`** + **`python main.py ad-feedback-serve`** (and usually **ngrok**) enable the questionnaire links; tally with **`python main.py ad-feedback-stats`**. Logs show **`Email HTML: using React Email layout (validated)`** on success, or **`Email HTML: using legacy template`** when Node deps are missing, `SKIP_REACT_EMAIL_HTML=1`, or validation fell back—see warnings on the same run.
- **Comet / Canvas:** Live fetches use cookies from the browser you set in `BROWSER`. Stay logged into Canvas there; renew the session if checks start failing. On macOS, approve keychain prompts once (e.g. **Always Allow**) so cookie decryption works.

## How it works

1. **Scoped parsing** — Only the "News feed" section is parsed. All surrounding Canvas markup is ignored.
2. **Normalized comparison** — Whitespace, formatting tags, and style attributes are stripped so cosmetic changes don't trigger false alerts.
3. **Two-level change detection** — Checks both the newest date and a content hash, catching new announcements as well as edits to the current day's entry.
4. **Atomic persistence** — State is written to a temp file and renamed, preventing corruption from crashes.
5. **Resilient polling** — Transient fetch/parse errors are logged and retried on the next cycle instead of crashing.

## Running tests

```bash
python3 -m pytest tests/ -v
```

Two tests exercise the real React Email ``tsx`` subprocess. In some **sandboxed** environments they are **skipped** (``tsx`` uses local IPC that can return ``listen EPERM``). Run the same command in a normal terminal or CI to execute them with no skips.
