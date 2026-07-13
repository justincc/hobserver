# 2. Read the ATOF JSONL directly rather than ingesting it into a database

Date: 2026-07-13

## Status

Accepted

Builds on [ADR 1](0001-consume-nemo-relay-atof-for-prompt-timing.md), which
chose the ATOF stream as the source of prompt-timing data.

## Context

With ATOF as the data source, the timing view needs a consumption strategy.
Three options were considered:

1. **Ingest ATOF events into `jmem0_logged.db`.** Rejected outright: that
   database is the mem0 logging wrapper's output, which this browser
   deliberately opens read-only so it is safe against the live writer.
   Ingesting would mix two unrelated schemas in one file, make the viewer a
   second writer alongside another process (SQLite locking pain for no
   benefit), and force a rename of the database.
2. **ETL into a separate viewer-owned SQLite database** (e.g.
   `hermes_timing.db`) via a tailer that converts JSONL lines to rows.
   Workable, but derived stores are where drift lives: schema migrations,
   dedup on re-ingest, and "the DB disagrees with the log" bugs — all to
   solve a scale problem we do not have.
3. **Read the JSONL directly as a read-only source**, the same way the
   browser reads `jmem0_logged.db` today.

Scale does not justify ETL: personal use produces tens of span events per
turn and at most hundreds of turns a day; parsing even ~100k JSONL lines at
startup takes on the order of a second. The existing tail pattern
(`/fragment/events?since=<id>`) translates directly, with a byte offset as
the cursor instead of a rowid. Reading the raw stream also serves the ADR 1
learning goal: the viewer becomes a faithful ATOF consumer rather than a
projection of our own flattened schema.

## Decision

The timing view reads the ATOF JSONL file(s) directly. No timing data is
ever written to `jmem0_logged.db` (which therefore keeps its name), and no
derived database is introduced at this stage.

Under the planned plugin restructure, each plugin owns a read-only view over
a data source produced externally: the mem0 plugin over `jmem0_logged.db`,
the timing plugin over the ATOF output directory. The browser never owns or
mutates data; it renders logs that other processes produce.

The reader is a small `atof_reader` module in the timing plugin, in three
layers:

1. **Tailer** — incremental line reader over the configured ATOF
   file/directory, persisting a byte-offset cursor. If the file is shorter
   than the saved offset (the exporter supports `mode=overwrite` as well as
   `append`), reset to zero and re-read.
2. **Parser** — raw JSONL line → typed event (span-open, span-close, mark),
   preserving the `metadata` correlation keys (`session_id`, `turn_id`,
   `api_request_id`, `tool_call_id`).
3. **Assembler** — pairs span open/close events, groups by `session_id` +
   `turn_id`, and computes the per-turn waterfall: turn total from the
   `hermes.turn.start` / `hermes.turn.end` marks, provider time from LLM
   span durations, tool time from tool spans (`duration_ms`), and overhead
   as the residual gap.

Flask views render the assembler's in-memory turn/span objects using the
same page patterns as the mem0 view: a newest-first index of turns with
tailing, and a detail page per turn showing the waterfall.

Operationally, the exporter runs with `mode=append` (one growing
`events.jsonl`; rotation deferred until it matters).

## Consequences

- No ETL code, no second store, no migrations; the view can never be stale
  or wrong relative to the log, because the log is what it reads.
- Startup reparses the file and rebuilds in-memory state. Acceptable at
  personal scale; if it ever gets slow, the agreed escape hatch is a SQLite
  file that is explicitly a rebuildable cache of the JSONL (delete it and it
  regenerates), never a source of truth.
- Aggregations happen in Python over in-memory objects rather than in SQL.
- The append-mode file grows without bound until rotation is introduced;
  the tailer's cursor design should not assume a single immortal file.
- Per ADR 1's fail-open caveat, the viewer must surface a loud
  "no events since X" state rather than an empty page when the stream goes
  quiet.
