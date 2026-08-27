# 6. Parse ATOF by declared schema era

Date: 2026-08-03

## Status

Accepted

Extends [ADR 2](0002-read-atof-jsonl-directly-no-etl.md).

## Context

hermes has written this stream under two different owners.

Until 2026-07-19 it was the `observability/nemo_relay` **plugin**, whose
envelope this app was built to read: `hermes.turn.*` marks bounding each
turn, `session_id` and `turn_id` on every span, and llm results in hermes'
canonical `{assistant_message, finish_reason, usage}` shape.

From 2026-07-19 hermes core writes it too (`agent/relay_runtime.py`, a
series by Alex Fournier merged late July). It reached this machine on
2026-08-03 and the Prompts tab stopped showing spans: every span after that
point landed in `(unknown session)`, which had no turns, so all of them were
filed as anomalies. 0 parse errors — the reader understood every line, it
just could not correlate them.

**The core runtime did not at first replace the plugin. It landed beside
it**, and both emitted together from 2026-07-19 until the plugin was removed
on 2026-08-10. This was not obvious while it lasted: the new exporter also
emits an `llm.chunk` per streamed token, which pushed consecutive turn marks
thousands of lines apart and made a sampled window look as though the marks
had stopped. They had not. Through that window every turn was described twice
— once as a pair of marks, once as a scope tree, milliseconds apart.

What actually broke was narrower than "the plugin stopped": spans lost their
`session_id`. Tool spans kept a `turn_id` that still matched the marks, but
with no session to look it up in they fell to `(unknown session)`; llm spans
carry no `turn_id` at all and had nothing left to correlate on.

Both dialects are in one file and will stay that way: the log is append-only
and holds months of history. The plugin's removal on 2026-08-10 was a cutover
for what hermes *writes* — new spans are the core dialect alone — but not for
what this app *reads*: the plugin era is still in the log and always will be.
So the reader carries both dialects indefinitely. Only the overlap window
(2026-07-19 to 2026-08-10) has two accounts of every turn; before it there is
only the plugin's marks, after it only the core runtime's scope tree.

Three facts shaped the decision:

- **The old exporter stamped its version.** Every plugin-written event
  carries `metadata.telemetry_schema_version: "hermes.observer.v1"` — 100%
  of them. The core runtime stamps nothing. Presence of the key is the dial.
- **The eras interleave.** When hermes restarted onto the new code the old
  runtime kept emitting for a further 13 events, and one scope pair
  straddled the changeover outright. A file-level sniff would misparse the
  tail of the old session.
- **Most of what "vanished" only moved.** `finish_reason` went into
  `category_profile.annotated_response`; the token counts into the
  provider's `usage` under different names; the turn into a `hermes.turn`
  *scope* rather than a pair of marks.

## Decision

**Detect the schema per event, and map the new dialect onto the old one at
the point of parsing.**

`atof_reader` tags every event with `AtofEvent.schema` and rewrites
core-runtime payloads into the canonical envelope. It is the only module in
the app that knows there were ever two formats: the assembler, all ~40
`Span` properties, every template and the entire existing test suite were
left unchanged.

Per event, never per file — because the eras interleave. A span takes its
era from its start event.

What could not be mapped, because it was structural rather than a rename,
is handled in the assembler: the core runtime nests a turn's work under a
turn scope by `parent_uuid`, so spans reach their turn by walking up that
tree. It is the only thing that places an llm span, which carries no
`turn_id`. The two wrapper scopes (`hermes.turn`,
`hermes.logical_llm_call`) are containers, never spans — left in, every turn
double-counts the model time they enclose.

**And because both exporters describe every turn, the two accounts are
merged rather than both built.** A turn scope is matched to the Turn the
marks already made — by the `turn_id` its spans carry, falling back to
interval overlap — and contributes its tree to that Turn instead of
becoming a second one. The mark is the better account of what the turn was;
the scope is the better account of what ran inside it. Building both put an
empty duplicate row beside every real turn.

**An unrecognized `telemetry_schema_version` is surfaced, not guessed at.**
`schema_is_known` goes False and the payload is passed through untouched, to
render via the generic fallback. Given how actively this part of hermes is
changing, the reader has to fail loudly at the *next* envelope rather than
quietly misread it.

## Consequences

Tool spans came back at full fidelity — their start payloads never changed.
LLM spans came back with `finish_reason`, tool calls, assistant text and the
whole token tree. Turn durations and the overhead residual are observed
again, from the turn scope. The user's prompt is recovered from the first
llm call's request.

Three things this buys that a per-file switch would not: a log spanning the
changeover renders correctly throughout; the 13 straggler events parse as
what they are; and adding a third dialect later means one more branch in one
module.

The costs, taken deliberately:

- **This bends ADR 2.** That ADR said read the JSONL directly with no ETL,
  and payloads are now transformed on the way in. It is still no ETL — no
  derived data is stored, nothing is written, the file remains the only
  source of truth — but the app no longer shows only what the exporter
  literally wrote. Where a value is derived rather than read
  (`usage.input_tokens`, the unwrapped prompt) the code says so at the site.
- **The canonical shape is now this app's, not hermes'.** It happens to
  match what the plugin emitted, which is a good target and keeps the
  templates honest, but nothing upstream maintains it any more. If hermes
  changes again, this is the layer that absorbs it.
- **Reconstruction is not emission.** Where no mark supplies a turn's
  prompt, it is taken as the last user message on the wire with hermes'
  wrappers stripped. Right in every case checked, and still an inference —
  which is why it is the fallback and the mark's `user_message` is
  preferred wherever one exists.
- **Merging assumes the two accounts agree.** They have, exactly, on every
  turn in the log — one `turn_id` per turn scope, matching a mark. If the
  exporters ever diverge on what a turn is, this merge hides the
  disagreement rather than surfacing it.
- **Some things are simply gone.** Subagent marks (delegation goals and
  outcomes), approval marks, and `hermes.session.end` are not emitted at
  all. No normalization invents them; the features that read them stay dark
  until hermes emits them again or the tree grows an equivalent.

## Related

- [docs/atof-reader.md](../atof-reader.md) — the mapping table, the turn
  tree, and the three traps (`input_tokens` changing meaning,
  `otel.status_code` not being `status`, `target` not being `file_glob`)
