"""Stage 3a — export a random sample of scored conversations for hand-labelling.

The reviewer reads each conversation and writes their own resolution label. This
tells us how far to trust the judge before quoting its numbers.

BLIND ON PURPOSE: the judge's label is deliberately NOT included in the export,
so the human isn't anchored to it. compare.py joins the two back together after.

Output: calibration/review_sample.csv with a blank `human_label` column.
Refuses to overwrite an existing file (so you can't clobber labels) unless --force.

Usage:
  python calibration/sample_for_review.py [--n 50] [--force]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make the project root importable so `import db`/`config` work from this subdir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent / "review_sample.csv"
VALID_LABELS = "resolved | partial | unresolved | no_question"


def _readable(full_text: str, limit: int = 4000) -> str:
    """Trim a transcript to something a human can skim in a spreadsheet cell."""
    text = re.sub(r"\n{3,}", "\n\n", (full_text or "").strip())
    return text[:limit] + (" …[truncated]" if len(text) > limit else "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export scored conversations for hand-labelling.")
    parser.add_argument("--n", type=int, default=config.CALIBRATION_SAMPLE_SIZE,
                        help=f"Sample size (default {config.CALIBRATION_SAMPLE_SIZE}).")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing review_sample.csv (loses any labels in it).")
    parser.add_argument("--raw", action="store_true",
                        help="Sample from raw (un-scored) bot conversations, so you can "
                             "build a gold set BEFORE scoring. compare.py joins to scores later.")
    args = parser.parse_args()

    if OUTPUT_PATH.exists() and not args.force:
        print(f"{OUTPUT_PATH} already exists. Fill it in, or pass --force to regenerate.")
        return

    if args.raw:
        source = db.bot_conversations()
        if source.empty:
            print("No bot conversations yet. Run pull_conversations.py first.")
            return
    else:
        source = db.scored_conversations()
        if source.empty:
            print("No scored conversations yet. Run score_conversations.py first "
                  "(or use --raw to label before scoring).")
            return

    n = min(args.n, len(source))
    sample = source.sample(n=n, random_state=None).copy()

    out = sample[["conversation_id", "channel"]].copy()
    out["transcript"] = sample["full_text"].map(_readable)
    out["human_label"] = ""   # reviewer fills this
    out["human_notes"] = ""   # optional free text
    out.to_csv(OUTPUT_PATH, index=False)

    print(f"Wrote {n} conversations to {OUTPUT_PATH}")
    print(f"\nFill in the `human_label` column with one of: {VALID_LABELS}")
    print("Judge yourself whether the BOT resolved it (a human agent answering = not resolved).")
    print("Leave rows blank to skip them. Then run: python calibration/compare.py")


if __name__ == "__main__":
    main()
