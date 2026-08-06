# 11. Index the ATOF log rather than hold it in memory

Date: 2026-08-06

## Status

Accepted, implemented 2026-08-06

Takes the escape hatch [ADR 2](0002-read-atof-jsonl-directly-no-etl.md)
reserved for itself. Does not supersede it: the JSONL stays the source of
truth and every payload is still read from it.

## Context

The tailer parses each appended line and keeps the resulting `AtofEvent` —
payload and all — for the app's lifetime, and every request runs `assemble()`
over the whole accumulated list. That was sized for ADR 2's "tens of span
events per turn and at most hundreds of turns a day". The log has outgrown it.

Measured 2026-08-06 against the live log, **1.19 GB / 281,490 lines**, growing
at roughly 340 MB/day:

| name \| category | lines | % lines | MB | % bytes | avg line |
|---|---:|---:|---:|---:|---:|
| `llm.chunk` | 264,267 | 93.9 | 111 | 9.3 | 420 B |
| `openai-codex \| llm` | 4,630 | 1.6 | 595 | 49.9 | 129 KB |
| `hermes.turn.end` | 331 | 0.1 | 221 | 18.5 | 667 KB |
| `hermes.turn.start` | 358 | 0.1 | 204 | 17.1 | 570 KB |
| everything else | ~11,900 | 4.2 | 60 | 5.0 | — |

**5,319 lines carry 85% of the bytes.** An llm start repeats the whole
conversation in `annotated_request.messages` and a turn mark repeats it again
in the hook kwargs, so the weight grows quadratically with turn length. Held
as Python objects this is several GB; a verification script was OOM-killed on
the file three days earlier, at less than half the size.

Three further measurements decide the shape of the fix.

- **Parsing is not the expensive part.** A full scan that runs the real
  `parse_line` over every line and keeps only a spine row per event, dropping
  each payload as it goes, completes in **6.3 s** at a peak RSS of **215 MB**.
  Reading the bytes is 0.2 s of that; `json.loads` is 5.1 s.
- **The spine is small.** 40 MB serialized for all 281,490 rows; **2.5 MB** for
  the 17,223 that are not `llm.chunk`.
- **`llm.chunk` is nearly all of the assembly cost and none of the model.**
  `assemble()` over payload-free events takes 3.32 s; with chunks excluded,
  **0.14 s** — for byte-identical output, 84 sessions, 361 turns, 262
  anomalies. They reach no template: `Turn.marks` is read only for subagent
  tracking and for `last_activity_us`.

The last one is a different axis from the finding on 2026-08-03 that filtering
chunks buys no runway. That was about bytes, and it still holds — chunks are
9% of the file, so dropping them does not solve the memory problem. What they
dominate is per-request CPU: 94% of the rows and 96% of the assembly time.

A store — parse the log into a database and read the database instead — was
considered and deferred on 2026-08-03, because hermes' emitted schema is still
moving (the NeMo Relay series is still landing fixes; the shape changed twice
inside one session) and a store bakes its guesses into migrations. That
reasoning is unchanged. What has changed is the recognition that the problem
does not need a store: what will not fit in memory is the payloads, and
payloads are exactly what a page needs least of. A turn list needs none. A
turn page needs the fifty or so belonging to one turn.

## Decision

**Persist a projection of the log — offsets and the fields assembly needs —
and read payloads out of the log by offset when a page actually renders one.**

### The index is a projection, not a copy

One row per event, holding:

- **the back-reference** — byte offset, byte length, line number;
- **the spine** — kind, scope category, uuid, parent_uuid, timestamp,
  name, category, and the schema era that [ADR 6](0006-parse-atof-by-declared-schema-era.md)
  reads per event;
- **the correlation fields** — `session_id`, `turn_id`, `status`,
  `call_role`, `model_name`, `tool_call_id`. These are *derived*, not copied:
  under the core runtime a session id is recovered from the composite turn id
  or from the llm request's `extra_headers`, and it is `atof_reader` that
  recovers it. The indexer runs the real parser and stores what it returns;
- **two small strings that assembly cannot proceed without** — the
  `user_message` from a `hermes.turn.start` (a short field inside a 570 KB
  payload), and `Span.request_prompt`, the reconstruction used only when no
  mark supplied the real prompt.

Nothing else. No `data`, no `category_profile`, no `annotated_request`.

`llm.chunk` events are aggregated to one timestamp per streaming llm span,
not carried as rows. That is the only thing they can contribute.

### Payloads are read from the log, on demand

A `Span` holds `(offset, length)` where it holds `start_data`, `end_data` and
`category_profile` today, and reads them on first access. A turn page pays for
its own spans and no others. The log remains the only place a payload exists,
which is what keeps ADR 2's "the view can never be stale relative to the log"
true of everything a reader is actually shown.

**A payload that cannot be re-read says so on the page.** The log can rotate
between assembly and render. Per ADR 2 that is a loud state — the row reports
that its payload is no longer in the log — never a blank field.

### The index is a cache, and is thrown away on any doubt

It carries no data the log does not, and a full rebuild is 6.3 seconds. Every
validity check is therefore biased towards rebuilding, and there is no
migration path: a stale index is deleted, not upgraded.

Four ways it can go stale, four checks, all run on refresh:

1. **Rotation, replacement, truncation** — `st_dev`/`st_ino` differ from the
   build, or `st_size` is below the indexed extent. Rebuild.
2. **In-place rewrite that kept the inode** — sha256 of the first 64 KB,
   stored at build time. In an append-only file the head never changes;
   `> file` followed by a rewrite keeps the inode but not this.
3. **Anything that moved the indexed region** — the offset, length and hash of
   the **last indexed line** are stored, and re-read on refresh. A mismatch
   means the bytes under our offsets moved. One seek and one small read, and
   it is the check that survives a rewrite preserving both size and head.
   Alongside it, the byte before the indexed extent must be `\n`.
4. **Our own parser changed** — the index is stamped with an explicit
   `INDEX_VERSION` **and** a hash of `atof_reader`'s source. The derived fields
   mean whatever the parser meant when it wrote them, and that parser will keep
   changing while hermes' schema does. Hashing the source invalidates them
   automatically instead of relying on someone remembering to bump a constant.
   It over-invalidates — a docstring edit forces a rebuild — which at six
   seconds is the right side to be wrong on.

The index lives in the observer's own cache directory, keyed by a hash of the
log path, and never beside the log: that directory is hermes'.

## Consequences

- **The Prompts tab works on a log that no longer fits in memory**, which it
  does not today. Steady-state assembly falls from 3.32 s to 0.14 s per
  request, which matters at the 2 s poll of a live turn page.
- **This app now stores derived data.** Principle 2's "stores nothing derived"
  is bent a second time, and the principles file must name this ADR beside
  ADR 6 as the place it is bent. The bend is bounded: nothing in the index is
  presented on screen as something hermes vouched for that was not read back
  out of the log, and deleting the index changes nothing but the six seconds.
- **A second thing can now be wrong.** ADR 2 rejected ETL partly to avoid "the
  DB disagrees with the log" bugs, and that class of bug is genuinely
  reintroduced — the safeguards above are the whole of the answer to it. The
  mitigation ADR 2 could not have is that the disagreement is never *served*:
  payloads come from the log at render time, so a bad index can misplace a span
  but cannot show a reader a payload the log does not contain.
- **ADR 6's per-event schema dial survives intact.** The indexer runs
  `parse_line`; the era is a stored field, not a per-file assumption, and a
  file holding both dialects indexes correctly.
- **Rebuild cost grows with the log.** 6.3 s at 1.19 GB is roughly 55 s at
  10 GB, which the current growth rate reaches inside a month. The index
  surviving restarts is therefore not a nicety — it is what makes the rebuild
  rare — and check 4's over-invalidation gets more expensive as the file grows.
  Rotation, deferred since ADR 2, becomes worth having again.
- **First run of a new install pays the full build.** Six seconds of blocked
  first request today; the tab should say what it is doing rather than appear
  hung.
- **`llm.chunk` stops being an event.** Anything later wanting per-token
  timing — a streaming-latency view — will have to re-read them from the log
  by offset rather than find them in the model. Recorded here so that is a
  known cost and not a surprise.
- **A store is still the eventual answer if the shape stops moving**, and this
  does not foreclose it. It removes the pressure to build one while hermes'
  schema is still changing, which was the reason for deferring.

## As implemented

Measured on the same 1.19 GB log the day this was written.

| | |
|---|---|
| full build | **9.6 s**, peak RSS **131 MB** |
| index on disk | **29 MB**, 17,223 rows |
| load those rows | 0.32 s |
| `assemble()` over them | **0.12 s** |
| refresh with nothing appended | **5.7 ms** |
| hydrate one 7-span turn | **3 ms** |
| turn list, cold process / warm | 0.53 s / 0.02 s |
| turn page, first / repeat | 0.05 s / 0.008 s |

The model is byte-identical to the one the in-memory path produced: 84
sessions, 361 turns, 262 anomalies, 0 parse errors.

Three things this decision did not anticipate:

- **Chunks reach no turn today, so folding them in is a small improvement
  rather than a preservation.** They carry no `session_id` and no `turn_id`,
  only a `parent_uuid` naming their llm span, so `assemble`'s attach-by-time
  path files every one of them under `(unknown session)`. Aggregating by
  parent span is the first time they inform a real turn's liveness — which
  matters for a long streaming call that otherwise looks silent since it
  started.
- **The module is `atof_index.py`, not `index.py`.** `plugins.prompts` already
  has an `index` — the turn-list view function — and it shadows a submodule of
  that name as a package attribute, which breaks `from plugins.prompts import
  index` and anything resolving that dotted path.
- **The threshold for keeping a payload is its size, never its key name.** A
  list of hermes' large keys would be the kind of in-tree list the fork test
  exists to catch, and the answer differs per tool. Below 4 KB is kept, which
  is what leaves the turn *list* able to read `child_session_id` off a
  subagent mark without touching the log.

## Related

- [ADR 2](0002-read-atof-jsonl-directly-no-etl.md) — the JSONL as source of
  truth, and the escape hatch this takes
- [ADR 6](0006-parse-atof-by-declared-schema-era.md) — the per-event era dial
  the indexer runs
- [docs/design/atof-reader.md](../atof-reader.md) — tailer → parser →
  assembler, and what each layer derives
