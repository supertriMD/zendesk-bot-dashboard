"""Stage 2 — the LLM judge.

Sends each bot conversation to Claude, which classifies how the BOT did:
resolved / partial / unresolved, plus a topic tag and (when not resolved) a
one-line reason. Results go to the separate `scores` table.

Key properties:
  * Measures the BOT ("Tri"), not human agents. If a human took over, that is a
    bot non-resolution, not a success.
  * The output is an ESTIMATE from an LLM judge — calibrate before trusting it.
  * Structured outputs guarantee valid JSON (no fragile parsing).
  * Idempotent: only scores conversations without a row in `scores`, unless
    --rescore is passed. Re-running never double-counts (scores upsert by id).

Usage:
  python score_conversations.py --limit 15      # score a small batch to eyeball
  python score_conversations.py                 # score all unscored bot convos
  python score_conversations.py --rescore       # re-score everything (e.g. new rubric)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Optional

import anthropic

import config
import db

# --- The rubric (stable → prompt-cached across the batch) ----------------------
SYSTEM_PROMPT = """You are a strict quality analyst for a customer-support bot named "Tri" that answers questions for a triathlon events company (Supertri). You will be given one support conversation. Judge how well THE BOT handled it.

You are measuring the bot, not the human support team. In the transcript, "Bot:" is Tri. "Agent:" is a human agent. "User:" is the customer.

Classify the bot's outcome as exactly one of:
- "resolved": the bot fully answered the user's question and the user needed nothing further. No human agent had to step in.
- "partial": the bot answered part of it but left a gap, an unanswered follow-up, or handed off after doing some useful work.
- "unresolved": the bot deflected, said it couldn't help, gave a wrong or off-topic answer, escalated to a human without answering ("I'll get a teammate…", "connect you with a human"), or the user expressed dissatisfaction. If a human agent had to answer the actual question, the BOT did not resolve it — even if the customer ended up happy.
- "no_question": the user did not ask an answerable support question at all — e.g. an automated reply or out-of-office bounce, a bare acknowledgement ("thanks!", "got it"), spam, or a marketing/newsletter reply with no request. These are excluded from the resolution rate, so use this label instead of forcing a resolved/partial/unresolved judgement on a non-question.

Also provide:
- confidence: your confidence in the resolution label, 0.0 to 1.0.
- primary_topic: a short lowercase tag for what the user was asking about, 1-2 words (e.g. "wave times", "refund", "bib pickup", "registration", "distance change"). Prefer reusing common tags over inventing granular ones. For "no_question", use "none".
- unanswered_reason: for "partial" or "unresolved", one short line on what the bot failed to answer. For "resolved" or "no_question", use an empty string.

Base your judgement only on the transcript provided. Return your assessment via the required output format."""

# JSON schema for structured output. Numeric min/max aren't supported here, so
# confidence is clamped to 0..1 in code after the model returns.
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {"type": "string",
                       "enum": ["resolved", "partial", "unresolved", "no_question"]},
        "confidence": {"type": "number"},
        "primary_topic": {"type": "string"},
        "unanswered_reason": {"type": "string"},
    },
    "required": ["resolution", "confidence", "primary_topic", "unanswered_reason"],
    "additionalProperties": False,
}

# Strip zero-width / soft-hyphen junk that bloats forwarded marketing emails.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "­​‌‍﻿͏"), None
)


def clean_text(text: str) -> str:
    """Light, safe cleanup: drop zero-width chars, collapse blank runs, truncate.

    Deliberately does NOT strip quoted email chains — the bot's reply is a
    separate turn appended after the user's, so cutting at 'On … wrote:' risks
    removing the bot's answer. Deeper quoted-reply stripping is a future refinement.
    """
    text = text.translate(_ZERO_WIDTH)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > config.JUDGE_MAX_CHARS:
        text = text[: config.JUDGE_MAX_CHARS] + "\n...[truncated]"
    return text


def score_one(client: "anthropic.Anthropic", row: Dict) -> Optional[Dict]:
    """Score a single conversation. Returns a scores row, or None on failure."""
    transcript = clean_text(row.get("full_text") or "")
    if not transcript:
        return None

    user_content = (
        f"Channel: {row.get('channel')}\n"
        f"A human agent joined: {bool(row.get('ended_with_human'))}\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        response = client.messages.create(
            model=config.JUDGE_MODEL,
            max_tokens=config.JUDGE_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # stable prefix → cached across batch
            }],
            output_config={
                "effort": config.JUDGE_EFFORT,
                "format": {"type": "json_schema", "schema": JUDGE_SCHEMA},
            },
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as e:
        print(f"  ! API error on {row['conversation_id']}: {e}", file=sys.stderr)
        return None

    if response.stop_reason == "refusal":
        print(f"  ! judge refused on {row['conversation_id']}", file=sys.stderr)
        return None

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        print(f"  ! no JSON returned for {row['conversation_id']} "
              f"(stop_reason={response.stop_reason})", file=sys.stderr)
        return None

    data = json.loads(text)  # structured outputs guarantee valid JSON
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    resolution = data["resolution"]
    reason = data.get("unanswered_reason") or ""
    if resolution in ("resolved", "no_question"):
        reason = ""

    return {
        "conversation_id": row["conversation_id"],
        "resolution": resolution,
        "confidence": confidence,
        "primary_topic": (data.get("primary_topic") or "").strip().lower(),
        "unanswered_reason": reason,
        "scored_at": datetime.now(timezone.utc),
        "model": config.JUDGE_MODEL,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score bot conversations with the LLM judge.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Score at most this many conversations (use for a first eyeball).")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score conversations that already have a score.")
    args = parser.parse_args()

    config.require(config.ANTHROPIC_REQUIRED_VARS)
    db.init_db()

    todo = db.conversations_to_score(rescore=args.rescore, limit=args.limit)
    if todo.empty:
        print("Nothing to score.")
        return

    client = anthropic.Anthropic()
    print(f"Scoring {len(todo)} conversation(s) with {config.JUDGE_MODEL} "
          f"(effort={config.JUDGE_EFFORT})...\n")

    scored = 0
    for row in todo.to_dict("records"):
        result = score_one(client, row)
        if result is None:
            continue
        db.upsert_scores([result])
        scored += 1
        topic = result["primary_topic"]
        reason = f" — {result['unanswered_reason']}" if result["unanswered_reason"] else ""
        print(f"  {result['conversation_id']:>8}  {result['resolution']:<10} "
              f"conf={result['confidence']:.2f}  [{topic}]{reason}")

    print(f"\nDone. Scored {scored}/{len(todo)}.")


if __name__ == "__main__":
    main()
