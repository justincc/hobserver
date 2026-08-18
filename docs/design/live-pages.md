# Live pages: polling, liveness, follow mode, tailing, navigation

## Polling and swapping

Pages self-update via the poll-and-swap script in `templates/base.html`: any
element with `data-live-poll="<ms>"` is refetched from the current URL on that
interval and swapped in place. "0" means static; the turns index uses 3 s, an
in-flight turn 2 s, and a finished turn page polls slowly while follow mode is
on. Polling pauses while the browser tab is hidden.

No SSE or WebSockets; a poll reuses the per-request tailer, which reads only
what the exporter appended since the last request.

### Selecting text pauses everything

A swap replaces the nodes a selection lives in, which drops it — so dragging
across a prompt to copy it lost the selection on the next tick, and copying
anything off a live page was impossible. Nothing can rescue a selection whose
text has just been removed, so the loop holds still instead: while a selection
sits **inside the live region**, there is no fetch, no swap and no follow-mode
navigation, and the next tick after it collapses resumes all three. A click
anywhere collapses it, which is what a reader does after copying.

Follow mode is paused with the rest deliberately: navigating away mid-copy
loses the selection just as thoroughly as swapping does. A selection outside
the region (the page header) survives a swap untouched and does not pause
anything.

## The in-flight strip

Both Turns pages show an in-flight strip
(`plugins/turns/templates/turns/_inflight.html`) listing every running turn, newest first,
with a short prompt snippet, elapsed time and span count, each linking to its
waterfall. The turn being viewed is marked.

A turn silent for more than `STALE_AFTER_US` (2 hours) is treated as a lost end
mark, not a running prompt, and dropped from the strip. The cutoff is generous
because an agentic turn can legitimately go quiet for many minutes — a slow
model call or a long tool run emits no ATOF events meanwhile — so a shorter one
would retire turns that are still working.

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

A "follow new turns" switch (persisted in localStorage) that auto-opens a
turn's waterfall when a new turn starts, with two guards: never while you are
watching a turn that is still in flight (concurrent turns just appear in the
strip for manual switching), and never to a stale entry.

**On by default.** Only an explicit "0" in localStorage turns it off, so a
browser that has never touched the switch follows. The checkbox therefore
carries `checked` in the markup as well — the switch has to read "on" before
the script runs, or a following page shows an off switch until it resyncs.

It is the same slider as **show all span details** on the turn page: both are
a persistent on/off for the page, so they are one control drawn once by
`.switch` in `base.html`, and each of `.follow-toggle` / `.detail-toggle`
says only where it sits. The `.track` span must follow its `<input>`
immediately — it is a sibling selector painting the checkbox's state, and
anything between them leaves a switch that never moves.
The strip's data attributes drive that JS: `data-inflight-start-us`,
`data-stale`, `data-inflight-current`, `data-turn-start-us`.

On a turn page the toggle rides the event-nav row, which is *inside* the live
region. **base.html must never hold a reference to it across a swap**: it
re-resolves the element on each use, resyncs its checked state after every
swap, and uses a delegated change listener. A captured reference detaches on
the first poll and the switch dies silently.

On the index the toggle stands alone above the live region.

## Tailing — following the newest spans within a turn

A region marked `data-live-tail` scrolls to its new bottom after each swap,
**but only if the reader was at the bottom when the swap began**. The turn
page opts in; the turn list deliberately does not, because it is newest-first
and its new rows arrive at the top.

Distinct from follow mode, and easy to confuse with it: **follow mode moves
you between turns, tailing moves you within one.**

There is no toggle. Being at the bottom *is* the opt-in — scroll up and it
stops, scroll back down and it resumes — so there is no switch to find, none
to leave on by mistake, and no state to persist or resync across a swap.

Two details that are load-bearing:

- **Measured before the swap, applied after it.** Whether the reader was at
  the bottom is a fact about the content they could see; where the bottom now
  *is* is a fact about the content that replaced it.
- **48px of slack, and it applies after the reopened detail rows.** "At the
  bottom" is never exact — subpixel rounding, zoom and rubber-banding all land
  a pixel or two off. The slack also makes the test true for a page shorter
  than the viewport, which is what starts a short turn tailing the moment it
  grows past one screen instead of only once it is already scrollable. The
  scroll runs after the reopened rows are restored because those change the
  height it is scrolling to.

The scroll is instant, not smooth: a 2 s poll would begin the next swap while
an animation was still running, and tailing wants to be where the newest row
is rather than on its way there.

## Item navigation

The nav row is the `item_nav` macro in `templates/_item_nav.html`, shared by
every plugin's detail page (turns turn, mem0 event) so item-by-item
navigation is identical everywhere: "← all X" first, then muted « prev /
next » steppers grouped beside it, prev always meaning older.

New plugins import it rather than rolling their own row. A page that wants
something else on the row (the follow toggle) passes it via `{% call %}`,
which space-between drops on the right.

## Waterfall colors

Validated with the dataviz six-checks palette validator against the light
surface: llm `#2a78d6`, tool `#eb6834`, other `#4a3aa7`. Span identity is
always in text as well — never color alone.
