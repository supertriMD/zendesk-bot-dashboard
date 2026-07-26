# Zendesk Bot Performance Dashboard

Measures how our Zendesk support **bot** is performing. A scheduled, read-only
pipeline pulls Zendesk conversations, uses Claude to score each as
**resolved / partial / unresolved** and tag its topic, and a Streamlit dashboard
shows:

1. the **estimated** % of questions answered to satisfaction, split **email vs web**, and
2. the **top-5 themes the bot could not answer** — the content backlog.

> The satisfaction % is an **estimate from an LLM judge, not ground truth.** It is
> labelled *estimated* everywhere in the UI and must be calibrated against
> hand-labelled data before anyone quotes it.

## Guardrails (true for the whole project)

- **Read-only against Zendesk.** Never replies to, modifies, closes, or tags a ticket. Reads only.
- **Secrets** (Zendesk token, Anthropic key) live in a **gitignored `.env`**, read from env vars — never hardcoded, printed, or committed.
- **Batch / re-runnable.** Every job is idempotent; re-running never double-counts or corrupts data.
- **Raw conversations stored separately from scores** — re-score without re-pulling.
- **All DB access is behind `db.py`** — the store can move to BigQuery later without touching the rest.

## Architecture

```
Zendesk API ──(pull)──▶ conversations ──(Claude judge)──▶ scores ──▶ Streamlit dashboard
                          (raw store)                     (labels)      + top-5 themes
```

Local **DuckDB** file for v1. `db.py` is the single seam to swap in BigQuery later.

## Layout

| File | Role |
|---|---|
| `config.py` | Settings + secret env-var names (values read from `.env`) |
| `db.py` | The only module that talks to the store |
| `schema.sql` | Table definitions (conversations, scores, themes, pull_state) |
| `pull_conversations.py` | Zendesk API → `conversations` (Stage 1, not built yet) |
| `score_conversations.py` | Unscored → Claude judge → `scores` (Stage 2, not built yet) |
| `themes.py` | Cluster unresolved into the top-5 backlog (Stage 3, not built yet) |
| `app.py` | Streamlit dashboard (Stage 4, not built yet) |
| `calibration/` | Export 50 for hand-labelling, then human-vs-Claude agreement |
| `docs/` | The original build briefs |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in real values
```

Python 3.9 works (system default here); 3.11+ recommended.

### Which Anthropic Console org owns the key (recorded 26 Jul 2026)

Worth knowing before anyone debugs a billing line or a rate limit:

- The `ANTHROPIC_API_KEY` in this project's `.env` belongs to the **"Supertri (API)" Console org**,
  created 24 Jul 2026 specifically for this work.
- A **separate, pre-existing "Supertri" Console org** also exists — very likely the one billing the
  existing Claude API usage on the analytics side. As at 26 Jul there was a **pending join-request**
  to it; the plan if it needs claiming was to go via support@anthropic.com with domain verification.

So **two orgs can legitimately show Supertri usage**, and this repo's spend lands on the newer one.
If a Console bill or usage graph looks wrong, check which org you are logged into before assuming a
problem here.

## Stage 0 check (this scaffold)

```bash
python db.py
```

Initialises the DuckDB file and prints table counts (all zero on a fresh install).
No Zendesk or Anthropic calls are made at this stage.

## Build sequence (verify each stage before the next)

1. **Pull** → confirm real conversations land in DuckDB, both channels.
2. **Score** a small batch → eyeball ~10 by hand.
3. **Calibrate** on 50 → know the agreement % (aim >80% before trusting the headline).
4. **Themes + dashboard** → top-5 matches your gut sense of user pain.

## The judge model

Set in `config.py` (`JUDGE_MODEL`, default `claude-opus-4-8`) or overridden via a
`JUDGE_MODEL` env var — so switching (e.g. Opus for calibration, a cheaper model
for bulk) is a one-line change.
