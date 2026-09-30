#!/usr/bin/env bash
# Cloud Run Job entrypoint for the daily Zendesk refresh. The same three idempotent steps as
# run_daily.sh (pull -> score -> classify), but logged to stdout for Cloud Logging instead of
# logs/daily.log, and FAIL-LOUD: any failed step exits non-zero, so the execution shows red in
# Cloud Run rather than looking green with an error buried in a log file. A retry is safe: the
# pull resumes from the saved cursor and score/classify only pick up rows not yet done.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY="${PY:-python}"
ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
rc=0

echo "[zendesk-job] $(ts) start (image ${ZENDESK_JOB_GIT_SHA:-unknown})"
echo "[zendesk-job] $(ts) pull_conversations.py"
if ! "$PY" pull_conversations.py; then
  echo "[zendesk-job] $(ts) PULL FAILED: skipping score/classify"
  exit 1
fi
echo "[zendesk-job] $(ts) score_conversations.py"
"$PY" score_conversations.py || { echo "[zendesk-job] $(ts) SCORE FAILED"; rc=1; }
echo "[zendesk-job] $(ts) classify_backlog.py"
"$PY" classify_backlog.py || { echo "[zendesk-job] $(ts) CLASSIFY FAILED"; rc=1; }
echo "[zendesk-job] $(ts) end (rc=$rc)"
exit "$rc"
