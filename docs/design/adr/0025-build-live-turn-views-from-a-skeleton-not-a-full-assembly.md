# 25. Build the live turn views from a skeleton, not a full assembly

Date: 2026-09-10

## Status

**Rejected** — the premise does not hold for hermes' current data (see
Rejection). Kept as a record so the idea is not rediscovered from scratch.

Would have extended
[ADR 11](0011-index-the-atof-log-rather-than-hold-it-in-memory.md).

## Context

The index (ADR 11) turns the log into rows, but every page then builds the
whole session/turn/span model from all of them (`assemble()`), and a live page
reruns that each poll. Two cheap fixes already landed — caching the
deserialised event list, and grouping turn-boundary marks by session in one
pass (that filter was O(sessions x events) and dominated) — taking a
60k-event/263-session log from ~4-10 s/poll to ~0.5 s. But ~0.5 s is still a
*full* rebuild, so the cost grows linearly with the log.

The turn detail page shows one turn (~12 events, ~0.2 ms to assemble) and the
in-flight strip shows the few live turns. The tempting optimisation: build a
cheap **turn skeleton** from just the turn-boundary and subagent-stop marks
(~2.5% of events) to get every turn's `(session_id, start_us, end_us,
superseded)`, then `assemble()` only the window of the turn a view actually
shows. Live views would become proportional to one turn and flat as the log
grows.

## Decision

**Rejected.** Do not scope views this way; keep the full `assemble()` per poll
(now ~0.5 s) until a genuinely incremental approach is worth building.

## Rejection — why the skeleton cannot be cheap

A turn's `session_id` is not on its boundary — it is only on its **leaf
spans**. Measured on the live log:

- **1001 of 1001** `hermes.turn` scopes carry no `session_id` on themselves,
  and none is recoverable from their parent agent scope (`scope_sessions` is
  empty — the agent scopes carry no session either).
- **457 turns are scope-only** (a `hermes.turn` scope, no boundary mark), so
  they are not in a marks-only skeleton at all. `_build_scope_turns` derives
  each one's session, `turn_id` and prompt from its child spans.

So you cannot determine which session a turn belongs to — and therefore cannot
place it in a session's turn list, compute superseding, bound its window, or
match a URL — without processing its spans, which is most of what `assemble()`
already does. The skeleton is not cheaper than the thing it was meant to avoid.

## What a real fix would take instead

Flatness requires assembling only what changed, and both viable routes process
spans (just incrementally), so both are substantial and out of scope here:

- **Incremental assembly** — cache the assembled model and re-assemble only the
  appended tail onto an immutable closed prefix (a closed turn can receive no
  more events). Splice-parity across the session boundary is the risk.
- **Materialised turn rows in the index** — compute turn summaries at build
  time and update only the tail on extend, so pages read turns instead of
  assembling them.

Until one of those is worth it, ~0.5 s stands. See the hobserver session note
on the ATOF live-view lag for the full investigation.
