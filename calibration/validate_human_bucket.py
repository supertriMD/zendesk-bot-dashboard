"""Validate the 'human' resolution-path bucket by eyeball.

The classifier put 43% of the backlog in "human" (needs a person). That's large,
decision-critical, and uncalibrated — a classifier can over-assign "human" to
messy-looking tickets. This exports a sample for the owner to check, then compares
their verdicts back and reports the corrected split.

Usage:
  python calibration/validate_human_bucket.py [--n 25] [--force]   # export sample
  python calibration/validate_human_bucket.py --compare            # read back verdicts
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent / "human_review.csv"
PATHS = ["content", "active_lookup", "other"]  # what a disagreement could reclassify to


def _readable(full_text: str, limit: int = 4000) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (full_text or "").strip())
    return text[:limit] + (" …[truncated]" if len(text) > limit else "")


def export(n: int, force: bool) -> None:
    if OUTPUT_PATH.exists() and not force:
        print(f"{OUTPUT_PATH} already exists. Fill it in, or pass --force to regenerate.")
        return
    sample = db.sample_by_path("human", limit=n)
    if sample.empty:
        print("No 'human'-classified conversations found. Run classify_backlog.py first.")
        return
    out = sample[["conversation_id", "channel", "primary_topic"]].copy()
    out["classifier_reason"] = sample["path_reason"]
    out["transcript"] = sample["full_text"].map(_readable)
    out["agree_human"] = ""   # owner fills: yes / no
    out["correct_path"] = ""  # if no: content / active_lookup / other
    out.to_csv(OUTPUT_PATH, index=False)
    print(f"Wrote {len(out)} 'human'-classified conversations to {OUTPUT_PATH}")
    print("\nFor each row, set `agree_human` to yes/no (does this genuinely need a person?).")
    print(f"If no, set `correct_path` to one of: {' / '.join(PATHS)}")
    print("Then run: python calibration/validate_human_bucket.py --compare")


def compare() -> None:
    if not OUTPUT_PATH.exists():
        print(f"{OUTPUT_PATH} not found. Run the export first.")
        return
    r = pd.read_csv(OUTPUT_PATH, dtype=str).fillna("")
    r["agree_human"] = r["agree_human"].str.strip().str.lower()
    labelled = r[r["agree_human"].isin(["yes", "no"])]
    if labelled.empty:
        print("No verdicts found. Fill `agree_human` with yes/no and re-run.")
        return

    n = len(labelled)
    agree = (labelled["agree_human"] == "yes").sum()
    agree_pct = agree / n
    print(f"Checked {n} 'human'-classified conversation(s).")
    print(f"\nOwner agrees it needs a human: {agree}/{n} = {agree_pct:.0%}")

    disagree = labelled[labelled["agree_human"] == "no"]
    if not disagree.empty:
        print(f"\nReclassified by owner ({len(disagree)}):")
        print(disagree["correct_path"].str.strip().str.lower().value_counts().to_string())

    # Extrapolate the correction to the full 'human' bucket.
    total_human = int(db.query_df(
        "SELECT count(*) n FROM resolution_paths WHERE resolution_path='human'").n.iloc[0])
    est_true_human = round(total_human * agree_pct)
    total_backlog = int(db.query_df("SELECT count(*) n FROM resolution_paths").n.iloc[0])
    print(f"\nExtrapolated to the full bucket ({total_human} 'human' of {total_backlog} backlog):")
    print(f"  estimated GENUINELY human: ~{est_true_human}  ({est_true_human/total_backlog:.0%} of backlog)")
    print(f"  estimated over-assigned (really content/lookup): ~{total_human - est_true_human}")
    print("\n(Sample-based estimate — wider sample = tighter number.)")


def main() -> None:
    p = argparse.ArgumentParser(description="Validate the 'human' resolution-path bucket.")
    p.add_argument("--n", type=int, default=25)
    p.add_argument("--force", action="store_true")
    p.add_argument("--compare", action="store_true")
    args = p.parse_args()
    if args.compare:
        compare()
    else:
        export(args.n, args.force)


if __name__ == "__main__":
    main()
