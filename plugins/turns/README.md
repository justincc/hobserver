# Turns tab

The Turns tab renders per-turn latency waterfalls from the NeMo Relay ATOF
JSONL stream. Its pages:

- `/turns/` — all turns, newest first, one line each: start time, the prompt
  (from the turn-start mark's `user_message`; em dash when absent — it fills the
  leftover width and ellipsizes, so nothing else is abbreviated), the session
  (the leading segment of the turn id, which hermes builds as
  `{session}:{task}:{hash}` — the full id is on the turn page), total / model /
  tool durations (as `m:ss` for a quick scan — the turn page keeps two-decimal
  seconds), and model-call and span counts. In-flight turns are marked. Parse errors and assembly anomalies are
  shown above the table, folded closed and on this page only — never dropped.
  Updates itself every 3 s.
- `/turns/turn/<session>/<start_us>` — one turn, described below.
- `/turns/span/<span uuid>/<key>` — one whole value of one span, on a page
  of its own: a model call's `request` (every message it was sent, system
  prompt included, one labelled box per message) or its `response`, rendered
  as markdown, with `?raw=1` for the characters underneath. Every label and
  heading the app writes is boxed chrome; anything inside a box went on the
  wire. Reached by the open-in-a-new-tab icon at the end
  of an excerpt, never navigated to by hand — so it carries no tab bar, just
  a nav row back to the turn the span ran in and to the raw text. Which values
  a scope offers is declared with it — see
  [ADR 12](../../docs/design/adr/0012-open-a-whole-value-on-its-own-page.md).

The turn page runs top to bottom:

1. The nav row — "all turns", then muted « prev / next » stepping one turn
   older / newer through the same interleaved-by-start-time ordering the turn
   list uses, so they cross session boundaries just as it does. Each is titled
   with the neighbour's prompt, and greys out at either end.
2. The in-flight strip.
3. The turn id / session / started heading; a muted icon beside the turn id
   copies it to the clipboard.
4. The prompt — whole when short, collapsed to its first couple of lines with
   the full text a click away when long.
5. Summary stats.
6. The span waterfall: model blue, tool orange, other violet, open spans faded,
   with offsets and durations as text.

The turn's non-boundary marks (approvals, session end) are instantaneous, so
they interleave with the spans as rows whose bar is a zero-width tick at their
offset, clamped to the track — session end fires just after the turn-end mark.

Each span row also shows what the call was *for*, drawn from its payload and
rendered per tool scope: the command a terminal call ran, the path a file tool
touched, a mem0_search's top hits, and so on. That is a subject of its own —
see [span-rendering.md](../../docs/design/span-rendering.md).

Where a row can only show an excerpt — what a model was asked, what it said —
a small icon at the end of it opens the whole thing in a new tab. That works
for every model call, including hermes' own background ones: a context
compaction's instruction and summary are readable there and nowhere else on
the site.

While the turn is in flight the page updates itself every 2 s; once it ends
the page is static.
