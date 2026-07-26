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
