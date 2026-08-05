# The ATOF reader

Three layers in `plugins/prompts/`, per ADR 2 (direct JSONL reading, no ETL).
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

### Two exporters, one file

hermes writes this stream through two owners **at the same time**.
`AtofEvent.schema` says which wrote each event:

| schema | who | how to tell |
|---|---|---|
| `hermes.observer.v1` | the `observability/nemo_relay` **plugin** | stamps `metadata.telemetry_schema_version` |
| `hermes.relay.runtime` | hermes **core** (`agent/relay_runtime.py`), from 2026-07-19 | stamps nothing |

The core runtime did not replace the plugin — it landed beside it. The
plugin still emits `hermes.turn.*` marks, session ids and unwrapped
prompts; the core runtime emits the scope tree, the provider-native
payloads, and a `hermes.chunk` flood that can push consecutive marks
thousands of lines apart. Both accounts of the same turn are in the file,
milliseconds apart.

**So the dial is read per event, never per file**, and neither exporter can
be assumed absent because a sample window did not happen to contain it.

What actually broke the Prompts tab on 2026-08-03 was not the marks
stopping — they never stopped. It was that **spans lost their
`session_id`**: tool spans kept a `turn_id` that still matched the marks,
but with no session to look it up in they fell to `(unknown session)`,
which had no turns, and llm spans carry no `turn_id` at all.

An unrecognized `telemetry_schema_version` is returned verbatim and
`schema_is_known` goes False. Such an event's payload is left exactly as it
arrived — an unknown envelope is not one to map — so it still renders through
the generic fallback rather than being silently misread (ADR 2).

### Normalizing the core runtime's payloads

The core runtime emits the provider's own response object where the plugin
emitted hermes' canonical envelope. Rather than teach forty `Span`
properties, the assembler and every template about a second shape, new events
are mapped back onto the canonical one as they are parsed — `atof_reader` is
the only module that knows there were ever two formats.

hermes speaks two provider APIs, and they are shaped differently: the main
loop uses `openai_responses`, while auxiliary work — context compression,
and whatever a plugin registers through `register_auxiliary_task` — goes out
over `openai_chat`. **`category_profile.annotated_response` is hermes' own
normalization and is uniform across both**: its `message` matched the raw
payload text exactly on every llm call in the log, both routes. So it leads,
and the raw payloads are the fallback.

| canonical field | recovered from |
|---|---|
| `assistant_message.content` | `annotated_response.message`; else `data.output[type=message].content[type=output_text].text` (responses) or `data.choices[].message.content` (chat) |
| `assistant_message.tool_calls` | `annotated_response.tool_calls`; else `data.output[type=function_call]` (responses) or `data.choices[].message.tool_calls`, whose name and arguments nest under `function` (chat). Arguments arrive as a JSON *string* on both raw routes |
| `finish_reason` | `annotated_response.finish_reason` |
| `usage.prompt_tokens` | `usage.input_tokens`, else `annotated_response.usage.prompt_tokens` |
| `usage.cache_read_tokens` | `usage.input_tokens_details.cached_tokens`, else `annotated_response.usage.cache_read_tokens` |
| `usage.cache_write_tokens` | `usage.input_tokens_details.cache_write_tokens` |
| `usage.output_tokens` | `usage.output_tokens`, else `annotated_response.usage.completion_tokens` |
| `usage.reasoning_tokens` | `usage.output_tokens_details.reasoning_tokens` |
| `usage.input_tokens` | **derived**: prompt − cache read − cache write, and only where the cache read was reported |
| `metadata.session_id` | the leading segment of the composite `turn_id`, else the llm request's `extra_headers.session_id` |
| `metadata.status` | `"error"` when the end payload carries an error string |

The raw payload wins wherever both report a count: on the responses route it
is the more detailed of the two, carrying the cache write and the reasoning
split that the annotation does not. The annotation only fills what the raw
payload did not say — which on the chat route is nearly everything, since
its `usage` reports a `total_tokens` and nothing else.

Three of these are traps worth keeping written down:

- **`input_tokens` changed meaning.** The provider's is the whole prompt
  including what the cache served; hermes' meant only what was sent fresh.
  Passing it through unchanged overstates fresh input by the entire cache
  read. It is derived as the remainder instead, and omitted when the parts
  do not partition the prompt.
- **`otel.status_code` is not the old `status`.** It reports the span's
  transport and reads `OK` on every failing tool call in the log. The tool's
  own error string is what the outcome is taken from.
- **A missing cache read is not a cache read of zero.** The `openai_chat`
  route reports no cache figures at all; deriving fresh input there would
  state that the whole prompt was new, which is a claim about caching from a
  payload silent on it. So `input_tokens` is derived only where the cache
  read was actually reported.
- **`target` is not a renamed `file_glob`.** Both are still separate
  parameters of hermes' `search_files` (`tools/file_tools.py`); `file_glob`
  is simply absent when the call did not filter. Nothing to map.

`request_count` has no counterpart and stays absent — absent is not zero.
Facts the new exporter never emits (subagent and approval marks) are absent,
not renamed, and normalization does not invent them.

## assembler.py — events → turns

- Spans are paired by uuid.
- Turns are bounded by `hermes.turn.start` / `hermes.turn.end` marks.
- Spans are assigned to a turn by `turn_id`, with a timestamp-containment
  fallback.
- Session comes from metadata, else `parent_uuid`.

The turn-start mark's `data` carries the hermes hook kwargs. `user_message`
becomes `Turn.user_message`, shown as a prompt snippet in the turn list and
in-flight strip, and collapsible on the turn page.

### Turns under the core runtime

The above is the mark-bounded path, and it still runs. Alongside it the core
runtime describes the same turn as a scope tree:

```
agent scope                     execution surface; empty payload
  └── hermes.turn               category "function"; start/end pair,
        │                       {"outcome": "success"} on the end
        ├── hermes.logical_llm_call    wraps each llm call
        │     └── llm scope           the physical provider attempt
        └── tool scopes
```

Spans find their turn by walking `parent_uuid` up to the nearest turn scope
(two hops for an llm call, one for a tool). That tree is the exporter's own
statement of ownership, so it wins over `turn_id` matching and over time
containment — and it is the only thing that places an llm span, which
carries no `turn_id`.

**Most turn scopes do not become a Turn.** The marks have usually built it
already, and building both put a duplicate empty row beside every real turn
— its spans having all gone to the other one. So a turn scope is first
matched to the Turn it is a second account of:

1. **by `turn_id`** — every span under a turn scope that carries one carries
   the *same* one, and it is the id the marks use. Exact.
2. **by overlap** — the fallback for a turn with only llm calls under it,
   which have no `turn_id` anywhere.

On a match the two are merged: the mark's account of *what the turn was*
(session, `turn_id`, hermes' own unwrapped `user_message`) and the scope's
account of *what ran inside it*, with the extent taken as the union of the
two intervals — they bracket the same work milliseconds apart, and the
waterfall lays its spans out from the turn's own start.

Only a turn scope with no mark behind it becomes a Turn of its own. It then
needs two things the scope does not carry:

- **Session** — from the first span beneath it that names one, else from the
  agent scope above. A turn that has run no span yet names nobody, and an
  agent scope is one session by construction, so its other turns answer for
  it. Without that fallback every turn *in flight* stranded itself in
  `(unknown session)` and duplicated.
- **The prompt** — from the first llm call's `annotated_request.messages`:
  the last user-role message, with hermes' `[Workspace::v1: …]` header and
  `<memory-context>` block stripped back off (`Span.request_prompt`). A
  reconstruction, not a value hermes emitted, and only reached when no mark
  supplied the real one. A message with neither wrapper is left whole rather
  than cut at a guess.

**`hermes.turn` and `hermes.logical_llm_call` are containers, never spans.**
Both have category `function`, so they count as neither llm nor tool time
while wrapping something that does. Left in, every turn double-counts its
model time and the wrapper's duration lands wholly in overhead.

Still gone, and not recoverable by any of this: subagent marks (delegation
tracking, `child_goal` / `child_status`), approval marks, and
`hermes.session.end`. Subagents are now visible only as nested `agent`
scopes under a turn, without the goal or outcome the marks carried.

Parse errors and assembly anomalies are always surfaced, never dropped — as
collapsed problem sections on the turn-list page only, so turn pages stay
uncluttered.

## Related

- What each span *shows* on the turn page: [span-rendering.md](span-rendering.md)
- Liveness, polling and follow mode: [live-pages.md](live-pages.md)
- Why the ATOF stream, and why no ETL: `docs/adr/0001`, `docs/adr/0002`
- Producer-side setup: [setup-prompt-timing.md](../running/setup-prompt-timing.md)
