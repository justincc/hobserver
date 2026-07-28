# The ATOF reader

Three layers in `plugins/timing/`, per ADR 2 (direct JSONL reading, no ETL).
Views run the tailer on each request and assemble in memory.

## tailer.py — incremental read

Byte-offset incremental read of the JSONL file, with an app-lifetime instance
in `app.extensions` so a request only pays for what the exporter appended
since the last one.

**Split the chunk on `"\n"` only — never `str.splitlines()`.** splitlines also
breaks on U+0085, U+2028 and U+2029, which JSON does not require escaping and
which hermes' assistant text contains verbatim. That shredded whole records
into unparseable fragments; when the shredded record was a `hermes.turn.end`,
its turn stayed open forever, so a long-finished turn kept polling at 2 s.

## atof_reader.py — parser

JSONL line → typed event. Its fixtures include the verbatim example lines from
the ATOF v0.1 spec.

## assembler.py — events → turns

- Spans are paired by uuid.
- Turns are bounded by `hermes.turn.start` / `hermes.turn.end` marks.
- Spans are assigned to a turn by `turn_id`, with a timestamp-containment
  fallback.
- Session comes from metadata, else `parent_uuid`.

The turn-start mark's `data` carries the hermes hook kwargs. `user_message`
becomes `Turn.user_message`, shown as a prompt snippet in the turn list and
in-flight strip, and collapsible on the turn page.

Parse errors and assembly anomalies are always surfaced, never dropped — as
collapsed problem sections on the turn-list page only, so turn pages stay
uncluttered.

## Related

- What each span *shows* on the turn page: [span-rendering.md](span-rendering.md)
- Liveness, polling and follow mode: [live-pages.md](live-pages.md)
- Why the ATOF stream, and why no ETL: `docs/adr/0001`, `docs/adr/0002`
- Producer-side setup: [setup-prompt-timing.md](setup-prompt-timing.md)
