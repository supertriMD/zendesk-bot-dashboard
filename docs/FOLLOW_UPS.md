# Follow-ups & ideas (not in scope for the current build stage)

Parked items to revisit. Nothing here is being built yet.

## Swim wave allocation → ACTIVE → Zendesk (Michael, 14 Jul 2026)

**Observation:** swim wave allocation is one of the most-asked participant questions.
That data lives in **ACTIVE** (the registration system), so it can be pulled from
ACTIVE and surfaced to participants via Zendesk.

**Why it matters here:** this is a concrete instance of the Part-1 → Part-2 loop —
the dashboard's "top-5 things the bot couldn't answer" becomes a content-backlog
item, and this is very likely to be one of them. Worth watching for once real
scoring data lands.

**One nuance to resolve before building it:** wave allocation is **per-participant
data, not a static KB fact.** It differs from normal KB content (same answer for
everyone). So surfacing it is one of:
  - a **published start list / wave chart** per event (public, same-for-all — fits the CMS/KB model), or
  - a **personalized lookup** ("what's *my* wave?") — needs the athlete keyed to their
    ACTIVE record, which is an authenticated/identity step the current read-only,
    aggregate dashboard does not do.

Decide which before wiring anything: the first is a content-publish job; the second
is a lookup/integration with different privacy and auth implications.

**Depends on:** confirming the wave-allocation field exists and is reliable in the
ACTIVE payload (the ACTIVE payload inventory work already underway on the analytics side).

**Update (28 Jul 2026):** confirmed by data — "wave times" is a top-3 unanswered theme
on both email and web chat. This is a real, high-volume content gap, not a hunch.

## NEXT DATA-SOURCE TASK: measure web-chat bot success (28 Jul 2026)

**The problem — web-chat resolution is not measurable from Zendesk tickets.**
We pull *tickets*. Zendesk messaging (the "Tri" web chat) only creates a ticket when a
conversation **escalates to a human**. Chats the bot fully resolves are closed in the
messaging widget and never become tickets — so they never enter our pull.

Evidence (28 Jul): 146 of 147 web-chat tickets contain a handoff phrase; 0 were judged
resolved. That is selection bias, not bot failure — our web-chat rows are, by
construction, the escalated subset (the failures). Participants report the bot *does*
successfully answer web-chat questions; those chats simply aren't in ticket data.

**Consequence, now handled in the app:** the dashboard reports the resolution rate on
**email only** (unbiased — every email is a ticket) and shows web chat as an
**escalations feed / content-gap source**, never as a resolution rate.

**The task:** to measure the web-chat bot's *true* success rate we need the messaging
layer itself, which contains ALL chats including bot-resolved ones. Options to scope:
  - **Zendesk bot / conversation-analytics** (Zendesk's own bot performance reporting), or
  - the **Sunshine Conversations API** (the messaging substrate; all conversations, not
    just ticketed), or
  - a **Zendesk messaging metrics / events export**.
Check what the current Zendesk plan exposes. This is a new *reader* (additive), separate
from `pull_conversations.py`, landing into the same store shape so the dashboard can
show a real web-chat rate alongside email.

**Also fixed 28 Jul (rubric):** the judge now scores "bot answered, then offered/handed to
a human" as resolved/partial (not unresolved) — a handoff after a real answer is not a
failure. This lifted some email labels too; all conversations were re-scored under the
new rubric.

## Backlog resolution paths — content vs ACTIVE integration (owner rules, 29 Jul 2026)

Michael's domain classification of what each backlog theme actually needs. The headline:
**an ACTIVE lookup integration is the high-leverage fix** (the ACTIVE API is available), and
it's the same primitive behind the top unengaged questions.

| Theme | Path | Note |
|---|---|---|
| wave times / start time | **ACTIVE lookup** | data is in ACTIVE |
| confirmation email / registration details | **ACTIVE lookup** | in ACTIVE |
| "am I registered" / "did my payment go through" / which option/distance I chose | **ACTIVE lookup** | per-athlete record |
| results | **Content** | patience until published; if wrong, give the timer's email to correct |
| transfers | **Content / how-to** | references their ACTIVE profile but the athlete does it manually — bot explains how |
| deferral, refund, air quality, athlete guide, parking | **Content** | static policy / KB |

Three actionable buckets for prioritisation:
- **A — ACTIVE lookup** (bot fetches athlete data): wave/start times, confirmation & registration
  details, registration/payment status, chosen options/distance.
- **B — Content/KB** (static answer or pointer): results, transfers (self-serve how-to), deferral,
  refund/air-quality policy, athlete guide, parking.
- **C — genuine human** (bot shouldn't answer): sensitive exceptions.

**Open quantification:** run the LLM classifier over the backlog with this A/B/C rubric to get the
exact volume/% an ACTIVE integration would resolve — the ROI number for the integration decision.
