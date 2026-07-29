-- Zendesk Bot Performance Dashboard — schema
-- Design rules baked in here:
--   * Raw conversations (pulled from Zendesk) are stored SEPARATELY from scores,
--     so we can re-score without re-pulling.
--   * Everything is keyed so pull/score jobs are idempotent (re-run = upsert, never double-count).
--   * All access goes through db.py; this file is the single source of table definitions.

-- Raw conversations pulled from Zendesk. Never edited by the scorer.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id   VARCHAR PRIMARY KEY,   -- Zendesk ticket / conversation id
    channel           VARCHAR,               -- 'email' | 'web' | 'messaging'
    created_at        TIMESTAMP,
    updated_at        TIMESTAMP,             -- used as the incremental high-water mark
    subject           VARCHAR,
    status            VARCHAR,               -- Zendesk ticket status: new/open/pending/hold/solved/closed
    full_text         VARCHAR,               -- concatenated turns, role-labelled (User:/Bot:/Agent:)
    turn_count        INTEGER,
    bot_participated  BOOLEAN,               -- did the bot (Tri, author -1) actually take part
    ended_with_human  BOOLEAN,               -- a human agent joined (bot did not fully resolve)
    user_rating       VARCHAR,               -- 'good' | 'bad' | NULL  (widget CSAT, real user signal)
    pulled_at         TIMESTAMP
);

-- Scored results. One row per conversation, latest-wins on re-score.
-- scored_at + model keep every number auditable ("why did the rate move?").
CREATE TABLE IF NOT EXISTS scores (
    conversation_id   VARCHAR PRIMARY KEY,
    resolution        VARCHAR,   -- 'resolved' | 'partial' | 'unresolved'  (the BOT's outcome)
    confidence        DOUBLE,    -- judge's self-reported 0..1
    primary_topic     VARCHAR,   -- short tag, lowercase 1-2 words, e.g. 'parking'
    unanswered_reason VARCHAR,   -- one line, only when not fully resolved
    scored_at         TIMESTAMP,
    model             VARCHAR    -- which model produced this score
);

-- Theme rollup: cached top-N unanswered themes per period.
-- The dashboard reads THIS cache (never clusters live) so labels are stable and free to view.
CREATE TABLE IF NOT EXISTS themes (
    theme             VARCHAR,
    example_question  VARCHAR,
    count             INTEGER,
    period            VARCHAR,   -- e.g. '2026-04-15..2026-07-14' or 'all'
    generated_at      TIMESTAMP,
    model             VARCHAR
);

-- Resolution-path classification of backlog conversations (how each should be solved).
-- Separate from `scores` so re-scoring never wipes these tags. One row per conversation.
CREATE TABLE IF NOT EXISTS resolution_paths (
    conversation_id   VARCHAR PRIMARY KEY,
    resolution_path   VARCHAR,   -- 'active_lookup' | 'content' | 'human'
    path_reason       VARCHAR,   -- one line on why
    classified_at     TIMESTAMP,
    model             VARCHAR
);

-- Small key/value table for job state (incremental pull high-water marks, etc.).
-- Keeps pulls idempotent and re-runnable without re-reading everything.
CREATE TABLE IF NOT EXISTS pull_state (
    key         VARCHAR PRIMARY KEY,   -- e.g. 'high_water_mark' or 'high_water_mark:web'
    value       VARCHAR,
    updated_at  TIMESTAMP
);
