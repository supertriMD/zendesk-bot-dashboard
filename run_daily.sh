#!/bin/bash
# Daily refresh for the Zendesk bot dashboard: pull new conversations (+ tags/custom fields
# and the AI agents export), score them,
# and classify any new backlog. Every step is idempotent and re-runnable, so a
# missed day or a re-run never double-counts. Appends to logs/daily.log.
#
# Run manually to test:   ./run_daily.sh
# Scheduled via launchd:   see deploy/com.supertri.zendeskbot.daily.plist
set -uo pipefail
cd "$(dirname "$0")" || exit 1

PY="./.venv/bin/python"
mkdir -p logs
LOG="logs/daily.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
  echo "===== [$(ts)] daily run start ====="
  echo "[$(ts)] pull_conversations.py"
  if ! "$PY" pull_conversations.py; then
    echo "[$(ts)] PULL FAILED — skipping score/classify this run"
    echo "===== [$(ts)] daily run end (pull failed) ====="
    exit 1
  fi
  echo "[$(ts)] pull_bot_export.py"
  "$PY" pull_bot_export.py || echo "[$(ts)] BOT EXPORT step reported an error (continuing)"
  echo "[$(ts)] score_conversations.py"
  "$PY" score_conversations.py || echo "[$(ts)] SCORE step reported an error (continuing)"
  echo "[$(ts)] classify_backlog.py"
  "$PY" classify_backlog.py || echo "[$(ts)] CLASSIFY step reported an error"
  echo "===== [$(ts)] daily run end ====="
} >> "$LOG" 2>&1
