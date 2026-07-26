"""Stage 3b — compare human labels against the judge's labels.

Reads the filled-in review_sample.csv, joins each row back to the judge's stored
label by conversation_id, and reports:
  * overall agreement %
  * a confusion breakdown (where human and judge diverge)

This is the number that decides whether the headline resolution rate can be
quoted to anyone. Aim for agreement above the trust threshold (config).

Usage:
  python calibration/compare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402

REVIEW_PATH = Path(__file__).resolve().parent / "review_sample.csv"
LABELS = ["resolved", "partial", "unresolved", "no_question"]


def main() -> None:
    if not REVIEW_PATH.exists():
        print(f"{REVIEW_PATH} not found. Run sample_for_review.py and fill it in first.")
        return

    review = pd.read_csv(REVIEW_PATH, dtype={"conversation_id": str})
    review["human_label"] = review["human_label"].astype(str).str.strip().str.lower()
    labelled = review[review["human_label"].isin(LABELS)].copy()

    if labelled.empty:
        print("No valid human labels found. Fill `human_label` with "
              f"{' / '.join(LABELS)} and re-run.")
        return

    # Join to the judge's labels.
    scores = db.scored_conversations()[["conversation_id", "resolution"]].copy()
    scores["conversation_id"] = scores["conversation_id"].astype(str)
    merged = labelled.merge(
        scores.rename(columns={"resolution": "judge_label"}),
        on="conversation_id", how="inner",
    )

    missing = len(labelled) - len(merged)
    if merged.empty:
        print("None of the labelled conversations have a judge score to compare against.")
        return

    merged["agree"] = merged["human_label"] == merged["judge_label"]
    agreement = merged["agree"].mean()
    n = len(merged)

    print(f"Compared {n} hand-labelled conversation(s)"
          + (f" ({missing} skipped: no matching judge score)" if missing else ""))
    print(f"\nAgreement: {agreement:.1%}  ({merged['agree'].sum()}/{n})")
    threshold = config.CALIBRATION_TRUST_THRESHOLD
    verdict = "ABOVE" if agreement >= threshold else "BELOW"
    print(f"Trust threshold: {threshold:.0%} → agreement is {verdict} it.")
    if agreement < threshold:
        print("=> Do not quote the headline resolution rate yet. Tune the rubric and re-score.")

    # Confusion matrix: rows = human (truth), cols = judge.
    print("\nConfusion (rows = your label, cols = judge):")
    confusion = pd.crosstab(
        merged["human_label"], merged["judge_label"],
        rownames=["human"], colnames=["judge"], dropna=False,
    ).reindex(index=LABELS, columns=LABELS, fill_value=0)
    print(confusion.to_string())

    # Where they disagree — the rows worth reading.
    disagree = merged[~merged["agree"]]
    if not disagree.empty:
        print(f"\n{len(disagree)} disagreement(s):")
        for r in disagree.itertuples():
            print(f"  {r.conversation_id}: you={r.human_label}  judge={r.judge_label}")


if __name__ == "__main__":
    main()
