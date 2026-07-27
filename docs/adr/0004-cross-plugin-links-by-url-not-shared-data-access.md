# 4. Cross-plugin links by URL, not shared data access

Date: 2026-07-27

## Status

Accepted

Amends [ADR 3](0003-plugin-views-as-flask-blueprints-with-tabs.md).

## Context

The timing tab renders a `mem0_search` span's top two hits from the span's
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

## Decision

Plugins may link to each other's pages by URL. They may not read each
other's data sources, import each other's readers, or reach into another
plugin's app config.

Where a link needs a lookup to resolve, the *target* plugin owns it and
exposes it as a redirect route — here `memory.search_event`, which takes the
session, the query and an optional `ts` (the span's start, epoch
microseconds, breaking the one tie the pair admits: the same query run twice
in a session) and redirects to `/memory/event/<id>`. The linking plugin
builds a `url_for` and knows nothing else.

An unresolvable link 404s, naming the reason. The two logs are written by
different processes with no shared lifecycle, so either can cover a call the
other does not; that is a real state and is reported, not hidden (ADR 2's
loud-failure rule).

## Consequences

- ADR 3's "plugins share nothing but `base.html` and the app config" no
  longer holds exactly: they may now also share endpoint names. This is the
  weakest coupling that does the job — a URL is already public surface, and
  Flask fails loudly at render time on an unknown endpoint.
- Lookups stay off the render path. A turn page can hold many mem0_search
  spans and polls every 2 s while the turn is live; had the timing view
  resolved event ids itself, every poll would have cost a query per span.
  The redirect costs one query, and only when a link is actually clicked.
- Correlation heuristics are confined to the plugin that owns the data. If
  the pairing ever needs strengthening — most likely by hermes stamping a
  shared id on both records — it changes in one route.
- A combined view remains additive and remains the answer for anything
  richer than a link, exactly as ADR 3 set out. This decision deliberately
  does not open the door to plugins querying each other's sources.
