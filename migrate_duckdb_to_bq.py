"""One-off: migrate the local DuckDB store into BigQuery.

Reads the old `zendesk_dashboard.duckdb` file and loads every table into the
configured BigQuery dataset via db.py's idempotent upserts (safe to re-run).

Prereqs:
  - GOOGLE_APPLICATION_CREDENTIALS + BQ_* set in .env (BigQuery access)
  - duckdb installed:  pip install duckdb
  - the old .duckdb file present

Usage:
  python migrate_duckdb_to_bq.py [path/to/old.duckdb]
"""
from __future__ import annotations

import sys

import duckdb

import db

OLD = sys.argv[1] if len(sys.argv) > 1 else "zendesk_dashboard.duckdb"


def _rows(con, table: str, columns: list) -> list:
    df = con.execute(f"SELECT {', '.join(columns)} FROM {table}").fetch_df()
    df = df.where(df.notna(), None)  # NaN/NaT -> None
    return df.to_dict("records")


def main() -> None:
    con = duckdb.connect(OLD, read_only=True)
    db.init_db()  # ensure the BQ dataset + tables exist (EU)

    n_conv = db.upsert_conversations(_rows(con, "conversations", db.CONVERSATION_COLUMNS))
    n_score = db.upsert_scores(_rows(con, "scores", db.SCORE_COLUMNS))
    # resolution_paths may not exist on very old files
    try:
        n_path = db.upsert_resolution_paths(
            _rows(con, "resolution_paths", db.RESOLUTION_PATH_COLUMNS))
    except duckdb.CatalogException:
        n_path = 0
    # carry over the incremental pull cursor(s)
    n_state = 0
    for r in _rows(con, "pull_state", ["key", "value"]):
        db.set_pull_state(r["key"], r["value"])
        n_state += 1

    print(f"Migrated -> conversations {n_conv}, scores {n_score}, "
          f"resolution_paths {n_path}, pull_state {n_state}")
    print("BigQuery now holds:")
    for t, n in db.table_counts().items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
