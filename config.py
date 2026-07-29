"""Central configuration.

All settings and secret *names* live here. Secret VALUES are read from the
environment (via a gitignored .env) and are never hardcoded, logged, or printed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from the project root if present. Real values stay in .env (gitignored);
# .env.example documents the required keys.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# --- Storage -------------------------------------------------------------------
# Single local DuckDB file. Swapping to BigQuery later is isolated to db.py.
DB_PATH = PROJECT_ROOT / os.environ.get("DB_FILENAME", "zendesk_dashboard.duckdb")
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"

# --- LLM judge -----------------------------------------------------------------
# Model is config, not code, so switching (e.g. Opus -> Sonnet for bulk) is one line.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-4-8")
JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "2048"))
# Reasoning depth for the judge. 'low' keeps Opus cost bounded on a 3-way
# classification; raise if calibration shows the judge struggling on hard cases.
JUDGE_EFFORT = os.environ.get("JUDGE_EFFORT", "low")
# How much conversation text to send to the judge (guards cost + context limits).
JUDGE_MAX_CHARS = int(os.environ.get("JUDGE_MAX_CHARS", "12000"))

# --- Pull ----------------------------------------------------------------------
# First pull covers this many days of history (decided: 90 for v1).
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "90"))
# Politeness / safety when talking to the Zendesk API.
ZENDESK_PAGE_SIZE = int(os.environ.get("ZENDESK_PAGE_SIZE", "100"))
ZENDESK_MAX_RETRIES = int(os.environ.get("ZENDESK_MAX_RETRIES", "5"))

# --- Dashboard ------------------------------------------------------------------
# Single shared password gating the Streamlit dashboard. Read from the environment
# (never hardcoded). If unset, the dashboard refuses to render rather than exposing
# data unprotected.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# --- Calibration ---------------------------------------------------------------
CALIBRATION_SAMPLE_SIZE = int(os.environ.get("CALIBRATION_SAMPLE_SIZE", "50"))
# Agreement % below which we do NOT quote the headline resolution rate to anyone.
CALIBRATION_TRUST_THRESHOLD = float(os.environ.get("CALIBRATION_TRUST_THRESHOLD", "0.80"))

# --- Secret env var names (values read at call time, never stored here) ---------
ZENDESK_SUBDOMAIN_VAR = "ZENDESK_SUBDOMAIN"
ZENDESK_EMAIL_VAR = "ZENDESK_EMAIL"
ZENDESK_API_TOKEN_VAR = "ZENDESK_API_TOKEN"
ANTHROPIC_API_KEY_VAR = "ANTHROPIC_API_KEY"

ZENDESK_REQUIRED_VARS: List[str] = [
    ZENDESK_SUBDOMAIN_VAR,
    ZENDESK_EMAIL_VAR,
    ZENDESK_API_TOKEN_VAR,
]
ANTHROPIC_REQUIRED_VARS: List[str] = [ANTHROPIC_API_KEY_VAR]


def missing_vars(names: List[str]) -> List[str]:
    """Return the names (never the values) of any required env vars that are unset/blank."""
    return [name for name in names if not os.environ.get(name)]


def require(names: List[str]) -> None:
    """Raise a clear error naming any missing env vars. Never prints their values."""
    missing = missing_vars(names)
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )
