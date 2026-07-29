"""Classify each backlog conversation by HOW it should be resolved.

Buckets (owner rules, see docs/FOLLOW_UPS.md):
  - active_lookup : the answer is a fact in the athlete's ACTIVE record the bot could
                    fetch (wave/start time, confirmation/registration details, "am I
                    registered", payment status, which option/distance chosen).
  - content       : a static answer, policy, or pointer resolves it — no per-athlete
                    lookup. Includes results (patience + timer email), transfers
                    (self-serve how-to in ACTIVE), deferral, refund/air-quality, guide, parking.
  - human         : genuinely needs a person (sensitive exception, judgement call).

Runs over partial/unresolved bot conversations. Idempotent (skips already-classified
unless --reclassify). Writes to the resolution_paths table.

Usage:
  python classify_backlog.py [--limit N] [--reclassify]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Dict, Optional

import anthropic

import config
import db
from score_conversations import clean_text  # reuse the same safe cleanup

SYSTEM_PROMPT = """You are triaging support conversations that a support bot (for a triathlon events company, Supertri) could NOT fully resolve. For each conversation, decide HOW it should be solved. Classify the resolution path as exactly one of:

- "active_lookup": the answer is a fact in THIS athlete's registration record that the bot could fetch from ACTIVE (the registration system) — their wave or start time, their confirmation or registration details, whether they are registered / their payment went through, which options or distance they chose. Choose this whenever a per-athlete data lookup would answer the question.
- "content": a static answer, policy, or pointer would resolve it, with NO per-athlete lookup. This includes: race results (results publish on a schedule; if they look wrong, contact the timer) ; how to transfer, defer, or change an entry (the athlete self-serves in ACTIVE — the bot only needs to explain how) ; refund and air-quality policy ; the athlete guide ; parking ; and general how-to. A "pointer" answer (giving an email address, a link, or step-by-step instructions) still counts as content.
- "human": genuinely needs a person — a sensitive exception, a judgement call, or something neither a data lookup nor an article/pointer can resolve.

Judge by what the USER actually needed. Return the path and a one-line reason via the required format."""

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "enum": ["active_lookup", "content", "human"]},
        "reason": {"type": "string"},
    },
    "required": ["path", "reason"],
    "additionalProperties": False,
}


def classify_one(client: "anthropic.Anthropic", row: Dict) -> Optional[Dict]:
    transcript = clean_text(row.get("full_text") or "")
    if not transcript:
        return None
    try:
        resp = client.messages.create(
            model=config.JUDGE_MODEL,
            max_tokens=config.JUDGE_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": config.JUDGE_EFFORT,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user",
                       "content": f"Topic: {row.get('primary_topic')}\n\nTranscript:\n{transcript}"}],
        )
    except anthropic.APIError as e:
        print(f"  ! API error on {row['conversation_id']}: {e}", file=sys.stderr)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return None
    data = json.loads(text)
    return {
        "conversation_id": row["conversation_id"],
        "resolution_path": data["path"],
        "path_reason": (data.get("reason") or "").strip(),
        "classified_at": datetime.now(timezone.utc),
        "model": config.JUDGE_MODEL,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify backlog by resolution path.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reclassify", action="store_true")
    args = parser.parse_args()

    config.require(config.ANTHROPIC_REQUIRED_VARS)
    db.init_db()
    todo = db.backlog_to_classify(reclassify=args.reclassify, limit=args.limit)
    if todo.empty:
        print("Nothing to classify.")
        return

    client = anthropic.Anthropic()
    print(f"Classifying {len(todo)} backlog conversation(s) with {config.JUDGE_MODEL}...\n")
    counts: Dict[str, int] = {}
    done = 0
    for row in todo.to_dict("records"):
        res = classify_one(client, row)
        if res is None:
            continue
        db.upsert_resolution_paths([res])
        counts[res["resolution_path"]] = counts.get(res["resolution_path"], 0) + 1
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(todo)} classified... {counts}")

    print(f"\nDone. Classified {done}/{len(todo)}.")
    print("Split:", counts)


if __name__ == "__main__":
    main()
