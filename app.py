"""Stage 4 — the Streamlit dashboard.

Read-only view over the scored store (via db.py). Shows:
  1. Estimated resolution rate (labelled ESTIMATED everywhere), overall + by channel
  2. The rate over time
  3. The top 5 themes the bot could NOT answer — the content backlog
  4. A date filter and a per-theme drill-down

Run:  streamlit run app.py
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

import config
import db

# The three "answerable" outcomes. 'no_question' is scored but excluded from the
# resolution rate so non-questions (auto-replies, 'thanks', spam) don't distort it.
RESOLUTION_ORDER = ["resolved", "partial", "unresolved"]
ANSWERABLE = RESOLUTION_ORDER


# --- Data ----------------------------------------------------------------------
@st.cache_data(ttl=120)
def load_data() -> pd.DataFrame:
    df = db.scored_conversations()
    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created_at"])
        # Email vs Website (the split the ops team cares about). Messaging + web
        # widget are both "website"; email tickets are "email".
        df["surface"] = df["channel"].map(lambda c: "Email" if c == "email" else "Website")
    return df


def first_user_question(full_text: str) -> str:
    """Pull the first thing the user actually asked, for theme examples."""
    for line in (full_text or "").splitlines():
        line = line.strip()
        if line.lower().startswith("user:"):
            q = line[len("user:"):].strip()
            if q:
                return q[:200]
    return "(no user message found)"


def answerable(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with a real question — excludes 'no_question'. The rate's denominator."""
    return df[df["resolution"].isin(ANSWERABLE)]


def resolution_rate(df: pd.DataFrame) -> float:
    a = answerable(df)
    if a.empty:
        return 0.0
    return (a["resolution"] == "resolved").mean()


# --- Page ----------------------------------------------------------------------
st.set_page_config(page_title="Zendesk Bot Performance", page_icon="🤖", layout="wide")
st.title("🤖 Zendesk Bot Performance")

st.warning(
    "**These numbers are ESTIMATES from an LLM judge, not ground truth.** "
    "Each conversation was scored automatically by Claude. Calibrate against "
    "hand-labelled data (see `calibration/`) before quoting the resolution rate."
)

data = load_data()

if data.empty:
    st.info(
        "No scored conversations yet.\n\n"
        "Run the pipeline first: `python pull_conversations.py` then "
        "`python score_conversations.py`. This dashboard will populate once "
        "scores exist."
    )
    st.stop()

# --- Filters -------------------------------------------------------------------
min_d, max_d = data["created_at"].min().date(), data["created_at"].max().date()
with st.sidebar:
    st.header("Filters")
    date_range = st.date_input(
        "Conversation date range", value=(min_d, max_d),
        min_value=min_d, max_value=max_d,
    )
    st.caption(f"Judge model: `{data['model'].iloc[0]}`")

if isinstance(date_range, tuple) and len(date_range) == 2:
    start, end = date_range
    mask = (data["created_at"].dt.date >= start) & (data["created_at"].dt.date <= end)
    df = data[mask].copy()
else:
    df = data.copy()

if df.empty:
    st.info("No conversations in the selected date range.")
    st.stop()

# --- Headline ------------------------------------------------------------------
st.subheader("Estimated resolution rate")
c1, c2, c3, c4 = st.columns(4)
ans = answerable(df)
n_excluded = len(df) - len(ans)
email_df, web_df = ans[ans["surface"] == "Email"], ans[ans["surface"] == "Website"]
c1.metric("Estimated resolution rate", f"{resolution_rate(df):.0%}",
          help="Share of answerable bot conversations the judge rated 'resolved'. Estimated — not calibrated.")
c2.metric("Answerable conversations", f"{len(ans):,}")
c3.metric("Email", f"{resolution_rate(email_df):.0%}" if len(email_df) else "—",
          help=f"{len(email_df)} email conversations")
c4.metric("Website", f"{resolution_rate(web_df):.0%}" if len(web_df) else "—",
          help=f"{len(web_df)} web/messaging conversations")

# Three-way breakdown (excludes no_question)
breakdown = (ans["resolution"].value_counts().reindex(RESOLUTION_ORDER, fill_value=0))
excluded_note = (f"  ·  {n_excluded} excluded as non-questions"
                 if n_excluded else "")
st.caption(
    f"Breakdown — resolved: {breakdown['resolved']}, "
    f"partial: {breakdown['partial']}, unresolved: {breakdown['unresolved']}"
    f"{excluded_note}"
)

# --- Trend over time -----------------------------------------------------------
st.subheader("Resolution rate over time")
weekly = (
    df.assign(week=df["created_at"].dt.to_period("W").dt.start_time)
      .groupby("week")
      .apply(lambda g: pd.Series({
          "Estimated resolution rate": resolution_rate(g),
          "conversations": len(g),
      }))
)
if len(weekly) >= 2:
    st.line_chart(weekly["Estimated resolution rate"])
else:
    st.caption("Not enough history yet for a trend (need at least two weeks).")

# --- Top 5 unanswered themes ---------------------------------------------------
st.subheader("Top 5 things the bot couldn't answer")
st.caption("Unresolved + partial conversations, grouped by topic. This is the content backlog.")

gaps = df[df["resolution"].isin(["unresolved", "partial"])].copy()
if gaps.empty:
    st.success("No unresolved or partial conversations in this range.")
else:
    themes = (
        gaps.groupby("primary_topic")
        .agg(count=("conversation_id", "size"),
             example=("full_text", lambda s: first_user_question(s.iloc[0])))
        .sort_values("count", ascending=False)
        .head(5)
        .reset_index()
        .rename(columns={"primary_topic": "theme", "example": "example question"})
    )
    st.dataframe(themes, use_container_width=True, hide_index=True)

    # --- Drill-down ------------------------------------------------------------
    st.subheader("Drill into a theme")
    choice = st.selectbox("Theme", themes["theme"].tolist())
    detail = gaps[gaps["primary_topic"] == choice]
    st.caption(f"{len(detail)} conversation(s) tagged '{choice}'")
    for row in detail.itertuples():
        label = f"{row.conversation_id} · {row.surface} · {row.resolution}"
        with st.expander(label):
            if row.unanswered_reason:
                st.markdown(f"**Why unresolved:** {row.unanswered_reason}")
            st.text(re.sub(r"\n{3,}", "\n\n", (row.full_text or "").strip())[:6000])

st.divider()
st.caption(
    "Resolution is judged from the bot's perspective — a conversation a human "
    "agent had to answer counts as *not* resolved by the bot. All figures are "
    "LLM estimates pending calibration."
)
