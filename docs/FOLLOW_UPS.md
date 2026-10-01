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

## What else Zendesk can give us — checked against our account (30 Sep 2026)

Read-only API check (subscription, settings, ticket fields; field fill-rates over the 1,000 most
recent tickets, 28 Aug–30 Sep 2026, counts only).

**Our account:** Support **Team** plan (not Suite), 4 agents, term ends 4 Jan 2027. One brand,
one ticket form. CSAT is **off** (0 ratings). SLA policies: not available (403). So Explore
Professional, SLAs, intelligent triage (topic/sentiment) and the new CSAT survey are all out on
this plan. The bot is **Ultimate, i.e. an AI agents – Advanced agent** (tag `escalated_by_ultimate`).

**The finding that matters: the tickets already carry the bot outcome, and we throw it away.**
`pull_conversations.py` stores neither `tags` nor `custom_fields`. Zendesk itself fills:
- `Resolution tier` on 67% of tickets: core resolution 325 · assisted escalation 174 ·
  non-automated 109 · contained resolution 66 (of 1,000).
- `Resolution type` = automated on 325.
- `Which event can we help you with?` on 14% (Kerrville 52, Chicago 45, general 12, Toulouse 6,
  Blenheim 6, Toronto 5). The bot also tags events itself (`ai_toulouse`, `ai_chicago`, …).
- Channel mix is now email 422 · native messaging 383 · web 195. Messaging conversations,
  including bot-contained ones, now arrive as tickets, which partly closes the web-chat gap above.

**To do, in order:**
1. **Store tags + the custom fields** (resolution tier/type, event, race division) in
   `conversations`. Additive columns, same pull, no new API. Gives Zendesk's own bot-resolution
   verdict per ticket (cross-check for our LLM judge) and support demand per event.
2. **AI Agents Data Export API** (`/ai-agents/api/data-export/v3/get-signed-urls`): one row per bot
   conversation incl. ones that never ticket, with resolution tier, BSAT and intents. Advanced
   agents qualify; needs an API key from the AI agents dashboard ▸ Organization management.
   Note the 18 May–1 Jun 2026 redefinition (Contained vs Verified) breaks any trend there.
3. **Ticket metrics** (`/api/v2/ticket_metrics`): first-reply / resolution time / reopens per
   event. Works on Team. No SLA events (no SLA policies on this plan).
4. **Owner decisions, not builds:** switch CSAT on (Team supports the legacy survey); make the
   event field required on the form; link requester to athlete by hashed email for a
   repeat-rate test (privacy call, aggregates only).

### AI agents setup, read from the dashboard (30 Sep 2026)
- Two Advanced AI agents, both active: **TRI** (email) bot id `6a4a9d689f8fdde94e39876f`, and
  **Tri** (messaging) bot id `6a41cee6fbfda88b1060d653`. The data export is per bot, so the
  reader must loop over BOTH ids (AI_AGENTS_BOT_ID takes a comma-separated list).
- Organization id `6a16bc521f475afb692a1abe` (AI agents ▸ Organization management).
- A "General purpose key" ALREADY EXISTS. It is shown only once and Regenerate invalidates the old
  one. Not in 1Password; who uses it is unknown (client admins: michael@, cathy.walker@).
- Conversation logs page has a manual **Export (XLSX)** per bot: the only route found to the TEXT
  of bot chats that never became a ticket. Manual, contains athlete PII.
- 30-day dashboard view: 752 conversations, 39% automated resolution; email bot 147 conv / 6% AR,
  messaging bot 59 conv / 64% AR (the totals don't reconcile on that screen; trust the export).

## Potential next steps — Zendesk questions data (Michael, 1 Oct 2026)

Built and live: daily Cloud Run job `zendesk-daily` (06:30 UK) → `ticket_attributes`, `ticket_metrics`,
`bot_conversations`, views `v_questions` + `v_question_topics`. First scheduled run failed on a
duplicate row in a growing bot-export file; fixed (`d92071b`, latest row per key) and re-run green.
Not started; each needs a go from Michael:

1. **Content-gap list for Paul (Space 3, athlete comms/CRM).** From `v_question_topics` + `resolution_path`:
   the not-fully-answered tickets that new/better help-centre content would fix (511 Jul–Sep: bib
   pickup, registration, athlete guide, refund, distance change, wave times), the 221 that need an
   ACTIVE lookup, and the bot-only chats with no article (371) or a not-understood message (~360).
   Output = a ranked backlog, aggregates only, no athlete details.
2. **Extend topic scoring to web-form tickets.** 1,142 web-form tickets (Jul–Sep) have no
   `primary_topic` because `score_conversations.py` only scores bot conversations. Add a topic-only
   pass for non-bot tickets (and optionally the bot-only chats via `automated_resolution_reasoning`).
   Small Claude spend; check cost before enabling daily.
3. **Make "Which event can we help you with?" required on the ticket form.** Zendesk config change
   (Paul/Robert decide). Today ~half of all questions carry no event.
4. **✅ DONE 1 Oct 2026 — management dashboard ▸ Athletes ▸ Support questions** (supertri_stage1 `support_board.py`, reads ONLY `zendesk_dash.v_support_questions`). Original note: A "Questions" view over `v_questions` / `v_question_topics`:
   volume by channel and event, answered share, top unanswered topics, the content/ACTIVE/human
   split, first-reply speed trend. Decide WHICH app first: the Zendesk bot dashboard (`app.py` in
   this repo, already reads `zendesk_bot`) or the management dashboard (supertri_stage1, Cloud Run
   + IAP, reads `supertri-reg-analytics` — would need cross-project read on `zendesk_bot`).
