"""Data-access layer — the ONLY module that talks to the store.

Everything else (pull, score, themes, dashboard, calibration) goes through here.
Keeping all SQL in one module is what makes the later swap to BigQuery a
single-file change rather than a rewrite.

Design guarantees:
  * Idempotent writes: upserts key on the primary key, so re-running a job
    updates in place and never double-counts.
  * Raw conversations and scores are separate tables (re-score without re-pull).
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import duckdb
import pandas as pd

import config

# --- Column definitions (explicit, so upserts are deterministic) ---------------
CONVERSATION_COLUMNS: List[str] = [
    "conversation_id",
    "channel",
    "created_at",
    "updated_at",
    "subject",
    "full_text",
    "turn_count",
    "bot_participated",
    "ended_with_human",
    "user_rating",
    "pulled_at",
]

SCORE_COLUMNS: List[str] = [
    "conversation_id",
    "resolution",
    "confidence",
    "primary_topic",
    "unanswered_reason",
    "scored_at",
    "model",
]


RESOLUTION_PATH_COLUMNS: List[str] = [
    "conversation_id",
    "resolution_path",
    "path_reason",
    "classified_at",
    "model",
]


def _now() -> datetime:
    """UTC timestamp for *_at columns. Kept in one place for consistency."""
    return datetime.now(timezone.utc)


def get_connection(read_only: bool = False) -> "duckdb.DuckDBPyConnection":
    """Open a DuckDB connection to the configured file.

    Pass read_only=True from the dashboard so a running pull/score job can't be
    blocked or corrupted by a reader. read_only requires the file to exist, so
    callers that might hit a fresh install should init_db() first.
    """
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def init_db() -> None:
    """Create tables from schema.sql if they don't exist. Safe to run repeatedly."""
    schema_sql = config.SCHEMA_PATH.read_text()
    with contextlib.closing(get_connection()) as con:
        con.execute(schema_sql)


def _upsert(con, table: str, columns: List[str], rows: Iterable[Dict]) -> int:
    """Generic keyed upsert on the first column (the primary key). Returns row count."""
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(columns))
    updatable = [c for c in columns[1:]]  # everything except the PK
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in updatable)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({columns[0]}) DO UPDATE SET {set_clause}"
    )
    values = [[row.get(col) for col in columns] for row in rows]
    con.executemany(sql, values)
    return len(values)


def upsert_conversations(rows: Iterable[Dict]) -> int:
    """Insert/update raw conversations. Idempotent on conversation_id."""
    with contextlib.closing(get_connection()) as con:
        return _upsert(con, "conversations", CONVERSATION_COLUMNS, rows)


def upsert_scores(rows: Iterable[Dict]) -> int:
    """Insert/update scores. Idempotent on conversation_id (latest-wins)."""
    with contextlib.closing(get_connection()) as con:
        return _upsert(con, "scores", SCORE_COLUMNS, rows)


# --- Pull job state (incremental high-water mark) ------------------------------
def get_pull_state(key: str) -> Optional[str]:
    with contextlib.closing(get_connection(read_only=True)) as con:
        result = con.execute(
            "SELECT value FROM pull_state WHERE key = ?", [key]
        ).fetchone()
    return result[0] if result else None


def set_pull_state(key: str, value: str) -> None:
    with contextlib.closing(get_connection()) as con:
        con.execute(
            "INSERT INTO pull_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            [key, value, _now()],
        )


# --- Read helpers used by the scorer / themes / dashboard ----------------------
def fetch_unscored_conversations(limit: Optional[int] = None) -> pd.DataFrame:
    """Conversations with no row in scores yet (drives incremental scoring)."""
    sql = (
        "SELECT c.* FROM conversations c "
        "LEFT JOIN scores s USING (conversation_id) "
        "WHERE s.conversation_id IS NULL "
        "ORDER BY c.updated_at"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql).fetch_df()


def conversations_to_score(rescore: bool = False, limit: Optional[int] = None) -> pd.DataFrame:
    """Bot conversations that need scoring.

    Only conversations the bot took part in (bot_participated) are scored — the
    human-only web-form tickets aren't bot performance. By default returns just
    the not-yet-scored ones (incremental); rescore=True returns all of them.
    """
    if rescore:
        sql = (
            "SELECT c.* FROM conversations c "
            "WHERE c.bot_participated ORDER BY c.updated_at"
        )
    else:
        sql = (
            "SELECT c.* FROM conversations c "
            "LEFT JOIN scores s USING (conversation_id) "
            "WHERE c.bot_participated AND s.conversation_id IS NULL "
            "ORDER BY c.updated_at"
        )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql).fetch_df()


def upsert_resolution_paths(rows: Iterable[Dict]) -> int:
    """Insert/update backlog resolution-path classifications. Idempotent on conversation_id."""
    with contextlib.closing(get_connection()) as con:
        return _upsert(con, "resolution_paths", RESOLUTION_PATH_COLUMNS, rows)


def backlog_to_classify(reclassify: bool = False, limit: Optional[int] = None) -> pd.DataFrame:
    """Backlog conversations (partial/unresolved bot conversations) needing a path tag.

    By default returns only those not yet classified; reclassify=True returns all backlog.
    """
    base = (
        "SELECT c.conversation_id, c.channel, c.full_text, s.resolution, s.primary_topic "
        "FROM conversations c JOIN scores s USING (conversation_id) "
        "WHERE c.bot_participated AND s.resolution IN ('partial','unresolved')"
    )
    if reclassify:
        sql = base + " ORDER BY c.updated_at"
    else:
        sql = (
            base.replace("FROM conversations c JOIN scores s USING (conversation_id)",
                         "FROM conversations c JOIN scores s USING (conversation_id) "
                         "LEFT JOIN resolution_paths r USING (conversation_id)")
            + " AND r.conversation_id IS NULL ORDER BY c.updated_at"
        )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql).fetch_df()


def sample_by_path(path: str, limit: int = 25) -> pd.DataFrame:
    """Random sample of backlog conversations classified with a given resolution_path.
    Used to validate a bucket (e.g. 'human') by eyeball."""
    sql = (
        "SELECT r.conversation_id, c.channel, s.primary_topic, r.path_reason, c.full_text "
        "FROM resolution_paths r "
        "JOIN conversations c USING (conversation_id) "
        "JOIN scores s USING (conversation_id) "
        "WHERE r.resolution_path = ? "
        "ORDER BY random() LIMIT ?"
    )
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql, [path, int(limit)]).fetch_df()


def bot_conversations(limit: Optional[int] = None) -> pd.DataFrame:
    """All bot conversations (raw, no score needed). For pre-scoring hand-labelling."""
    sql = "SELECT * FROM conversations WHERE bot_participated ORDER BY updated_at"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql).fetch_df()


def scored_conversations(
    start: Optional[str] = None, end: Optional[str] = None
) -> pd.DataFrame:
    """Bot conversations that have a score, joined with their score.

    This is the dashboard's and calibration's single read. Scoped to
    bot_participated (bot performance only). Optional inclusive date filters on
    created_at ('YYYY-MM-DD').
    """
    sql = (
        "SELECT c.conversation_id, c.channel, c.created_at, c.subject, "
        "       c.full_text, c.turn_count, c.ended_with_human, c.user_rating, "
        "       s.resolution, s.confidence, s.primary_topic, s.unanswered_reason, "
        "       s.scored_at, s.model, r.resolution_path "
        "FROM conversations c JOIN scores s USING (conversation_id) "
        "LEFT JOIN resolution_paths r USING (conversation_id) "
        "WHERE c.bot_participated"
    )
    params: list = []
    if start:
        sql += " AND CAST(c.created_at AS DATE) >= CAST(? AS DATE)"
        params.append(start)
    if end:
        sql += " AND CAST(c.created_at AS DATE) <= CAST(? AS DATE)"
        params.append(end)
    sql += " ORDER BY c.created_at"
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql, params).fetch_df()


def query_df(sql: str, params: Optional[list] = None) -> pd.DataFrame:
    """Generic read-only query returning a DataFrame. For dashboard/reporting use."""
    with contextlib.closing(get_connection(read_only=True)) as con:
        return con.execute(sql, params or []).fetch_df()


def table_counts() -> Dict[str, int]:
    """Quick health check: row counts per table. Used to verify the scaffold works."""
    counts: Dict[str, int] = {}
    with contextlib.closing(get_connection(read_only=True)) as con:
        for table in ("conversations", "scores", "themes", "pull_state"):
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


if __name__ == "__main__":
    # `python db.py` initialises the store and prints table counts — the Stage-0 check.
    init_db()
    print("Initialised", config.DB_PATH)
    for table, n in table_counts().items():
        print(f"  {table}: {n}")
