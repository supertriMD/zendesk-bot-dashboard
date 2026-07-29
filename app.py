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


def containment_rate(df: pd.DataFrame) -> float:
    """Share closed WITHOUT a human replying — the 'reduced human workload' metric."""
    if df.empty:
        return 0.0
    return (~df["ended_with_human"]).mean()


def contained_answered_share(df: pd.DataFrame) -> float:
    """Of contained conversations, the share the bot actually answered (resolved/partial).
    Distinguishes a real containment win from a contained-but-unanswered drop-off."""
    contained = answerable(df[~df["ended_with_human"]])
    if contained.empty:
        return 0.0
    return contained["resolution"].isin(["resolved", "partial"]).mean()


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
# Email is the only channel we can measure a true resolution rate for: every
# email becomes a ticket. Web chat only creates a ticket when it ESCALATES, so
# our web-chat rows are the escalated subset only — shown as a gap feed (a count),
# never as a resolution rate.
email_all = df[df["surface"] == "Email"]
email_ans = answerable(email_all)
webchat_all = df[df["surface"] == "Website"]
n_excluded = len(email_all) - len(email_ans)

# Containment is the headline — it matches the bot's goal (reduce human workload).
st.subheader("Work handled without a human (containment)")
cc = st.columns(4)
cc[0].metric("Email — no human needed", f"{containment_rate(email_all):.0%}",
             help="Share of email bot conversations closed without any human agent "
                  "replying. This is the 'reduced workload' metric.")
cc[1].metric("↳ of those, bot answered", f"{contained_answered_share(email_all):.0%}",
             help="Quality of that containment: of the contained emails, the share where "
                  "the bot gave a real answer (resolved or partial). The rest are "
                  "contained-but-unanswered — possible drop-offs to watch.")
cc[2].metric("Web chat — no human needed", f"{containment_rate(webchat_all):.0%}",
             help="FLOOR only — bot-resolved chats never create a Zendesk ticket, so the "
                  "true web-chat containment is higher than this.")
cc[3].metric("↳ of those, bot answered", f"{contained_answered_share(webchat_all):.0%}")

n_e_cont = int((~email_all["ended_with_human"]).sum())
n_w_cont = int((~webchat_all["ended_with_human"]).sum())
st.caption(
    f"Email: {n_e_cont} of {len(email_all)} closed without a human. "
    f"Web chat: {n_w_cont} of {len(webchat_all)} — escalated tickets only, so a floor "
    "(bot-resolved chats aren't in ticket data). Measuring true web-chat containment "
    "needs the messaging-layer source."
)

# Answer quality — estimated resolution (email is the unbiased channel).
st.subheader("Answer quality — estimated resolution (email)")
c1, c2, c3 = st.columns(3)
c1.metric("Estimated resolution rate (email)", f"{resolution_rate(email_all):.0%}",
          help="Of answerable email conversations, the share the judge rated fully "
               "'resolved'. Estimated by the LLM judge, not calibrated.")
c2.metric("Email conversations", f"{len(email_ans):,}")
c3.metric("Web chat escalations", f"{len(webchat_all):,}",
          help="Escalated web-chat conversations — a content-gap feed, not a resolution rate.")

breakdown = (email_ans["resolution"].value_counts().reindex(RESOLUTION_ORDER, fill_value=0))
excluded_note = f"  ·  {n_excluded} excluded as non-questions" if n_excluded else ""
st.caption(
    f"Email breakdown — resolved: {breakdown['resolved']}, "
    f"partial: {breakdown['partial']}, unresolved: {breakdown['unresolved']}{excluded_note}"
)

# --- Trend over time (email) ---------------------------------------------------
st.subheader("Email resolution rate over time")
weekly = (
    email_all.assign(week=email_all["created_at"].dt.to_period("W").dt.start_time)
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

# --- How the backlog gets solved (resolution path) ----------------------------
paths = df[df["resolution_path"].notna()]
if not paths.empty:
    st.subheader("How the backlog gets solved")
    total_p = len(paths)
    counts_p = paths["resolution_path"].value_counts()
    labels_p = {"active_lookup": "ACTIVE lookup", "content": "Content / KB", "human": "Needs a human"}
    pc = st.columns(3)
    for col, key in zip(pc, ["active_lookup", "content", "human"]):
        n = int(counts_p.get(key, 0))
        col.metric(labels_p[key], f"{n}",
                   help=f"{n/total_p:.0%} of the {total_p} classified backlog conversations. "
                        + {"active_lookup": "An integration reading the athlete's ACTIVE record would resolve it.",
                           "content": "A KB article or pointer would resolve it — no per-athlete lookup.",
                           "human": "Genuinely needs a person (exception, judgement call)."}[key])

    def _top_path(path: str, k: int = 6) -> pd.DataFrame:
        t = paths[paths["resolution_path"] == path]["primary_topic"].value_counts().head(k)
        return t.rename_axis("theme").reset_index(name="conversations")

    lc, rc = st.columns(2)
    with lc:
        st.markdown("**What an ACTIVE integration would resolve**")
        st.dataframe(_top_path("active_lookup"), use_container_width=True, hide_index=True)
    with rc:
        st.markdown("**What content / KB would resolve**")
        st.dataframe(_top_path("content"), use_container_width=True, hide_index=True)
    st.caption(
        "Each partial/unresolved conversation classified by how it should be solved. "
        "**Estimated by the LLM classifier, not calibrated — the 'human' bucket is pending "
        "owner validation** (`calibration/validate_human_bucket.py`)."
    )

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
