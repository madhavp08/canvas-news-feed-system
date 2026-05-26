# Canvas News Feed Monitor

Watch the **News feed** section of a Canvas / ELMS course page, detect new or edited announcements, and email a styled bulletin to your classmates — with an optional [Gemini](https://ai.google.dev/) TL;DR, a promo callout, and a "Was this ad helpful?" feedback questionnaire backed by a tiny SQLite server.

> **Stack:** Python 3.10+ · Node 18+ · BeautifulSoup + lxml · SendGrid v3 HTTP API · [React Email](https://react.email) · Google Gen AI SDK · SQLite · ngrok

```
Canvas page  →  fetcher.py  →  parser.py  →  detector.py  →  state.json
   (cookies)     (REST API)     (News feed)    (NEW_DATE /          │
                                                UPDATED_SAME_DATE)   ▼
                                                            notifier.py
                                                          ┌──────┴──────┐
                                                     React Email     SendGrid
                                                     (email-render)   v3 API
                                                          │              │
                                                     Gemini TL;DR    classmates' inboxes
                                                          │
                                                  ┌───────┴───────┐
                                               promo strip   "Was this ad helpful?"
                                                                    │
                                                              ngrok → ad-feedback-serve
                                                              (port 8765) → SQLite
```

## Table of contents

- [Quickstart](#quickstart)
- [What lands in the inbox](#what-lands-in-the-inbox)
- [Architecture](#architecture)
- [Setup](#setup)
  - [1. Install dependencies](#1-install-dependencies)
  - [2. Configure `.env`](#2-configure-env)
  - [3. Bring up the ad-feedback server + ngrok tunnel](#3-bring-up-the-ad-feedback-server--ngrok-tunnel)
- [Usage](#usage)
  - [Commands at a glance](#commands-at-a-glance)
  - [One-shot check](#one-shot-check)
  - [Continuous polling](#continuous-polling)
  - [Resend mail from disk state](#resend-mail-from-disk-state)
  - [Inspect ad-feedback votes](#inspect-ad-feedback-votes)
- [`.env` reference](#env-reference)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Tests](#tests)

## Quickstart

```bash
# 1. Clone and enter the repo
git clone <this-repo> && cd canvas-news-feed-system

# 2. Python + Node dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd email-render && npm ci && cd ..

# 3. Fill in your secrets / recipients
cp .env.example .env && $EDITOR .env

# 4. Smoke-test against a saved page, no email sent
python main.py check --html-file 216HomePage.html --no-notify

# 5. Smoke-test the email pipeline to yourself only
python main.py check --html-file 216HomePage.html --notify-email you@example.com
```

To run for real against live Canvas, see [Setup](#setup) below — you'll also need to bring up the ad-feedback server + ngrok tunnel so the questionnaire links in each email actually resolve.

## What lands in the inbox

Each notification email contains, in order:

1. **TL;DR card** — 2–4 short bullets summarized by Gemini (only when `GEMINI_API_KEY` is set).
2. **Bulletin** — every line from the matched News-feed date, hyperlinks preserved.
3. **Promo strip** — your logo + a single-CTA line (only when `EMAIL_PROMO_TEXT` is set).
4. **"Was this ad helpful?" questionnaire** — Yes / Meh / No links that record a tap and redirect to a generic thank-you page (only when both `EMAIL_PROMO_TEXT` and `AD_FEEDBACK_PUBLIC_URL` are set).

Subject lines follow `CMSC216 News Feed update — <date>` for new posts and `CMSC216 News Feed edited — <date>` for in-place edits. (The `CMSC216` prefix is hard-coded in `notifier._build_subject`; swap it there if you adapt this for another course.)

## Architecture

| Module | Responsibility |
|---|---|
| `fetcher.py` | Retrieves page HTML — local file, or the Canvas REST `front_page` endpoint with cookies decrypted from your browser's keychain. 8-attempt exponential backoff on transient network errors. |
| `parser.py` | Locates the `News feed` heading and converts each `<h3>` date + `<ol>` bullet block into a `NewsEntry` dict with structured link segments and a stable SHA-256 content hash. |
| `detector.py` | Pure function comparing the newest entry against persisted state. Emits `INITIALIZED`, `NEW_DATE`, `UPDATED_SAME_DATE`, or `NO_CHANGE`. |
| `state_store.py` | Atomic JSON persistence (`state.json`) using `tempfile` + `os.replace`. |
| `notifier.py` | Builds plain + HTML bodies, calls the React Email CLI subprocess, optionally fetches a Gemini TL;DR, then POSTs to SendGrid's v3 `mail/send` API with one personalization per recipient (so addresses stay private). |
| `email-render/` | Node + TypeScript package: React Email template + a `tsx` CLI (`src/render-cli.tsx`) that reads JSON props from stdin and writes the final HTML to stdout. |
| `ad_feedback_store.py` | SQLite schema + `vote_href()` helper. |
| `ad_feedback_cli.py` | Threading HTTP server (`/vote`, `/thanks`) and chart/stats renderers. |
| `main.py` | Argparse front end — subcommands `check`, `poll`, `send-from-state`, and the `ad-feedback-{serve,stats,chart}` shortcuts. |
| `scripts/run_poll.sh` | Helper that activates the venv and execs `python main.py poll` (handy for cron). |

## Setup

The system has three setup steps. Step 3 brings up the local ngrok-tunneled feedback server that the "Was this ad helpful?" links in each email point at — skip it if you don't intend to use the questionnaire (the rest of the email still renders).

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd email-render && npm ci && cd ..
```

> **Node is mandatory.** `notifier.py` shells out to `email-render/node_modules/.bin/tsx` on every send. Without that binary, the poll loop starts with a warning (`React Email CLI missing — run: cd email-render && npm ci`) and emails won't render correctly. Node 18+ is required.

### 2. Configure `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in at least the four required keys (see the [full reference](#env-reference)):

| Key | Required | What to put |
|---|---|---|
| `SENDGRID_API_KEY` | Yes | Generate at https://app.sendgrid.com/settings/api_keys (free tier is fine). |
| `SENDGRID_FROM_EMAIL` | Yes | Must be a **verified sender** in your SendGrid account. |
| `NOTIFY_EMAILS` | Yes | Comma-separated list (no spaces around commas is safest). |
| `CANVAS_COURSE_URL` | Yes | `https://<your-canvas-host>/courses/<id>`. |

The poll loop reads `.env` once at startup. **Restart `python main.py poll` after any `.env` change.**

### 3. Bring up the ad-feedback server + ngrok tunnel

The "Was this ad helpful?" links in each email point at a tiny `ThreadingHTTPServer` (`python main.py ad-feedback-serve`, port `8765`) that writes votes to `data/ad_feedback.sqlite`. SendGrid recipients can only reach that server through a public HTTPS URL, so the production setup pairs it with an [ngrok](https://ngrok.com/download) tunnel.

**One-time:** install ngrok and authenticate it (free account is fine):

```bash
ngrok config add-authtoken <your_token>
```

**Every run, in two terminals:**

```bash
# Terminal A — local feedback server
python main.py ad-feedback-serve
# 2026-01-01 00:00:00 INFO  Ad feedback server on http://127.0.0.1:8765 (DB: data/ad_feedback.sqlite)
```

```bash
# Terminal B — public HTTPS tunnel
ngrok http 8765
# Forwarding  https://<random>.ngrok-free.app -> http://127.0.0.1:8765
```

Copy the `https://<random>.ngrok-free.app` URL into `.env`:

```env
AD_FEEDBACK_PUBLIC_URL=https://<random>.ngrok-free.app
```

Then start (or restart) `python main.py poll` so the new URL is picked up.

> **Free ngrok hostnames rotate** every time the tunnel restarts. Whenever you restart `ngrok`, update `AD_FEEDBACK_PUBLIC_URL` in `.env` and restart `python main.py poll`. The questionnaire is also silently disabled if `EMAIL_PROMO_TEXT` is unset, so you need both env vars to see the Yes/Meh/No strip.

The `/thanks` page shown to respondents is intentionally generic — no tally is ever exposed to the public, even via `ad-feedback-chart` by default.

## Usage

### Commands at a glance

| Command | What it does |
|---|---|
| `python main.py check --url <course_url>` | Run one fetch → parse → detect → notify cycle. |
| `python main.py check --html-file page.html` | Same, but read HTML from disk (no Canvas hit). |
| `python main.py poll` | Loop on `POLL_INTERVAL`. Cookies are cached across cycles; the loop survives transient errors. |
| `python main.py send-from-state` | Re-render and resend the bulletin currently in `state.json` (no Canvas fetch). |
| `python main.py ad-feedback-serve` | HTTP server on `127.0.0.1:8765` recording `/vote?choice=yes|meh|no` to SQLite. |
| `python main.py ad-feedback-stats` | ASCII bar chart of votes in your terminal. |
| `python main.py ad-feedback-chart [--full]` | Write `ad-feedback-chart.html`. Hides totals by default; `--full` includes counts (operators only). |

Every command supporting recipients accepts `--notify-email EMAIL` (repeatable) to override `NOTIFY_EMAILS` for that one run, and `--no-notify` (where it makes sense) to skip the email entirely.

### One-shot check

```bash
# Live check using Comet browser cookies
python main.py check --url https://umd.instructure.com/courses/1398395

# Using a saved HTML file (for testing)
python main.py check --html-file 216HomePage.html

# Skip email notification entirely
python main.py check --url https://umd.instructure.com/courses/1398395 --no-notify

# Smoke-test the full email pipeline to yourself only
python main.py check --url https://umd.instructure.com/courses/1398395 \
  --notify-email you@example.com
```

`--notify-email` is ignored when `--no-notify` is also passed.

### Continuous polling

```bash
# Default: every hour, reads CANVAS_COURSE_URL from .env
python main.py poll

# Custom interval (in seconds) and explicit URL
python main.py poll --url https://umd.instructure.com/courses/1398395 --interval 1800

# Override recipients for every cycle
python main.py poll --notify-email you@example.com

# Run detached
nohup python main.py poll >> monitor.log 2>&1 &
```

Ctrl+C (or `SIGTERM`) cleanly drains the current cycle and exits.

You can also run `chmod +x scripts/run_poll.sh` once, then `./scripts/run_poll.sh` (extra args pass through, e.g. `--interval 1800`). Handy for cron or a Terminal shortcut — it auto-activates the venv and execs `python main.py poll`.

### Resend mail from disk state

After tweaking promo / feedback URLs or testing layout, you can send one more notification using the bulletin that's already in `state.json` (no Canvas fetch):

```bash
python main.py send-from-state
```

Override recipients for that send only:

```bash
python main.py send-from-state \
  --notify-email you@school.edu --notify-email friend@school.edu
```

If `last_change_type` is missing from state (e.g. you ran the very first `check` with `--no-notify`), pass `--change-type NEW_DATE` or `--change-type UPDATED_SAME_DATE`.

### Inspect ad-feedback votes

```bash
# Quick terminal view
python main.py ad-feedback-stats
# Was this ad helpful?
#   Total votes: 7
#   Yes 😃   │█████████░░░░░░░░░░░░░░░░░░░░░░░░░░░│    3 (42.9%)
#   Meh 😐   │█████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│    2 (28.6%)
#   No 😔    │█████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│    2 (28.6%)

# Write a sharable HTML file (totals hidden by default)
python main.py ad-feedback-chart -o /tmp/feedback.html

# Same, but include numeric breakdown — operators only, don't publish
python main.py ad-feedback-chart --full -o /tmp/feedback-full.html
```

The default chart and the `/thanks` redirect both deliberately omit counts so respondents never see how others voted.

## `.env` reference

| Key | Required | Default | Notes |
|---|---|---|---|
| `SENDGRID_API_KEY` | Yes | — | SendGrid v3 API key. |
| `SENDGRID_FROM_EMAIL` | Yes | — | Verified sender. |
| `NOTIFY_EMAILS` | Yes | — | Comma-separated. Each address gets its own SendGrid personalization, so recipients can't see each other. |
| `CANVAS_COURSE_URL` | Yes | — | `https://<host>/courses/<id>`. |
| `BROWSER` | No | `comet` | Cookie source. One of `chrome`, `firefox`, `brave`, `chromium`, `comet`. |
| `POLL_INTERVAL` | No | `3600` | Seconds between `poll` cycles. |
| `GEMINI_API_KEY` | No | — | When set, emails open with a TL;DR card. Get one at https://aistudio.google.com/app/apikey. |
| `GEMINI_MODEL` | No | `gemini-2.5-flash-lite` | Flash-Lite is the fastest tier; override only if you want different behavior. |
| `EMAIL_PROMO_TEXT` | No | — | One-line promo appended to every bulletin. The **first** `http(s)` URL in the line becomes the single CTA for the logo and every linkified URL. Plain text only — no HTML markup is interpreted, and don't wrap values in quotes (they appear verbatim). |
| `EMAIL_PROMO_LOGO_URL` | No | bundled Thinkex SVG | `<img src>` for the promo strip's logo. Click-through always uses the promo CTA, never this. |
| `AD_FEEDBACK_PUBLIC_URL` | No | — | Public **HTTPS** base where the feedback server is reachable (your ngrok URL). Required for the "Was this ad helpful?" strip. Ignored if `EMAIL_PROMO_TEXT` is unset. |
| `AD_FEEDBACK_DB_PATH` | No | `data/ad_feedback.sqlite` | SQLite path relative to cwd. |

> **Precedence:** for every key in the table above (and a few internal flags), the value in `.env` **overrides** anything already set in the shell environment when the Python process starts. Anything *not* in that allowlist uses "set if missing" semantics (shell wins if already exported). This protects you from a stale `EXPORT SENDGRID_API_KEY=...` in a shell rc file silently shadowing the project's `.env`.

## How it works

1. **Cookie-based Canvas fetch.** Canvas course wiki pages are rendered client-side, so a plain GET returns an empty shell. `fetcher.py` instead calls `https://<host>/api/v1/courses/<id>/front_page`, which returns JSON with a `body` field containing the same HTML the browser would inject. Session cookies are decrypted out of your browser's keychain — Comet uses the same Chromium PBKDF2(`saltysalt`, 1003 iters) → AES-128 scheme as Chrome/Brave, but with its own `Comet Safe Storage` keychain entry.
2. **Scoped parsing.** `parser.py` looks for the `News feed` heading and only walks its siblings until it hits another top-level heading. Each date `<h3>` is paired with the following `<ol>`; bullets are emitted as structured segments (text + link with absolute `href`).
3. **Two-level change detection.** A SHA-256 of the canonicalized bullet text is stored alongside the normalized date. A different date means a new post (`NEW_DATE`); same date + different hash means an edit (`UPDATED_SAME_DATE`); same on both means no email.
4. **Atomic persistence.** `state.json` is written via `tempfile.mkstemp` in the same directory then `os.replace`d into place, so a crash mid-write can never corrupt it.
5. **Resilient polling.** Transient `ConnectionError` / `Timeout` / chunked-transfer errors trigger up to 8 retries with exponential backoff (1s → 60s). Cookies are loaded once per process and re-read on `401`/`403` only. Anything else is logged and retried on the next cycle.
6. **React Email rendering.** `notifier.py` builds a JSON props bundle, pipes it into the `tsx` CLI in `email-render/`, and validates the returned HTML — the rendered output must start with `<`, weigh enough vs. the plaintext body, contain the announcement date, and contain a non-trivial substring from the first bullet. Failing any of those, a legacy inline-styled fallback is used so SendGrid still receives a readable body.
7. **Optional TL;DR.** With `GEMINI_API_KEY` set, the bulletin is serialized (bounded at 12,000 chars), sent to `gemini-2.5-flash-lite` with a system prompt instructing 2–4 short bullets, and the response is normalized into the React Email TL;DR card.
8. **Per-recipient send.** Each address gets its own SendGrid personalization in batches of up to 1,000 — no recipient sees the others, and one bad address doesn't poison the rest of the send.

## Troubleshooting

<details>
<summary><strong>The keychain dialog never appears / Comet password lookup times out.</strong></summary>

Run from a real Terminal (not from inside an IDE — IDE-launched subprocesses often don't get permission to display modal keychain prompts). When the dialog appears, click **Always Allow** so future runs don't prompt again. Then re-run the same command.

</details>

<details>
<summary><strong>Canvas returns 401/403, or the API returns HTML instead of JSON.</strong></summary>

Your session has expired. Log into Canvas in whichever browser you set in `BROWSER` and try again. The `poll` loop auto-retries once with refreshed cookies on auth failures, so this typically self-heals on the next cycle as long as the browser session is valid.

</details>

<details>
<summary><strong>Logs say <code>React Email CLI missing</code>.</strong></summary>

`email-render/node_modules` is absent. Run:

```bash
cd email-render && npm ci && cd ..
```

then restart `python main.py poll`. You should see `React Email CLI available — notifications will use the styled template…` on the next startup.

</details>

<details>
<summary><strong>Recipients see broken Yes/Meh/No links (DNS failure or 404).</strong></summary>

The ngrok tunnel has rotated its hostname or the `ad-feedback-serve` process is down. Restart both, copy the new `https://…ngrok-free.app` URL into `AD_FEEDBACK_PUBLIC_URL`, and restart `python main.py poll` so it picks up the new value.

</details>

<details>
<summary><strong>Repeated <code>Network error after 8 attempts (DNS/connectivity)</code> in the poll log.</strong></summary>

This is logged at `WARNING` (not `ERROR`) on purpose — the loop is designed to ride out flaky Wi-Fi and laptop sleep cycles. If it persists for multiple cycles, check `ping umd.instructure.com` and confirm your machine isn't on a captive-portal network.

</details>

<details>
<summary><strong>I want to disable a particular feature.</strong></summary>

- **TL;DR card:** unset / delete `GEMINI_API_KEY`.
- **Promo strip + feedback questionnaire:** unset `EMAIL_PROMO_TEXT` (the feedback strip is silently disabled when promo text is absent).
- **Feedback questionnaire only:** unset `AD_FEEDBACK_PUBLIC_URL`.
- **Skip email for one cycle:** pass `--no-notify` to `check` or `poll`.

</details>

## Project layout

```
canvas-news-feed-system/
├── main.py                    # CLI entry point
├── fetcher.py                 # Canvas REST + browser cookie decryption
├── parser.py                  # BeautifulSoup → NewsEntry
├── detector.py                # Pure change classifier
├── state_store.py             # Atomic state.json read/write
├── notifier.py                # SendGrid + Gemini + promo + feedback wiring
├── ad_feedback_store.py       # SQLite schema + vote URL helper
├── ad_feedback_cli.py         # Vote HTTP server + chart renderers
├── email-render/              # React Email template (Node 18+)
│   ├── package.json
│   └── src/
│       ├── render-cli.tsx     # Reads JSON props from stdin, writes HTML to stdout
│       └── emails/
│           ├── NewsFeedEmail.tsx
│           ├── EmailSummaryCard.tsx
│           └── types.ts
├── scripts/run_poll.sh        # venv-activating wrapper for cron / shortcuts
├── data/ad_feedback.sqlite    # Created on first vote (gitignored)
├── state.json                 # Persistent monitor state (gitignored)
├── requirements.txt
├── .env.example               # Annotated template — copy to .env
└── tests/                     # pytest suite
```

## Tests

```bash
python3 -m pytest tests/ -v
```

The suite covers the parser, detector, state store, fetcher retry policy, ad-feedback store, the `main.py` argparse front end, and the notifier. Two notifier tests (`test_subprocess_react_email_passes_content_checks` and `test_subprocess_react_email_promo_before_footer`) exercise the **real** React Email `tsx` subprocess — they auto-`skip` if `email-render/node_modules` is missing or if the local `tsx` IPC pipe is blocked (a common sandboxed-runner failure mode that surfaces as `listen EPERM`). Run the same command in a normal terminal or CI to execute them with no skips.
