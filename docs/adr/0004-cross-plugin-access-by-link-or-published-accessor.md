# 4. Cross-plugin access by link or published accessor, never a shared source

Date: 2026-07-27

## Status

Accepted

Amends [ADR 3](0003-plugin-views-as-flask-blueprints-with-tabs.md).

## Context

The timing tab renders a `mem0_search` span's top hits from the span's
end payload. The full ranked list, the context messages mem0 retrieved
against, and the raw payload are all already on the memory tab's event page,
so the span should link there rather than reproduce any of it.

[ADR 3](0003-plugin-views-as-flask-blueprints-with-tabs.md) anticipated a
combined memory-plus-timing display arriving as a third plugin reading both
sources, and recorded as a consequence that "plugins share nothing but
`base.html` and the app config". A link from one tab to another is the first
thing to test that claim. It is also far short of a combined view: one tab
points at a page the other already serves.

The two sources record the same `mem0_search` call but share no key. ATOF
carries a span uuid and no event id; `jmem0_logged.db` carries a row id and
no span uuid. What both hold is the session id and the query text, and
across the whole log to date those matched exactly one event for all 104
mem0_search spans — no ambiguity, no misses. The db timestamps the call's
completion about a second after the span starts, so time is a tiebreaker but
not an identifier.

A second case pushed harder on the same boundary. A `mem0_update` or
`mem0_delete` span names a memory only by id, so neither says what the
memory contained — the one thing a reader wants, and for a delete the only
record that it ever existed. mem0 cannot supply it: hermes' `Mem0Backend`
exposes only search/add/update/delete, and the platform cannot return a
deleted memory at all. Asking it would also mean the observer making
authenticated network calls, which ADR 2 rules out.

The local log has the text anyway. A `mem0_search` result carries each hit's
full text beside its id, and the agent can only learn an id *from* a search,
so every change is preceded by the search that surfaced it — all 19 in the
log, always in the same session, a median of 30 s earlier. But that text
lives in the memory plugin's store while the span lives in the timing tab's,
and it has to be shown in place, on the span, where a link cannot help.

## Decision

Plugins may link to each other's pages by URL, and may call an accessor
another plugin publishes. They may not open each other's data sources,
import each other's readers, or reach into another plugin's app config.

The boundary is *ownership of the source*, not the absence of a call. The
plugin that owns a store owns every query against it; other plugins ask, and
never hold a connection, a path, or a schema assumption.

Two shapes, by what the caller needs:

- **A link**, when the caller only needs to send the reader somewhere. The
  target owns any lookup the link needs and exposes it as a redirect route
  — `memory.search_event`, taking the session, the query and an optional
  `ts` (the span's start, epoch microseconds, breaking the one tie the pair
  admits: the same query run twice in a session) and redirecting to
  `/memory/event/<id>`. The linking plugin builds a `url_for` and knows
  nothing else.
- **A published accessor**, when the caller needs the data itself on its own
  page — `memory.prior_memory_text`, which the memory plugin puts in
  `app.extensions` at registration and the timing tab calls to show what a
  memory said before a `mem0_update`/`mem0_delete` span changed it. Published
  through the app shell rather than imported, so the plugins do not depend
  on each other directly and the caller degrades to doing without when the
  accessor is absent.

Prefer the link. Reach for an accessor only when the data has to appear
in place, and keep it a narrow function over the target's own store, never
a handle to it.

An unresolvable link 404s, naming the reason. The two logs are written by
different processes with no shared lifecycle, so either can cover a call the
other does not; that is a real state and is reported, not hidden (ADR 2's
loud-failure rule).

## Consequences

- ADR 3's "plugins share nothing but `base.html` and the app config" no
  longer holds exactly: they may now also share endpoint names and
  accessors published on the app. Both are the weakest couplings that do
  their jobs — a URL is already public surface and Flask fails loudly on an
  unknown endpoint, and an `app.extensions` entry is absent rather than
  broken when a plugin is not registered.
- Link lookups stay off the render path. A turn page can hold many
  mem0_search spans and polls every 2 s while the turn is live; had the
  timing view resolved event ids itself, every poll would have cost a query
  per span. The redirect costs one query, and only when a link is clicked.
- Accessor lookups *are* on the render path, so they must stay cheap and
  rare. `prior_memory_text` is one indexed row per `mem0_update`/
  `mem0_delete` span (0.014 ms measured, and most turns have no such span),
  which needs no caching. Anything heavier should be a link instead.
- Data reconstructed across sources must say so where it is shown. The
  previous text of a memory comes from the local event log, not from mem0,
  and can in principle be stale; the UI names its source and its age rather
  than presenting it as history mem0 vouched for.
- Correlation heuristics are confined to the plugin that owns the data. If
  the pairing ever needs strengthening — most likely by hermes stamping a
  shared id on both records — it changes in one route.
- A combined view remains additive and remains the answer for anything
  richer than a link, exactly as ADR 3 set out. This decision deliberately
  does not open the door to plugins querying each other's sources.
