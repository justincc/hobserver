# Live pages: polling, liveness, follow mode, navigation

## Polling and swapping

Pages self-update via the poll-and-swap script in `templates/base.html`: any
element with `data-live-poll="<ms>"` is refetched from the current URL on that
interval and swapped in place. "0" means static; the timing index uses 3 s, an
in-flight turn 2 s, and a finished turn page polls slowly while follow mode is
on. Polling pauses while the browser tab is hidden.

No SSE or WebSockets; a poll reuses the per-request tailer, which reads only
what the exporter appended since the last request.

## The in-flight strip

Both timing pages show an in-flight strip
(`templates/timing/_inflight.html`) listing every running turn, newest first,
with a short prompt snippet, elapsed time and span count, each linking to its
waterfall. The turn being viewed is marked.

A turn silent for more than 10 minutes is flagged stale — probably a lost end
mark, not a running prompt — and stale turns are never auto-followed.

## Liveness — an open turn is not a running turn

Turn.end marks go missing often, so "still open" is not evidence of work in
progress. `Turn.is_live` means open **and not proven finished**, and only live
turns reach the strip, mark `data-inflight-current`, or poll at 2 s.

Two proofs override open — both exact and immediate, where a staleness clock
is neither:

- `Turn.superseded` — a later turn started in the same session. Set in
  `_build_turns`, where that anomaly is already detected.
- `Assembly.finished_subagent_sessions` — a `hermes.subagent.stop` names the
  session.

**Never invent an `end_us` for these.** The duration was never observed
(ADR 2); they render "no end mark".

This applies to liveness only — the turn table and turn pages keep everything.

A permanently-live turn also freezes follow mode, which will not leave a live
turn. That was the bug supersession fixed, and no time threshold could have:
the turn had been silent for 2 minutes.

## Follow mode

A "follow new turns" toggle (persisted in localStorage) that auto-opens a
turn's waterfall when a new turn starts, with two guards: never while you are
watching a turn that is still in flight (concurrent turns just appear in the
strip for manual switching), and never to a stale entry.
The strip's data attributes drive that JS: `data-inflight-start-us`,
`data-stale`, `data-inflight-current`, `data-turn-start-us`.

On a turn page the toggle rides the event-nav row, which is *inside* the live
region. **base.html must never hold a reference to it across a swap**: it
re-resolves the element on each use, resyncs its checked state after every
swap, and uses a delegated change listener. A captured reference detaches on
the first poll and the switch dies silently.

On the index the toggle stands alone above the live region.

## Item navigation

The nav row is the `item_nav` macro in `templates/_item_nav.html`, shared by
every plugin's detail page (timing turn, memory event) so item-by-item
navigation is identical everywhere: "← all X" first, then muted « prev /
next » steppers grouped beside it, prev always meaning older.

New plugins import it rather than rolling their own row. A page that wants
something else on the row (the follow toggle) passes it via `{% call %}`,
which space-between drops on the right.

## Waterfall colors

Validated with the dataviz six-checks palette validator against the light
surface: llm `#2a78d6`, tool `#eb6834`, other `#4a3aa7`. Span identity is
always in text as well — never color alone.
