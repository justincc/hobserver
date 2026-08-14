# 16. Recover a matched store entry from a listing in the same turn

Date: 2026-08-14

## Status

Accepted, implemented 2026-08-14

Bounded by [ADR 2](0002-read-atof-jsonl-directly-no-etl.md) — this
reconstructs something the log does not state, which that ADR is otherwise
against — and by
[ADR 11](0011-index-the-atof-log-rather-than-hold-it-in-memory.md), which is
why it runs for one turn rather than for the log.

## Context

The `memory` tool addresses an entry in MEMORY.md / USER.md by a *fragment*
of it. `$HERMES_SOURCE/tools/memory_tool.py` matches by containment —
`[e for e in entries if old_text in e]` — and rejects the call if more than
one distinct entry matches, which is what makes a short fragment safe for the
model to send. The payload therefore holds the fragment and nothing else: the
tool never logs what it resolved to, and the success payload reports a char
count and an entry count but not the entries.

So a replace rendered as the log holds it reads:

    − For household fault-finding
    + Household fault-finding: rank common causes; use diagrams and steps.

which states that a short phrase was replaced by a sentence. What was really
replaced was a 131-character entry. A reader cannot tell the difference, and
that was the question that prompted this: *is the fuller text in the log and
we are not showing it, or did hermes only ever log this much?*

The turn usually holds the answer. A write rejected for the char budget comes
back with `current_entries` — the entire store — and consolidating after such
a rejection is the routine reason to replace an entry at all. In the case
above the listing and the write that used it were 4 seconds apart in one
turn.

## Decision

**Match the fragment against a store listing from the same turn, show the
entry it resolves to on the − side, and say on the row where that came from.**

Four decisions inside that.

**Only a listing counts as evidence.** `current_entries` appears on failure
paths alone, and a rejected write — batch included, it is all-or-nothing —
changed nothing, so the listing describes the store as that span found it. It
answers for the span that returned it as well as for later ones.

**A listing is dropped as soon as a write lands.** A successful write says
what it cost and never what the store now says, so after one this app does
not know the store. Later ops go unresolved until another rejection lists it
again. The alternative — replaying our own idea of each write onto the
listing — would be a second copy of someone else's store, kept by us, wrong
in a way nothing on the page could show. That is ADR 2's rule, and the
"discard on any doubt" clause ADR 11 inherited.

**Within one span the ops *are* replayed**, because there the log is
complete: a batch is applied atomically to one working list and every
operation in it is on the span. A later op in a batch therefore matches what
the earlier ones left, exactly as the tool does it. This is a reading of the
payload, not a guess about the world — the distinction the paragraph above
turns on.

**Nothing is presented as logged.** A `.prov` row under the diff names the
listing and its age ("matched entry from the store listing 4 s earlier in
this turn") and carries the logged fragment with it, so the payload's own
value stays on the page beside the entry this app worked out. Where the
listing cannot answer — no match, or more than one — the note says that
instead, and the − side falls back to the fragment. Silence is never the
answer to an unresolved match: a row that simply showed the fragment would
look like one we had not tried to resolve. This is design principle 2, and
deliberately the same shape as mem0's recovered previous text.

It runs as `assembler.resolve_memory_entries(turn)`, a turn-level pass called
from the turn view after hydration — it reads one span's end payload to make
sense of another's, which is not something a `Span` property can do, and it
needs payloads that are in the log rather than in the index.

## Consequences

- **The − side of a memory replace is worth reading.** It shows the entry,
  the store's units, rather than a phrase from inside it.
- **Resolution is per turn and per store.** A fragment whose listing was in
  an earlier turn stays unresolved. Hydrating other turns to go looking is
  the cost ADR 11 exists to avoid, and cross-turn distance is exactly where
  a listing is least likely to still hold.
- **The page can now say "not resolved".** Ambiguous and unmatched fragments
  are stated. An unmatched one is itself information: the listing has moved
  on, or the write is being read against the wrong store.
- **One more thing to keep in step with hermes.** If the tool ever logs the
  resolved entry, or reports the store after a successful write, this pass
  should give way to the payload rather than compete with it.
