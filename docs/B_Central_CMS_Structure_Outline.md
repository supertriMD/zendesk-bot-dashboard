# Part 2 — Central Content System
## Structure Outline

*The system to manage athlete-guide + operational content centrally, feeding Zendesk, the website, the guide builder, and (optionally) an internal staff bot. **The store technology decision — Firestore vs Google Sheet — is deliberately HELD OPEN in this document.** Everything below is true either way; the one section that differs by choice is marked.*

---

## 1. The principle

**Single source, many outputs.** Content lives once, as structured data. Every output — Zendesk KB, website, printed guides, staff bot — is a render of that one source. Fix a fact once; every output updates. This is the same architecture as the registration data lakes (source → outputs), applied to content.

It exists to kill the drift we've repeatedly hit: the Palmer toilet error surviving edits, the para-awards wording differing across three documents, KBs falling out of date when decks change. Those are all symptoms of content living in its output format.

---

## 2. The backbone: content as structured records

Whatever the store, the content is modelled the same way — **one row/record per fact**, never a page of prose:

```
event | section | field           | value              | status     | owner | updated
------|---------|-----------------|--------------------|------------|-------|--------
LB    | swim    | sprint_distance | 750m               | confirmed  | ops   | ...
LB    | bike    | cutoff          | 10:15 AM           | confirmed  | ops   | ...
LB    | awards  | para_structure  | M/F within each... | confirmed  | ops   | ...
NJ    | swim    | olympic_start   | 07:55 AM           | confirmed  | ops   | ...
```

- **The schema = the existing 25-section KB template.** That template is already 90% of the data model — the design work is largely done.
- **Every fact carries a `status`:** confirmed / draft / open. **Only `confirmed` publishes** to public outputs. This makes "accuracy over speed" structural, not a rule people must remember — the accuracy register becomes a column, not a separate document.
- **Every fact carries an `owner` and `updated` timestamp** — the single-owner discipline, enforced by the data.

---

## 3. The four outputs (identical work regardless of store)

```
                 CONTENT STORE (Sheet or Firestore)
                 one record per fact + status
                          │
         ┌────────┬───────┴────────┬─────────────┐
         ▼        ▼                ▼             ▼
    Zendesk KB  Website        Guide builder  Internal
    (confirmed  (same data)    (PDF/InDesign) staff bot
     articles)                  athlete +      (query the
                                operational    store)
                                guides)
```

| Output | How it's fed | Notes |
|---|---|---|
| **Zendesk KB** | Publish job groups confirmed fields into formatted articles → Zendesk Help Center API. | Needs a light templating step (fields → readable article). |
| **Website** | Same job publishes a data feed (JSON/API) the site consumes. | **Depends on how supertri.com is built — must confirm the site can read a feed.** |
| **Guide builder** | Export confirmed fields → InDesign data merge (athlete guides) + simpler template (operational guides). | Cleanest fit — a guide is a document generated from a data table. |
| **Internal staff bot** | Bot queries the store for staff questions ("what's the Toronto bike cut-off?"). | **This is the one output that differs sharply by store choice — see §5.** |

**Key property:** the internal bot and the public Zendesk bot draw from the *same source*. No separate internal wiki to drift.

---

## 4. Who does what (roles)

- **One content owner** holds the pen — the only person who flips a fact to `confirmed`. Everyone else proposes/reads.
- **Ops team** edits drafts in whatever the editing layer is (Sheet UI, or CMS editor).
- **Publish** is a scheduled job or a button — not manual copy-paste to each destination.
- **The dashboard (Part 1)** feeds the backlog: its top-5 unanswered themes become `open` records to be filled.

---

## 5. THE ONE DECISION HELD OPEN: Google Sheet vs Firestore

Everything above is identical either way. This is the only fork. **Not deciding here — laying out the trade so it can be decided separately.**

| | Google Sheet as hub | Firestore (CMS) |
|---|---|---|
| Editing layer | The Sheet itself (ops already knows it) | Needs Rowy/FireCMS + light onboarding |
| In existing stack | ✅ Already owned | ❌ New service to run |
| Setup effort (Claude Code) | Low — days | Higher — weeks |
| Structure enforcement | By discipline only (a stray cell can corrupt) | By design (schema + validation reject bad data) |
| Status filter (confirmed-only) | Works by convention | Enforced natively |
| Feeds Zendesk / web / guides | ✅ All three, via a read script | ✅ All three, via API |
| **Internal staff bot** | ❌ Weak — a bot reading a spreadsheet is clumsy | ✅ Native MCP — bot queries directly and cleanly |
| Scales to 20+ events, many editors | 🔶 Gets fragile | ✅ Built for it |

**The deciding question:** *is the internal staff bot a real near-term need?*
- **Bot = nice-to-have later** → start with the **Sheet**. Faster, no new tooling, fits "no over-engineering." Structure it as one-row-per-fact so the Firestore upgrade later is near lift-and-shift.
- **Bot = real near-term need** → go straight to **Firestore**. It's the one capability a Sheet can't fake; retrofitting means rebuilding the store anyway.

**Migration path is friendly:** if the Sheet is modelled as structured records now, moving to Firestore later is a data-shape-preserving lift — not a rebuild. So starting with the Sheet does not foreclose Firestore.

---

## 6. The honest risks (same as discussed)

- **Sheet stays clean by discipline, not design.** [Certain] A Sheet will happily accept the next Palmer-type error; Firestore's validation structurally prevents it. If the drift problem is the thing you most want solved, that argues for Firestore — but at higher build cost.
- **Website leg depends on the site.** [Unverified] If supertri.com is a CMS (WordPress/Webflow) or reads an API, the feed is clean. If it's hand-edited HTML, the "feed" still needs a human paste — which doesn't solve website drift. **Confirm how the site is built before relying on this leg.**
- **Half-migration is the trap.** [Certain] Some events in the hub, some still in old docs = two sources of truth again. Whichever store: migrate all events, or none. Same lesson as the data-lake migration.
- **Operating cost, not just build cost.** [Certain] Whatever is built becomes load-bearing and needs an owner. Keep the pipelines boring (scheduled batch, re-runnable) so they fail quietly and recover easily.

---

## 7. Suggested sequence (once the store is chosen)

1. Model **one event** (Long Beach) as structured records from the existing template.
2. Build the **Zendesk publish job** (confirmed-only filter). This alone retires the KB-drift problem.
3. Add the **guide builder** (the cleanest, highest-visibility output).
4. Add the **website feed** (after confirming the site can consume it).
5. Add the **internal staff bot** (only if Firestore).
6. **Close the loop:** Part-1 dashboard's top-5 unanswered writes `open` records into the store as the content backlog.

---

## 8. The two things to confirm before building Part 2

1. **Internal staff bot — near-term need or later?** → decides Sheet vs Firestore.
2. **How is supertri.com built?** → decides whether the website leg is a clean feed or needs rework.

*(Part 1 — the dashboard — needs neither of these answered and can start immediately.)*
