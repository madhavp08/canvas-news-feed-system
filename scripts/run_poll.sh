#!/usr/bin/env bash
# Run the Canvas News Feed monitor in continuous poll mode.
# Usage: ./scripts/run_poll.sh
#        ./scripts/run_poll.sh --interval 1800
# Cron example: 0 * * * * /path/to/canvas-news-feed-system/scripts/run_poll.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
else
  echo "run_poll.sh: .venv not found. Run: python3 -m venv .venv && pip install -r requirements.txt" >&2
  exit 1
fi

exec python main.py poll "$@"
