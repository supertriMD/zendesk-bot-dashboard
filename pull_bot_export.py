"""Pull the AI agents (Ultimate) data export: one row per bot conversation.

This closes the gap tickets can't: a chat the bot fully handles may never become a ticket,
so the ticket pull alone can't give a true bot containment rate. The export has every
conversation that ended on a given day, with Zendesk's resolution tier, automated-resolution
verdict, bot satisfaction and the intents/use cases that fired.

READ-ONLY. API: POST /ai-agents/api/data-export/v3/get-signed-urls with {"date": ...} returns
signed URLs to that day's immutable JSON file(s); files are built at midnight UTC, so the
latest complete day is yesterday. Docs:
https://developer.zendesk.com/documentation/ai-agents/getting-started/data-export/

Privacy: Zendesk exports no message text. `conversations_data` holds session parameters
(which can include contact details), so it is NOT stored; only its satisfaction keys are kept
(bsat_json) plus the list of key names, so we can see what else exists without holding it.

Credentials (from the AI agents dashboard ▸ Organization management): AI_AGENTS_API_KEY,
AI_AGENTS_BOT_ID, AI_AGENTS_ORG_ID. The export is per bot and we run two (email "TRI" and
messaging "Tri"), so AI_AGENTS_BOT_ID is a comma-separated list; every bot is pulled each day. If none is set the step SKIPS cleanly (exit 0), so the
daily job runs before they are configured; if some but not all are set, it fails loud.

Usage:
  python pull_bot_export.py                  # every day since the last one pulled, to yesterday
  python pull_bot_export.py --date 2026-09-29 [--date ...]   # specific day(s), re-pull allowed
  python pull_bot_export.py --probe --date 2026-09-29        # print field names/types, no writes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests

import config
import db

STATE_KEY = "bot_export_last_date"
_SATISFACTION_KEY = re.compile(r"bsat|csat|satisf|rating|feedback", re.I)

# export field -> our column, for the plain scalar fields (types coerced below).
_TEXT = {
    "conversation_id": "conversation_id", "platform_conversation_id": "platform_conversation_id",
    "bot_id": "bot_id", "bot_name": "bot_name", "channel": "channel", "language": "language",
    "conversation_type": "conversation_type", "conversation_status": "conversation_status",
    "resolution_tier": "resolution_tier", "last_resolution": "last_resolution",
    "automated_resolution_reasoning": "automated_resolution_reasoning",
}
_BOOL = {
    "automated_resolution": "automated_resolution", "is_llm_conversation": "is_llm_conversation",
    "has_knowledge_response_attempt": "has_knowledge_response_attempt", "test_mode": "test_mode",
}
_INT = {
    "bot_messages_count": "bot_messages_count", "visitor_messages_count": "visitor_messages_count",
    "not_understood_messages_count": "not_understood_messages_count",
    "knowledge_responseGenerated_count": "knowledge_response_generated_count",
    "knowledge_fallback_count": "knowledge_fallback_count",
    "knowledge_notUnderstood_count": "knowledge_not_understood_count",
    "knowledge_escalationRequired_count": "knowledge_escalation_required_count",
    "knowledge_errorOccurred_count": "knowledge_error_occurred_count",
}
_TIME = {"conversation_start_time": "conversation_start_time",
         "conversation_end_time": "conversation_end_time"}
_JSON = {
    "labels": "labels_json", "triggered_use_cases": "triggered_use_cases_json",
    "triggered_intent_replies": "triggered_intent_replies_json",
    "triggered_procedures": "triggered_procedures_json", "triggered_replies": "triggered_replies_json",
    "knowledge_sources": "knowledge_sources_json", "segments": "segments_json",
}


# --- Coercion (the export's value types are not fully documented: be tolerant, count misses) ---
_unmapped: Dict[str, int] = {}


def _miss(field: str) -> None:
    _unmapped[field] = _unmapped.get(field, 0) + 1


def _bool(field: str, v) -> Optional[bool]:
    if v is None or isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no", ""):
        return False
    _miss(field)
    return None


def _int(field: str, v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        _miss(field)
        return None


def _ts(field: str, v) -> Optional[str]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).isoformat()
    except ValueError:
        _miss(field)
        return None


def to_row(rec: dict, export_date: date, pulled_at: datetime) -> dict:
    row: Dict[str, object] = {}
    for src, col in _TEXT.items():
        v = rec.get(src)
        row[col] = None if v is None else str(v)
    for src, col in _BOOL.items():
        row[col] = _bool(src, rec.get(src))
    for src, col in _INT.items():
        row[col] = _int(src, rec.get(src))
    for src, col in _TIME.items():
        row[col] = _ts(src, rec.get(src))
    for src, col in _JSON.items():
        v = rec.get(src)
        row[col] = None if v in (None, [], {}) else json.dumps(v, sort_keys=True)
    data = rec.get("conversations_data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        sat = {k: v for k, v in data.items() if _SATISFACTION_KEY.search(k)}
        row["bsat_json"] = json.dumps(sat, sort_keys=True) if sat else None
        row["conversations_data_keys"] = sorted(data.keys())
    else:
        row["bsat_json"] = None
        row["conversations_data_keys"] = []
    row["export_date"] = export_date.isoformat()
    row["pulled_at"] = pulled_at
    return row


# --- API ---------------------------------------------------------------------
class BotExportClient:
    def __init__(self) -> None:
        config.require(config.AI_AGENTS_REQUIRED_VARS + [config.ZENDESK_SUBDOMAIN_VAR])
        self.url = (f"https://{os.environ[config.ZENDESK_SUBDOMAIN_VAR]}.zendesk.com"
                    "/ai-agents/api/data-export/v3/get-signed-urls")
        self.bot_ids = [b.strip() for b in os.environ[config.AI_AGENTS_BOT_ID_VAR].split(",") if b.strip()]
        self.headers = {
            "authorization": f"Bearer {os.environ[config.AI_AGENTS_API_KEY_VAR]}",
            "organizationId": os.environ[config.AI_AGENTS_ORG_ID_VAR],
            "Content-Type": "application/json",
        }

    def _post(self, day: date, bot_id: str) -> dict:
        for attempt in range(config.ZENDESK_MAX_RETRIES):
            resp = requests.post(self.url, headers={**self.headers, "botId": bot_id},
                                 json={"date": day.isoformat()}, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                print(f"  {resp.status_code} on {day}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise RuntimeError("AI agents export: 401 Unauthorized. The API key is wrong or "
                                   "revoked (it is shown once when generated; make a new one).")
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"AI agents export: giving up on {day} after retries")

    def records(self, day: date) -> List[dict]:
        """Every conversation record in that day's export file(s), across all bots."""
        out: List[dict] = []
        for bot_id in self.bot_ids:
            out.extend(self._records(day, bot_id))
        return out

    def _records(self, day: date, bot_id: str) -> List[dict]:
        out: List[dict] = []
        for url in self._post(day, bot_id).get("urls") or []:
            resp = requests.get(url, timeout=300)   # signed URL: no auth header
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
                out.extend(data if isinstance(data, list) else [data])
            except ValueError:                        # tolerate JSON Lines too
                out.extend(json.loads(line) for line in text.splitlines() if line.strip())
        return out


def _days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run(days: Optional[List[date]], probe: bool) -> None:
    present = [v for v in config.AI_AGENTS_REQUIRED_VARS if os.environ.get(v)]
    if not present and not probe:
        print("SKIP: AI agents export not configured (AI_AGENTS_API_KEY / _BOT_ID / _ORG_ID unset).")
        return
    client = BotExportClient()   # fails loud if only some are set
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

    if probe:
        for day in days or [yesterday]:
            recs = client.records(day)
            print(f"{day}: {len(recs)} conversation records")
            if recs:
                print("  fields:", {k: type(v).__name__ for k, v in sorted(recs[0].items())})
        print("No data was written.")
        return

    db.init_db()
    if not days:
        last = db.get_pull_state(STATE_KEY)
        start = (date.fromisoformat(last) + timedelta(days=1)) if last else date.fromisoformat(config.BOT_EXPORT_START)
        days = list(_days(start, yesterday))
        advance_state = True
    else:
        advance_state = False
    if not days:
        print("Bot export already current.")
        return

    pulled_at = datetime.now(timezone.utc)
    total = 0
    for day in days:
        rows = [to_row(r, day, pulled_at) for r in client.records(day)]
        rows = [r for r in rows if r.get("conversation_id")]
        n = db.upsert_bot_conversations(rows) if rows else 0
        total += n
        if advance_state:                   # only after the day is written: a crash re-pulls it
            db.set_pull_state(STATE_KEY, day.isoformat())
        if n:
            print(f"  {day}: {n}")
    print(f"Done. Upserted {total} bot conversations over {len(days)} day(s).")
    if _unmapped:
        print(f"  WARN: values that did not fit their type (stored NULL): {_unmapped}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description="Pull the AI agents data export (read-only).")
    p.add_argument("--date", action="append", default=None, help="YYYY-MM-DD; repeatable.")
    p.add_argument("--probe", action="store_true", help="Print record counts/field types; no writes.")
    a = p.parse_args()
    run([date.fromisoformat(d) for d in a.date] if a.date else None, a.probe)


if __name__ == "__main__":
    main()
