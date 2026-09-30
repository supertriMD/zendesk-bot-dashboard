"""Stage 1 — pull Zendesk conversations into the raw store.

READ-ONLY against Zendesk: this module issues GET requests only. It never
replies to, modifies, closes, or tags a ticket.

Design:
  * Incremental via Zendesk's cursor-based Incremental Ticket Export. A
    high-water cursor is saved in pull_state, so re-runs pick up only new/changed
    tickets. Writes are upserts (idempotent) so overlap or a re-run never
    double-counts.
  * Assembles full_text from a ticket's public comments, role-labelled
    (User: / Bot: / Agent:). Bot turns are identified from configurable author
    ids/name hints — use `--probe` first to discover how YOUR bot is marked.
  * Handles pagination and rate limits (honours Retry-After on 429).

Usage:
  python pull_conversations.py --probe [--limit 20]   # inspect, no DB writes
  python pull_conversations.py [--since 2026-04-15] [--limit N] [--reset]
  python pull_conversations.py --attributes-only --since 2024-01-01   # backfill tags/fields

Every pulled ticket also writes a ticket_attributes row: its tags and custom-field values
(incl. the AI agent's Resolution tier and the event field). Free-text custom fields are never
stored. --attributes-only walks the ticket export WITHOUT fetching comments, so a history
backfill is cheap and leaves the saved conversations cursor alone.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

import config
import db

# Pull-state keys
CURSOR_KEY = "tickets_after_cursor"
ATTR_BACKFILL_KEY = "attributes_backfill_cursor"

# Bot identification.
# Discovered from the data: the bot ("Tri") posts as Zendesk's system/automation
# user, author_id == -1 (email replies via api, and the web chat_transcript).
# Human agents post under their real admin ids, so -1 on a public comment = the bot.
BOT_AUTHOR_IDS = {"-1"} | {
    x.strip() for x in os.environ.get("BOT_AUTHOR_IDS", "").split(",") if x.strip()
}
BOT_NAME_HINT = os.environ.get("BOT_NAME_HINT", "bot").strip().lower()
# The bot's display name inside web chat transcripts (used to relabel speakers).
BOT_TRANSCRIPT_NAME = os.environ.get("BOT_TRANSCRIPT_NAME", "Tri")


# --- Zendesk client (GET-only) -------------------------------------------------
class ZendeskClient:
    """Thin read-only Zendesk API client with pagination + rate-limit handling."""

    def __init__(self) -> None:
        config.require(config.ZENDESK_REQUIRED_VARS)
        subdomain = os.environ[config.ZENDESK_SUBDOMAIN_VAR]
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self._auth = HTTPBasicAuth(
            f"{os.environ[config.ZENDESK_EMAIL_VAR]}/token",
            os.environ[config.ZENDESK_API_TOKEN_VAR],
        )
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get(
        self, path: str, params: Optional[dict] = None, allow_status: Tuple[int, ...] = ()
    ) -> Optional[dict]:
        """GET with retry/backoff. `path` may be a full URL or a /api/v2-relative path.

        Statuses in allow_status return None instead of raising (e.g. 404 for a
        deleted ticket, so the caller can skip and continue).
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(config.ZENDESK_MAX_RETRIES):
            resp = self._session.get(url, params=params, auth=self._auth, timeout=60)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "60"))
                print(f"  429 rate-limited; waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"  {resp.status_code} server error; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code in allow_status:
                return None
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Giving up on {url} after {config.ZENDESK_MAX_RETRIES} retries")

    def iter_incremental_tickets(
        self, start_time: Optional[int], cursor: Optional[str]
    ) -> Iterator[Tuple[dict, Optional[str]]]:
        """Yield (ticket, after_cursor) from the cursor-based incremental export.

        Start from a saved cursor if we have one, else from start_time (unix).
        """
        path = "/incremental/tickets/cursor.json"
        params: dict = {"cursor": cursor} if cursor else {"start_time": start_time}
        while True:
            payload = self.get(path, params=params)
            after_cursor = payload.get("after_cursor")
            for ticket in payload.get("tickets", []):
                yield ticket, after_cursor
            if payload.get("end_of_stream"):
                return
            if not after_cursor:
                return
            params = {"cursor": after_cursor}

    def fetch_ticket_fields(self) -> List[dict]:
        """All ticket field definitions (id, title, type, options). Read once per run."""
        fields: List[dict] = []
        url: Optional[str] = f"{self.base_url}/ticket_fields.json"
        params: Optional[dict] = {"page[size]": 100}
        while url:
            payload = self.get(url, params=params)
            fields.extend(payload.get("ticket_fields", []))
            url = (payload.get("links") or {}).get("next") if (payload.get("meta") or {}).get("has_more") else None
            params = None
        return fields

    def fetch_comments(
        self, ticket_id
    ) -> Tuple[Optional[List[dict]], Dict[str, dict]]:
        """Return (comments, users_by_id). users sideloaded to get author roles.

        Returns (None, {}) if the ticket 404s (deleted/inaccessible) so the
        caller can skip it rather than store an empty conversation.
        """
        comments: List[dict] = []
        users_by_id: Dict[str, dict] = {}
        path = f"/tickets/{ticket_id}/comments.json"
        params: Optional[dict] = {"include": "users", "page[size]": 100}
        url = f"{self.base_url}{path}"
        while url:
            payload = self.get(url, params=params, allow_status=(404,))
            if payload is None:
                return None, {}
            comments.extend(payload.get("comments", []))
            for user in payload.get("users", []):
                users_by_id[str(user["id"])] = user
            url = payload.get("next_page")
            params = None  # next_page is a full URL with params baked in
        return comments, users_by_id


# --- Assembly helpers ----------------------------------------------------------
def normalize_channel(via_channel: Optional[str]) -> str:
    """Map Zendesk via.channel to our enum: 'email' | 'web' | 'messaging'.

    Unknown values pass through raw so --probe surfaces anything unmapped.
    """
    raw = (via_channel or "").lower()
    mapping = {
        "email": "email",
        "native_messaging": "messaging",
        "messaging": "messaging",
        "chat": "web",
        "web": "web",
        "web_form": "web",
        "web_service": "web",
        "helpcenter": "web",
    }
    if raw in mapping:
        return mapping[raw]
    # Fall back to keyword matching so new Zendesk channel strings still classify.
    if "messaging" in raw:
        return "messaging"
    if "mail" in raw:
        return "email"
    if any(k in raw for k in ("web_widget", "widget", "answer_bot", "web", "chat")):
        return "web"
    return raw or "unknown"


def _role_label(author_id, user: Optional[dict]) -> str:
    """Classify a comment author as 'Bot', 'Agent', or 'User'."""
    if str(author_id) in BOT_AUTHOR_IDS:
        return "Bot"
    role = (user or {}).get("role", "end-user")
    name = ((user or {}).get("name") or "").lower()
    if role in ("agent", "admin"):
        # A human agent unless the name marks it as the bot.
        if BOT_NAME_HINT and BOT_NAME_HINT in name:
            return "Bot"
        return "Agent"
    return "User"


def normalize_transcript(text: str) -> str:
    """Relabel a web chat transcript's speakers to the uniform Bot:/User: format.

    Transcripts arrive as one blob like:
        (16:59:33) Tri: Hi, I'm Tri...  (16:59:49) Web User 6a4f...: my question
    We rewrite the bot's name to 'Bot:' and 'Web User <id>:' to 'User:' so the
    judge sees the same format as email conversations. Human agents who join keep
    their own name.
    """
    text = re.sub(rf"\b{re.escape(BOT_TRANSCRIPT_NAME)}:", "Bot:", text)
    text = re.sub(r"Web User(?: [0-9a-fA-F]+)?:", "User:", text)
    return text


class FieldMap:
    """Maps custom-field ids to titles, option values to display names, and the strategic
    fields (config.TICKET_FIELD_COLUMNS) to their named columns. Built from the live field
    definitions each run, so nothing depends on a hardcoded field id."""

    def __init__(self, fields: List[dict]) -> None:
        norm = lambda t: " ".join((t or "").split()).lower()
        by_title = {norm(title): col for col, title in config.TICKET_FIELD_COLUMNS.items()}
        self.title: Dict[int, str] = {}
        self.column: Dict[int, str] = {}
        self.options: Dict[int, Dict[str, str]] = {}
        self.stored: set = set()
        for f in fields:
            fid = f["id"]
            self.title[fid] = " ".join((f.get("title") or "").split())
            if f.get("type") not in config.TICKET_FIELD_FREE_TEXT_TYPES:
                self.stored.add(fid)
            if norm(f.get("title")) in by_title:
                self.column[fid] = by_title[norm(f.get("title"))]
            self.options[fid] = {o.get("value"): o.get("name") for o in f.get("custom_field_options") or []}
        missing = set(config.TICKET_FIELD_COLUMNS) - set(self.column.values())
        if missing:
            print(f"  WARN: no Zendesk ticket field matches {sorted(missing)} "
                  "(renamed? update config.TICKET_FIELD_COLUMNS)", file=sys.stderr)

    def display(self, fid: int, value):
        if isinstance(value, list):
            return [self.options.get(fid, {}).get(v, v) for v in value]
        return self.options.get(fid, {}).get(value, value)


def assemble_attributes(ticket: dict, fields: FieldMap, pulled_at: datetime) -> dict:
    """Build a ticket_attributes row: tags + non-free-text custom fields, never comment text."""
    named: Dict[str, Optional[str]] = {col: None for col in config.TICKET_FIELD_COLUMNS}
    other: Dict[str, object] = {}
    for cf in ticket.get("custom_fields") or []:
        fid, value = cf.get("id"), cf.get("value")
        if fid not in fields.stored or value in (None, "", []):
            continue
        shown = fields.display(fid, value)
        if fid in fields.column:
            named[fields.column[fid]] = ", ".join(map(str, shown)) if isinstance(shown, list) else str(shown)
        else:
            other[fields.title.get(fid, str(fid))] = shown
    sat = (ticket.get("satisfaction_rating") or {}).get("score")
    return {
        "conversation_id": str(ticket["id"]),
        "via_channel": (ticket.get("via") or {}).get("channel"),
        "ticket_form_id": str(ticket["ticket_form_id"]) if ticket.get("ticket_form_id") else None,
        "brand_id": str(ticket["brand_id"]) if ticket.get("brand_id") else None,
        "priority": ticket.get("priority"),
        "ticket_type": ticket.get("type"),
        "satisfaction_score": sat if sat not in (None, "unoffered") else None,
        "tags": list(ticket.get("tags") or []),
        **named,
        "custom_fields_json": json.dumps(other, sort_keys=True) if other else None,
        "ticket_created_at": ticket.get("created_at"),
        "ticket_updated_at": ticket.get("updated_at"),
        "pulled_at": pulled_at,
    }


def assemble_row(
    ticket: dict, comments: List[dict], users_by_id: Dict[str, dict], pulled_at: datetime
) -> dict:
    """Build a conversations row from a ticket and its comments."""
    public_comments = [c for c in comments if c.get("public", True)]
    lines: List[str] = []
    ended_with_human = False
    bot_participated = False
    for c in public_comments:
        via_channel = (c.get("via") or {}).get("channel")
        body = html.unescape((c.get("plain_body") or c.get("body") or "").strip())
        if via_channel == "chat_transcript":
            # A whole web-chat dialogue in one comment, already speaker-labelled.
            lines.append(normalize_transcript(body))
            bot_participated = True
            continue
        user = users_by_id.get(str(c.get("author_id")))
        label = _role_label(c.get("author_id"), user)
        if label == "Agent":
            ended_with_human = True
        elif label == "Bot":
            bot_participated = True
        lines.append(f"{label}: {body}")

    full_text = "\n".join(lines)
    # Count role-labelled turns (works for both transcripts and comment threads).
    turn_count = sum(full_text.count(f"{r}:") for r in ("User", "Bot", "Agent"))

    sat = ticket.get("satisfaction_rating") or {}
    score = sat.get("score")
    user_rating = score if score in ("good", "bad") else None

    return {
        "conversation_id": str(ticket["id"]),
        "channel": normalize_channel((ticket.get("via") or {}).get("channel")),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "full_text": full_text,
        "turn_count": turn_count,
        "bot_participated": bot_participated,
        "ended_with_human": ended_with_human,
        "user_rating": user_rating,
        "pulled_at": pulled_at,
    }


def _default_start_time() -> int:
    """Unix start_time for a first pull: now - HISTORY_DAYS."""
    start = datetime.now(timezone.utc) - timedelta(days=config.HISTORY_DAYS)
    return int(start.timestamp())


# --- Commands ------------------------------------------------------------------
def probe(client: ZendeskClient, limit: int, since: Optional[str] = None) -> None:
    """Inspect a small sample WITHOUT writing to the store.

    Confirms the two open questions: (1) do both email and web/messaging
    conversations come back, and (2) how are bot vs human-agent turns marked.
    Pass --since to look at the bot era rather than the oldest tickets in the window.
    """
    if since:
        start_time = int(datetime.strptime(since, "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc).timestamp())
    else:
        start_time = _default_start_time()
    print(f"Probing up to {limit} tickets since {datetime.utcfromtimestamp(start_time)} UTC\n")
    channel_counts: Counter = Counter()
    authors_seen: Dict[str, dict] = {}
    sample_shown = 0
    skipped = 0

    for i, (ticket, _cursor) in enumerate(client.iter_incremental_tickets(start_time, None)):
        if i >= limit:
            break
        channel_counts[normalize_channel((ticket.get("via") or {}).get("channel"))] += 1
        comments, users_by_id = client.fetch_comments(ticket["id"])
        if comments is None:
            skipped += 1
            continue
        for c in comments:
            aid = str(c.get("author_id"))
            u = users_by_id.get(aid, {})
            authors_seen[aid] = {"name": u.get("name"), "role": u.get("role")}
        if sample_shown < 2 and comments:
            row = assemble_row(ticket, comments, users_by_id, datetime.now(timezone.utc))
            print(f"--- sample conversation {row['conversation_id']} "
                  f"(channel={row['channel']}, turns={row['turn_count']}, "
                  f"ended_with_human={row['ended_with_human']}) ---")
            print(row["full_text"][:1500])
            print()
            sample_shown += 1

    print(f"Skipped {skipped} deleted/inaccessible tickets (404 on comments).")
    print("Channel distribution:", dict(channel_counts))
    print("\nDistinct comment authors (id -> name, role) — use these to set BOT_AUTHOR_IDS:")
    for aid, meta in authors_seen.items():
        print(f"  {aid}: {meta['name']!r}  role={meta['role']!r}")
    print("\nNo data was written. If bot turns are mislabelled 'Agent:' above, set "
          "BOT_AUTHOR_IDS in .env to the bot's author id and re-probe.")


def run(client: ZendeskClient, since: Optional[str], limit: Optional[int], reset: bool) -> None:
    """Incremental pull into the conversations table."""
    db.init_db()
    cursor = None if reset else db.get_pull_state(CURSOR_KEY)
    if since:
        start_time = int(datetime.strptime(since, "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc).timestamp())
        cursor = None  # explicit --since overrides the saved cursor
    else:
        start_time = _default_start_time()

    pulled_at = datetime.now(timezone.utc)
    fields = FieldMap(client.fetch_ticket_fields())
    batch: List[dict] = []
    attrs: List[dict] = []
    total = 0
    total_attrs = 0
    skipped = 0
    last_cursor = cursor

    for ticket, after_cursor in client.iter_incremental_tickets(start_time, cursor):
        # Attributes need no comment fetch, so they are kept even for a ticket whose
        # comments 404; they are written BEFORE the cursor advances, like the conversations.
        attrs.append(assemble_attributes(ticket, fields, pulled_at))
        comments, users_by_id = client.fetch_comments(ticket["id"])
        if comments is None:
            skipped += 1
            last_cursor = after_cursor or last_cursor
            continue
        batch.append(assemble_row(ticket, comments, users_by_id, pulled_at))
        last_cursor = after_cursor or last_cursor
        if len(batch) >= 200:
            total += db.upsert_conversations(batch)
            total_attrs += db.upsert_ticket_attributes(attrs)
            if last_cursor:
                db.set_pull_state(CURSOR_KEY, last_cursor)
            print(f"  upserted {total} so far...")
            batch, attrs = [], []
        if limit and total + len(batch) >= limit:
            break

    if batch:
        total += db.upsert_conversations(batch)
    if attrs:
        total_attrs += db.upsert_ticket_attributes(attrs)
    if last_cursor:
        db.set_pull_state(CURSOR_KEY, last_cursor)

    print(f"Done. Upserted {total} conversations ({total_attrs} ticket attribute rows), "
          f"skipped {skipped} deleted/inaccessible. Store now:")
    for table, n in db.table_counts().items():
        print(f"  {table}: {n}")


def backfill_attributes(client: ZendeskClient, since: Optional[str], reset: bool) -> None:
    """Tags + custom fields only, straight from the ticket export (no comment fetch).

    Has its own resumable cursor, separate from the conversations cursor, so it can be
    re-run or interrupted without affecting the daily pull.
    """
    db.init_db()
    cursor = None if (reset or since) else db.get_pull_state(ATTR_BACKFILL_KEY)
    start = since or config.BOT_EXPORT_START
    start_time = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    pulled_at = datetime.now(timezone.utc)
    fields = FieldMap(client.fetch_ticket_fields())
    attrs: List[dict] = []
    total = 0
    last_cursor = cursor
    print(f"Backfilling ticket attributes from {'saved cursor' if cursor else start}...")
    for ticket, after_cursor in client.iter_incremental_tickets(start_time, cursor):
        attrs.append(assemble_attributes(ticket, fields, pulled_at))
        last_cursor = after_cursor or last_cursor
        if len(attrs) >= 1000:
            total += db.upsert_ticket_attributes(attrs)
            if last_cursor:
                db.set_pull_state(ATTR_BACKFILL_KEY, last_cursor)
            print(f"  {total} so far...")
            attrs = []
    if attrs:
        total += db.upsert_ticket_attributes(attrs)
    if last_cursor:
        db.set_pull_state(ATTR_BACKFILL_KEY, last_cursor)
    print(f"Done. Upserted {total} ticket attribute rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull Zendesk conversations (read-only).")
    parser.add_argument("--probe", action="store_true",
                        help="Inspect a small sample and print diagnostics; writes nothing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after roughly this many tickets (probe defaults to 20).")
    parser.add_argument("--since", type=str, default=None,
                        help="Override start date (YYYY-MM-DD); ignores the saved cursor.")
    parser.add_argument("--reset", action="store_true",
                        help="Ignore the saved cursor and pull from the history window again.")
    parser.add_argument("--attributes-only", action="store_true",
                        help="Backfill tags + custom fields only (no comments, own cursor).")
    args = parser.parse_args()

    client = ZendeskClient()
    if args.attributes_only:
        backfill_attributes(client, args.since, args.reset)
    elif args.probe:
        probe(client, args.limit or 20, args.since)
    else:
        run(client, args.since, args.limit, args.reset)


if __name__ == "__main__":
    main()
