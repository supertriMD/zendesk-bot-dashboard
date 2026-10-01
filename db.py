"""Data-access layer — the ONLY module that talks to the store.

Backend: BigQuery (dataset `config.BQ_DATASET` in project `config.BQ_PROJECT`, EU).
Everything else (pull, score, classify, dashboard, calibration) goes through here,
so the store technology stays isolated to this one module.

Auth: the google-cloud-bigquery client reads GOOGLE_APPLICATION_CREDENTIALS
(a service-account key path) or Application Default Credentials from the
environment. We never handle key contents here.

Guarantees:
  * Idempotent writes — upserts MERGE on the primary key, so re-running a job
    updates in place and never double-counts.
  * Raw conversations and scores are separate tables (re-score without re-pull).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import pandas as pd
from google.cloud import bigquery

import config

# --- Column definitions (order matters for upserts) ----------------------------
CONVERSATION_COLUMNS: List[str] = [
    "conversation_id", "channel", "created_at", "updated_at", "subject", "status",
    "full_text", "turn_count", "bot_participated", "ended_with_human",
    "user_rating", "pulled_at",
]
SCORE_COLUMNS: List[str] = [
    "conversation_id", "resolution", "confidence", "primary_topic",
    "unanswered_reason", "scored_at", "model",
]
RESOLUTION_PATH_COLUMNS: List[str] = [
    "conversation_id", "resolution_path", "path_reason", "classified_at", "model",
]
# Zendesk's own ticket metadata: tags + custom fields (incl. the AI agent's resolution tier).
# Its own table rather than extra columns on `conversations`, so it can be backfilled from the
# ticket export alone (no comment fetch) and the dashboard's existing queries are untouched.
TICKET_ATTRIBUTE_COLUMNS: List[str] = [
    "conversation_id", "via_channel", "ticket_form_id", "brand_id", "priority", "ticket_type",
    "satisfaction_score", "tags",
    "zd_resolution_tier", "zd_resolution_type", "zd_event", "zd_race_division", "zd_topic",
    "zd_inquiry", "zd_channel_group", "custom_fields_json", "messaging_conversation_id",
    "ticket_created_at", "ticket_updated_at", "pulled_at",
]
# Zendesk's ticket metric set (sideloaded on the same ticket export): response speed.
TICKET_METRIC_COLUMNS: List[str] = [
    "conversation_id", "reply_time_min", "first_resolution_time_min", "full_resolution_time_min",
    "agent_wait_time_min", "requester_wait_time_min", "on_hold_time_min", "reopens", "replies",
    "group_stations", "assignee_stations", "initially_assigned_at", "solved_at",
    "latest_comment_added_at", "metric_updated_at", "pulled_at",
]
# AI agents (Ultimate) data export: one row per bot conversation, ticketed or not.
# No message text is exported by Zendesk; conversations_data (session parameters, which can
# carry contact details) is NOT stored whole, only allowlisted signal keys (session_signals_json).
BOT_CONVERSATION_COLUMNS: List[str] = [
    "conversation_id", "platform_conversation_id", "bot_id", "bot_name", "channel", "language",
    "conversation_type", "conversation_status", "conversation_start_time", "conversation_end_time",
    "resolution_tier", "last_resolution", "automated_resolution", "automated_resolution_reasoning",
    "is_llm_conversation", "has_knowledge_response_attempt", "test_mode",
    "bot_messages_count", "visitor_messages_count", "not_understood_messages_count",
    "knowledge_response_generated_count", "knowledge_fallback_count",
    "knowledge_not_understood_count", "knowledge_escalation_required_count",
    "knowledge_error_occurred_count",
    "labels_json", "triggered_use_cases_json", "triggered_intent_replies_json",
    "triggered_procedures_json", "triggered_replies_json", "knowledge_sources_json",
    "segments_json", "session_signals_json", "conversations_data_keys", "rbp_status_name",
    "session_ended_at", "extra_json", "export_date", "pulled_at",
]

# --- BigQuery table schemas ----------------------------------------------------
_SF = bigquery.SchemaField
SCHEMAS: Dict[str, list] = {
    "conversations": [
        _SF("conversation_id", "STRING"), _SF("channel", "STRING"),
        _SF("created_at", "TIMESTAMP"), _SF("updated_at", "TIMESTAMP"),
        _SF("subject", "STRING"), _SF("status", "STRING"),
        _SF("full_text", "STRING"), _SF("turn_count", "INT64"),
        _SF("bot_participated", "BOOL"), _SF("ended_with_human", "BOOL"),
        _SF("user_rating", "STRING"), _SF("pulled_at", "TIMESTAMP"),
    ],
    "scores": [
        _SF("conversation_id", "STRING"), _SF("resolution", "STRING"),
        _SF("confidence", "FLOAT64"), _SF("primary_topic", "STRING"),
        _SF("unanswered_reason", "STRING"), _SF("scored_at", "TIMESTAMP"),
        _SF("model", "STRING"),
    ],
    "resolution_paths": [
        _SF("conversation_id", "STRING"), _SF("resolution_path", "STRING"),
        _SF("path_reason", "STRING"), _SF("classified_at", "TIMESTAMP"),
        _SF("model", "STRING"),
    ],
    "themes": [
        _SF("theme", "STRING"), _SF("example_question", "STRING"),
        _SF("count", "INT64"), _SF("period", "STRING"),
        _SF("generated_at", "TIMESTAMP"), _SF("model", "STRING"),
    ],
    "pull_state": [
        _SF("key", "STRING"), _SF("value", "STRING"), _SF("updated_at", "TIMESTAMP"),
    ],
    "ticket_attributes": [
        _SF("conversation_id", "STRING"), _SF("via_channel", "STRING"),
        _SF("ticket_form_id", "STRING"), _SF("brand_id", "STRING"),
        _SF("priority", "STRING"), _SF("ticket_type", "STRING"),
        _SF("satisfaction_score", "STRING"), _SF("tags", "STRING", mode="REPEATED"),
        _SF("zd_resolution_tier", "STRING"), _SF("zd_resolution_type", "STRING"),
        _SF("zd_event", "STRING"), _SF("zd_race_division", "STRING"),
        _SF("zd_topic", "STRING"), _SF("zd_inquiry", "STRING"),
        _SF("zd_channel_group", "STRING"), _SF("custom_fields_json", "STRING"),
        _SF("messaging_conversation_id", "STRING"),
        _SF("ticket_created_at", "TIMESTAMP"), _SF("ticket_updated_at", "TIMESTAMP"),
        _SF("pulled_at", "TIMESTAMP"),
    ],
    "ticket_metrics": [
        _SF("conversation_id", "STRING"),
        _SF("reply_time_min", "INT64"), _SF("first_resolution_time_min", "INT64"),
        _SF("full_resolution_time_min", "INT64"), _SF("agent_wait_time_min", "INT64"),
        _SF("requester_wait_time_min", "INT64"), _SF("on_hold_time_min", "INT64"),
        _SF("reopens", "INT64"), _SF("replies", "INT64"),
        _SF("group_stations", "INT64"), _SF("assignee_stations", "INT64"),
        _SF("initially_assigned_at", "TIMESTAMP"), _SF("solved_at", "TIMESTAMP"),
        _SF("latest_comment_added_at", "TIMESTAMP"), _SF("metric_updated_at", "TIMESTAMP"),
        _SF("pulled_at", "TIMESTAMP"),
    ],
    "bot_conversations": [
        _SF("conversation_id", "STRING"), _SF("platform_conversation_id", "STRING"),
        _SF("bot_id", "STRING"), _SF("bot_name", "STRING"), _SF("channel", "STRING"),
        _SF("language", "STRING"), _SF("conversation_type", "STRING"),
        _SF("conversation_status", "STRING"),
        _SF("conversation_start_time", "TIMESTAMP"), _SF("conversation_end_time", "TIMESTAMP"),
        _SF("resolution_tier", "STRING"), _SF("last_resolution", "STRING"),
        _SF("automated_resolution", "BOOL"), _SF("automated_resolution_reasoning", "STRING"),
        _SF("is_llm_conversation", "BOOL"), _SF("has_knowledge_response_attempt", "BOOL"),
        _SF("test_mode", "BOOL"),
        _SF("bot_messages_count", "INT64"), _SF("visitor_messages_count", "INT64"),
        _SF("not_understood_messages_count", "INT64"),
        _SF("knowledge_response_generated_count", "INT64"), _SF("knowledge_fallback_count", "INT64"),
        _SF("knowledge_not_understood_count", "INT64"),
        _SF("knowledge_escalation_required_count", "INT64"),
        _SF("knowledge_error_occurred_count", "INT64"),
        _SF("labels_json", "STRING"), _SF("triggered_use_cases_json", "STRING"),
        _SF("triggered_intent_replies_json", "STRING"), _SF("triggered_procedures_json", "STRING"),
        _SF("triggered_replies_json", "STRING"), _SF("knowledge_sources_json", "STRING"),
        _SF("segments_json", "STRING"), _SF("session_signals_json", "STRING"),
        _SF("conversations_data_keys", "STRING", mode="REPEATED"),
        _SF("rbp_status_name", "STRING"), _SF("session_ended_at", "TIMESTAMP"),
        _SF("extra_json", "STRING"),
        _SF("export_date", "DATE"), _SF("pulled_at", "TIMESTAMP"),
    ],
}
_PK = {"conversations": "conversation_id", "scores": "conversation_id",
       "resolution_paths": "conversation_id", "pull_state": "key",
       "ticket_attributes": "conversation_id", "bot_conversations": "conversation_id",
       "ticket_metrics": "conversation_id"}

_client: Optional[bigquery.Client] = None


def client() -> bigquery.Client:
    """BigQuery client. Credentials, in order:
      1. GCP_SERVICE_ACCOUNT_JSON env var (raw JSON) — used on Streamlit Cloud,
         where the key comes from st.secrets (see app.py's secrets bridge).
      2. Otherwise Application Default Credentials / GOOGLE_APPLICATION_CREDENTIALS
         (local dev, the daily job).
    """
    global _client
    if _client is None:
        creds_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
        if creds_json:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
            _client = bigquery.Client(project=config.BQ_PROJECT,
                                      location=config.BQ_LOCATION, credentials=creds)
        else:
            _client = bigquery.Client(project=config.BQ_PROJECT, location=config.BQ_LOCATION)
    return _client


def _tbl(name: str) -> str:
    return f"{config.BQ_PROJECT}.{config.BQ_DATASET}.{name}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _py(v):
    """Make a value JSON-serialisable for a BigQuery load: NaN/NaT -> None,
    datetimes/Timestamps -> ISO string, numpy scalars -> native python."""
    try:
        if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:
            return v
    return v


# --- Schema / setup ------------------------------------------------------------
def init_db() -> None:
    """Create the dataset (EU) and tables if they don't exist. Safe to re-run."""
    c = client()
    dataset = bigquery.Dataset(f"{config.BQ_PROJECT}.{config.BQ_DATASET}")
    dataset.location = config.BQ_LOCATION
    c.create_dataset(dataset, exists_ok=True)
    for name, schema in SCHEMAS.items():
        table = c.create_table(bigquery.Table(_tbl(name), schema=schema), exists_ok=True)
        # Additive schema evolution: a column added to SCHEMAS after the table was created is
        # appended here (BigQuery allows adding NULLABLE/REPEATED columns, never dropping).
        have = {f.name for f in table.schema}
        missing = [f for f in schema if f.name not in have]
        if missing:
            table.schema = list(table.schema) + missing
            c.update_table(table, ["schema"])
            print(f"  {name}: added column(s) {[f.name for f in missing]}")
    apply_views()


def apply_views() -> None:
    """(Re)create the reporting views in views.sql. Idempotent (CREATE OR REPLACE)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "views.sql")
    if not os.path.exists(path):
        return
    ds = f"{config.BQ_PROJECT}.{config.BQ_DATASET}"
    sql = open(path, encoding="utf-8").read().replace("${DS}", ds)
    for stmt in (x.strip() for x in sql.split("\n;;\n")):
        if stmt and any(not l.strip().startswith("--") for l in stmt.splitlines() if l.strip()):
            client().query(stmt).result()


# --- Idempotent writes (load-to-staging + MERGE) -------------------------------
def _merge(table: str, columns: List[str], rows: Iterable[Dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    c = client()
    pk = _PK[table]
    schema = [f for f in SCHEMAS[table] if f.name in columns]
    staging = _tbl(f"_stg_{table}")
    payload = [{col: _py(r.get(col)) for col in columns} for r in rows]
    c.load_table_from_json(
        payload, staging,
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"),
    ).result()
    updatable = [col for col in columns if col != pk]
    set_clause = ", ".join(f"T.{c_} = S.{c_}" for c_ in updatable)
    cols = ", ".join(columns)
    vals = ", ".join(f"S.{c_}" for c_ in columns)
    c.query(
        f"MERGE {_tbl(table)} T USING {staging} S ON T.{pk} = S.{pk} "
        f"WHEN MATCHED THEN UPDATE SET {set_clause} "
        f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})"
    ).result()
    return len(rows)


def upsert_conversations(rows: Iterable[Dict]) -> int:
    return _merge("conversations", CONVERSATION_COLUMNS, rows)


def upsert_scores(rows: Iterable[Dict]) -> int:
    return _merge("scores", SCORE_COLUMNS, rows)


def upsert_resolution_paths(rows: Iterable[Dict]) -> int:
    return _merge("resolution_paths", RESOLUTION_PATH_COLUMNS, rows)


def upsert_ticket_attributes(rows: Iterable[Dict]) -> int:
    return _merge("ticket_attributes", TICKET_ATTRIBUTE_COLUMNS, rows)


def upsert_ticket_metrics(rows: Iterable[Dict]) -> int:
    return _merge("ticket_metrics", TICKET_METRIC_COLUMNS, rows)


def upsert_bot_conversations(rows: Iterable[Dict]) -> int:
    return _merge("bot_conversations", BOT_CONVERSATION_COLUMNS, rows)


def update_statuses(pairs: Iterable[Dict]) -> int:
    """Backfill/refresh only the `status` column. pairs: [{conversation_id, status}]."""
    pairs = list(pairs)
    if not pairs:
        return 0
    c = client()
    staging = _tbl("_stg_status")
    payload = [{"conversation_id": p["conversation_id"], "status": _py(p.get("status"))}
               for p in pairs]
    c.load_table_from_json(
        payload, staging,
        job_config=bigquery.LoadJobConfig(
            schema=[_SF("conversation_id", "STRING"), _SF("status", "STRING")],
            write_disposition="WRITE_TRUNCATE"),
    ).result()
    c.query(
        f"MERGE {_tbl('conversations')} T USING {staging} S "
        "ON T.conversation_id = S.conversation_id "
        "WHEN MATCHED THEN UPDATE SET status = S.status"
    ).result()
    return len(pairs)


# --- Pull-job state ------------------------------------------------------------
def get_pull_state(key: str) -> Optional[str]:
    df = query_df(f"SELECT value FROM {_tbl('pull_state')} WHERE key = @key", {"key": key})
    return None if df.empty else df["value"].iloc[0]


def set_pull_state(key: str, value: str) -> None:
    _merge("pull_state", ["key", "value", "updated_at"],
           [{"key": key, "value": value, "updated_at": _now()}])


# --- Reads ---------------------------------------------------------------------
def query_df(sql: str, params: Optional[dict] = None) -> pd.DataFrame:
    """Read-only query returning a DataFrame. Params are @named scalars."""
    job_config = None
    if params:
        qp = []
        for k, v in params.items():
            typ = "INT64" if isinstance(v, int) and not isinstance(v, bool) else "STRING"
            qp.append(bigquery.ScalarQueryParameter(k, typ, v if typ == "INT64" else str(v)))
        job_config = bigquery.QueryJobConfig(query_parameters=qp)
    return client().query(sql, job_config=job_config).to_dataframe()


def conversations_to_score(rescore: bool = False, limit: Optional[int] = None) -> pd.DataFrame:
    """Bot conversations needing scoring (unscored by default; all if rescore)."""
    if rescore:
        sql = (f"SELECT c.* FROM {_tbl('conversations')} c "
               "WHERE c.bot_participated ORDER BY c.updated_at")
    else:
        sql = (f"SELECT c.* FROM {_tbl('conversations')} c "
               f"LEFT JOIN {_tbl('scores')} s USING (conversation_id) "
               "WHERE c.bot_participated AND s.conversation_id IS NULL "
               "ORDER BY c.updated_at")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return query_df(sql)


def scored_conversations(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Bot conversations that have a score, joined with score + resolution_path."""
    # Deliberately NOT selecting full_text here — transcripts are large and the
    # aggregate views don't need them. Fetch them on demand via transcripts_for().
    sql = (
        "SELECT c.conversation_id, c.channel, c.created_at, c.subject, "
        "       c.turn_count, c.ended_with_human, c.user_rating, "
        "       s.resolution, s.confidence, s.primary_topic, s.unanswered_reason, "
        "       s.scored_at, s.model, r.resolution_path "
        f"FROM {_tbl('conversations')} c JOIN {_tbl('scores')} s USING (conversation_id) "
        f"LEFT JOIN {_tbl('resolution_paths')} r USING (conversation_id) "
        "WHERE c.bot_participated"
    )
    params: dict = {}
    if start:
        sql += " AND DATE(c.created_at) >= DATE(@start)"
        params["start"] = start
    if end:
        sql += " AND DATE(c.created_at) <= DATE(@end)"
        params["end"] = end
    sql += " ORDER BY c.created_at"
    return query_df(sql, params or None)


def transcripts_for(ids: Iterable[str]) -> pd.DataFrame:
    """Fetch full_text for specific conversation ids (drill-down / examples only)."""
    ids = [str(i) for i in ids]
    if not ids:
        return pd.DataFrame(columns=["conversation_id", "full_text"])
    sql = (f"SELECT conversation_id, full_text FROM {_tbl('conversations')} "
           "WHERE conversation_id IN UNNEST(@ids)")
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)])
    return client().query(sql, job_config=job_config).to_dataframe()


def all_conversations() -> pd.DataFrame:
    """Every conversation (all channels incl. web-form) for the weekly view."""
    return query_df(
        "SELECT conversation_id, channel, created_at, status, "
        f"bot_participated, ended_with_human FROM {_tbl('conversations')}"
    )


def bot_conversations(limit: Optional[int] = None) -> pd.DataFrame:
    """All bot conversations (raw). For pre-scoring hand-labelling."""
    sql = f"SELECT * FROM {_tbl('conversations')} WHERE bot_participated ORDER BY updated_at"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return query_df(sql)


def backlog_to_classify(reclassify: bool = False, limit: Optional[int] = None) -> pd.DataFrame:
    """Backlog conversations (partial/unresolved) needing a resolution-path tag."""
    select = ("SELECT c.conversation_id, c.channel, c.full_text, s.resolution, s.primary_topic "
              f"FROM {_tbl('conversations')} c JOIN {_tbl('scores')} s USING (conversation_id) ")
    where = "WHERE c.bot_participated AND s.resolution IN ('partial','unresolved')"
    if reclassify:
        sql = select + where + " ORDER BY c.updated_at"
    else:
        sql = (select + f"LEFT JOIN {_tbl('resolution_paths')} r USING (conversation_id) "
               + where + " AND r.conversation_id IS NULL ORDER BY c.updated_at")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return query_df(sql)


def sample_by_path(path: str, limit: int = 25) -> pd.DataFrame:
    """Random sample of backlog conversations with a given resolution_path."""
    sql = (
        "SELECT r.conversation_id, c.channel, s.primary_topic, r.path_reason, c.full_text "
        f"FROM {_tbl('resolution_paths')} r JOIN {_tbl('conversations')} c USING (conversation_id) "
        f"JOIN {_tbl('scores')} s USING (conversation_id) "
        f"WHERE r.resolution_path = @path ORDER BY RAND() LIMIT {int(limit)}"
    )
    return query_df(sql, {"path": path})


def table_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for t in ("conversations", "scores", "resolution_paths", "themes", "pull_state"):
        counts[t] = int(query_df(f"SELECT COUNT(*) AS n FROM {_tbl(t)}")["n"].iloc[0])
    return counts


if __name__ == "__main__":
    init_db()
    print(f"Initialised {config.BQ_PROJECT}.{config.BQ_DATASET} ({config.BQ_LOCATION})")
    for table, n in table_counts().items():
        print(f"  {table}: {n}")
