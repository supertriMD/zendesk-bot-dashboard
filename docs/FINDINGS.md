# Findings — first full scoring run (24 Jul 2026)

**Extracted from the 24 Jul working session** so it survives the chat. These are the first real
results off the scoring pipeline.

> ⚠ **ESTIMATED AND UNCALIBRATED — do not quote externally yet.** Every number below is Claude's
> judgement of whether a conversation was resolved, with **no human agreement check yet**. The gate
> for treating these as real is the calibration run described at the bottom (>80% human-vs-Claude
> agreement). Until then they are directional: good enough to steer the content backlog, not good
> enough to put in a board pack or share with Zendesk/agency partners.

## Headline

**421 bot conversations scored** (model: Opus). Of the **400 answerable** ones — 21 were
`no_question` and are excluded from the rates:

| Verdict | Count |
|---|--:|
| Resolved | 146 |
| Partial | 96 |
| Unresolved | 158 |

**Estimated resolution ≈ 36%.**

## The finding that actually matters: channel split

| Channel | Resolution |
|---|--:|
| **Email** | **≈42% resolved** |
| **Website (Tri web-chat)** | **≈0% resolved** |

**The website bot is a triage layer, not a resolver** — 53 of 54 website conversations end in a
handoff to a human, averaging ~10 turns first. This was checked for the obvious artefacts (it is not
a scoring quirk or a tiny-sample effect within that channel).

That reframes the question from "why is the bot only resolving a third?" to "should the website bot
be *expected* to resolve, or should it be measured on clean, fast handoff?" — a design decision, not
a tuning problem. Worth settling before optimising anything.

## Top-5 content backlog (what the bot could not answer)

| Rank | Theme | Conversations |
|---|---|--:|
| 1 | Distance change | 27 |
| 2 | Registration | 26 |
| 3 | **Wave times** | 23 |
| 4 | Refund | 14 |
| 5 | Athlete guide | 11 |

**Wave times (#3) independently confirms the hunch already parked in `FOLLOW_UPS.md`** — swim wave
allocation is one of the most-asked questions and the data lives in ACTIVE, so it can be pulled and
surfaced rather than answered by hand. That parked item now has evidence behind it.

## Where this sits in the workflow

**Blocked on calibration, and that is the next step.** A gold set of **30 blind conversations** is
staged at `calibration/review_sample.csv`, currently **unlabelled** — it needs a human pass from
Michael. Valid labels are `resolved` / `partial` / `unresolved` / `no_question`. Then `compare.py`
reports human-vs-Claude agreement; **>80% is the bar** before the 36% (or any number here) is quoted
as real.

The calibration *mechanism* is documented in the README; this file records the *state* — scored 421,
gold set staged, waiting on labelling.
