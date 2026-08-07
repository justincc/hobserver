# The ATOF reader

Four layers in `plugins/turns/`, per ADR 2 (the JSONL is the source of
truth) and ADR 11 (a rebuildable index of it, so the log need not fit in
memory).

| layer | file | what it does |
|---|---|---|
| line reader | `tailer.py` | byte offsets → complete lines |
| parser | `atof_reader.py` | one line → one typed event |
| index | `atof_index.py` | events → a cached spine; payloads stay in the log |
| assembler | `assembler.py` | events → sessions → turns → waterfall |

Views refresh the index on each request and assemble in memory. The turn page
reads back the payloads of the one turn it is showing, and no others.

## tailer.py — the line reader

`read_lines(path, offset)` yields `(offset, length, text)` for each complete
line from a byte offset, streaming, so a rebuild of a multi-gigabyte log never
holds more than one read chunk. `read_at(path, offset, length)` is the other
direction — the pair is what lets the index store a line's position instead of
its contents.

It parses nothing and remembers nothing: the index owns the cursor, because
the cursor and the rows built from it have to advance together or not at all.

**Split the chunk on `b"\n"` only — never `str.splitlines()`.** splitlines also
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

What actually broke the Turns tab on 2026-08-03 was not the marks
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
| `usage.*` | see [the token counts](#the-token-counts) below |
| `metadata.session_id` | the leading segment of the composite `turn_id`, else the llm request's `extra_headers.session_id` |
| `metadata.status` | `"error"` when the end payload carries an error string |

Three of these are traps worth keeping written down:

- **`input_tokens` changed meaning.** The provider's is the whole prompt
  including what the cache served; hermes' meant only what was sent fresh.
  Passing it through unchanged overstates fresh input by the entire cache
  read. It is derived as the remainder instead, and omitted when the parts
  do not partition the prompt.
- **`otel.status_code` is not the old `status`.** It reports the span's
  transport and reads `OK` on every failing tool call in the log. The tool's
  own error string is what the outcome is taken from.
- **A missing cache read is not a cache read of zero.** Deriving fresh input
  from a payload that said nothing about caching would state that the whole
  prompt was new — a claim about caching from a payload silent on it. So
  `input_tokens` is derived only where the cache read was actually reported.
- **`target` is not a renamed `file_glob`.** Both are still separate
  parameters of hermes' `search_files` (`tools/file_tools.py`); `file_glob`
  is simply absent when the call did not filter. Nothing to map.

`request_count` has no counterpart and stays absent — absent is not zero.
Facts the new exporter never emits (subagent and approval marks) are absent,
not renamed, and normalization does not invent them.

### The token counts

> Lives in `plugins/turns/providers.py`, not `atof_reader.py` — everything
> that depends on *which provider answered* is there, and its token shapes
> are a published extension point
> ([ADR 13](adr/0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md),
> [writing-a-provider-spec.md](../extending/writing-a-provider-spec.md)).
> Kept documented here because it is the same normalization story as the
> section above, read in the same sitting.

Several producers write token counts into this log and no two agree on the
names. A `UsageShape` probes the payload's own keys — the route named in
`metadata` is a label, not a schema declaration — and maps them onto the
canonical names `assembler.TOKEN_TREE` renders.

**Most counts mean the same thing wherever they appear**, so one list of
sources per canonical name covers every producer; the first source that
answers wins. That is `_COMMON_USAGE`:

| canonical | sources, in order |
|---|---|
| `cache_read_tokens` | `input_tokens_details.cached_tokens`, `prompt_tokens_details.cached_tokens`, `cache_read_input_tokens`, `cache_read_tokens` |
| `cache_write_tokens` | `input_tokens_details.cache_write_tokens`, `prompt_tokens_details.cache_write_tokens`, `cache_creation_input_tokens`, `cache_write_tokens` |
| `output_tokens` | `output_tokens`, `completion_tokens` |
| `reasoning_tokens` | `output_tokens_details.reasoning_tokens`, `completion_tokens_details.reasoning_tokens` |
| `total_tokens` | `total_tokens` |

#### One count does not: the provider's "input" figure

Two mutually exclusive conventions are in the wild, and reading one as the
other is a **wrong number rather than a missing one**:

| convention | what the figure means | who | the app derives |
|---|---|---|---|
| **whole** | the entire prompt, cache included | OpenAI Chat Completions and Responses, and every OpenAI-compatible proxy — openai-codex, openrouter | `in` = prompt − cache read − cache write |
| **parts** | only what was sent *fresh*, cache counted alongside | Anthropic Messages | `prompt` = in + cache read + cache write |

hermes forks on the same distinction when it prices a call — the
`anthropic_messages` arm of `agent/usage_pricing.py` adds where the others
subtract — which is the corroboration for treating this as a real fork and
not a quirk of one payload.

The convention is not carried as a flag. It is **which canonical key the
table maps the figure to**: `_WHOLE_PROMPT` maps it to `prompt_tokens`,
`_FRESH_INPUT` maps it to `input_tokens`, and `_complete_the_prompt` fills
in whichever of the two is still missing. So the relation `cache read + in +
cache write == prompt` is the *definition* of the derived figure and cannot
diverge, in either direction.

Two traps in the probe, both load-bearing:

- **Anthropic's cache key names do not imply Anthropic's input convention.**
  OpenAI-compatible proxies routing Claude expose `cache_read_input_tokens`
  beside an OpenAI-shaped `prompt_tokens` that is still the whole prompt
  (hermes carries the same fallback, citing OpenRouter, the Vercel AI
  Gateway and Cline). Summing there would double-count the cache. So the
  `parts` convention needs both halves of its signature — a top-level
  `input_tokens`, *and* Anthropic cache names, *and* no OpenAI-shaped
  prompt key.
- **A bare `input_tokens` with nothing said about caching is read as the
  whole prompt.** The two conventions coincide when nothing was cached, and
  this is the reading that invents no cache rows.

Neither direction is derived from a payload silent on caching: subtracting
nothing would claim the whole prompt was fresh, adding nothing would claim
none of it was cached, and both are claims about caching from a payload that
made none.

Finally, hermes' own `annotated_response.usage` is the chat shape with the
cache read flattened to the top level, and fills only what the raw payload
did not say. The raw payload wins wherever both speak: on the responses
route it is the more detailed of the two, carrying the cache write and the
reasoning split the annotation does not.

**Where the counts are is not the same question as what they are called.**
On the chat-completions route the span's own end payload carries `usage:
null` — the provider reports usage only on the *last chunk of the stream*,
and hermes asks it to (`stream_options.include_usage`). Those chunks are
`llm.chunk` marks, which the index folds away rather than carrying as
events, so the counts reach a span through
[the index's stream row](#what-is-stored-and-what-is-left-in-the-log) and
`Span.usage` falls back to them. Without that fallback every openrouter call
shows no token counts at all while the log holds every one of them.

## atof_index.py — the index

A SQLite cache of the log's spine, so the log itself never has to fit in
memory. Decided in
[ADR 11](adr/0011-index-the-atof-log-rather-than-hold-it-in-memory.md), which
carries the measurements; this is what it looks like in the code.

### What is stored, and what is left in the log

One row per event, minus `llm.chunk` (below): the line's **offset and
length**, the **spine** (kind, uuid, parent_uuid, timestamp, name, category,
schema era), the **correlation envelope** verbatim — `metadata` averages
268 bytes — and the `category_profile` with its large values dropped.

**The rule for dropping is size, not key name.** A payload value over
`PAYLOAD_INLINE_MAX_BYTES` (4 KB) stays in the log; anything smaller is kept.
This app is not the authority on which of hermes' payload keys are the big
ones and the answer differs per tool, so nothing here names
`annotated_request`. What that threshold buys, measured on the live log:

| | kept in the index | left in the log |
|---|---|---|
| `category_profile` | 0.55 MB (`model_name`, `tool_call_id`) | 299 MB |
| whole log | 29 MB of SQLite | 1.19 GB |

A large *dict* payload is dropped down to whichever of its keys are small,
which is what keeps short fields like `error` and `child_session_id`
reachable from the turn **list** without a trip to the log.

### The three projections

Three facts are derived at index time out of payloads the index then leaves
behind, because assembly cannot build turns without them:

| projection | out of | needed for |
|---|---|---|
| `user_message` | a `hermes.turn.start` mark's data (570 KB average) | the prompt on every turn row |
| `request_prompt` | the first llm call's `annotated_request` | naming a turn no mark described |
| `data_session_id` | an agent scope naming its session in the payload | keeping its spans out of `(unknown session)` |

Each is read back through one accessor that prefers the projection and falls
back to the payload — `AtofEvent.projected`, `Span.request_prompt` — so the
same code works either side of the index.

**Keep this list short.** Every entry is a payload key this app knows about
outside a scope spec, which is the coupling ADR 7 and the fork test exist to
limit. A fact earns a place by blocking the assembly of turns, not by being
interesting to display.

### Staleness — four checks, all biased towards rebuilding

Byte offsets are only valid while the bytes under them have not moved. Each
check catches what the cheaper one before it cannot; all run on every refresh,
and the whole set costs a `stat` and two small reads.

1. **`st_dev`/`st_ino`, and size below the indexed extent** — rotation,
   replacement, truncation.
2. **sha256 of the first 64 KB** — an in-place rewrite that kept the inode,
   which `>` on an open path does.
3. **The seam**: the offset, length and hash of the *last indexed line*,
   re-read and compared. This is the one that survives a rewrite preserving
   both size and head, and past the first 64 KB it is the only one that can
   see anything at all.
4. **A hash of `atof_reader.py`, `assembler.py` and `atof_index.py`** — the
   stored spine means whatever those meant when they wrote it, and they keep
   changing while hermes' schema does. It over-invalidates (a docstring edit
   rebuilds), which at seconds per rebuild is the right side to be wrong on.
   **Expect a rebuild on the next request after any edit to those files.**

A failing check deletes the rows and rebuilds. There is no migration path and
no repair: deleting the SQLite file costs a rebuild and nothing else.

### Chunks

`llm.chunk` is 264,267 of the log's 281,490 lines — and reaches no template.
Carrying them costs 3.32 s of every request's assembly against 0.14 s without,
for byte-identical output.

They carry neither `session_id` nor `turn_id`, only a `parent_uuid` pointing
at their llm span, so under the mark-and-time attachment in `assemble` they
land in `(unknown session)` and reach no turn at all. The index aggregates
them to one row per span, holding the two things they can say:

| column | from | read by |
|---|---|---|
| `last_us` | `MAX(timestamp)` | `Span.stream_last_us` → `last_activity_us` |
| `usage` | the last chunk that carried any, mapped to canonical names | `Span.usage`, when the end payload reported none |

The timestamp is a small *improvement*: a long streaming call shows a sign
of life where before it looked silent since it started.

The usage is **not optional**. On the chat-completions route the provider
reports token counts only on the stream's final chunk and leaves the span's
end payload with `usage: null` — so for every openrouter call, this row is
the only place the counts exist. Dropping chunks wholesale would drop them.

A refresh that extends the index can pick up a stream mid-flight, so the row
accumulates rather than being replaced, and a batch that saw no usage must
not erase one an earlier batch stored (`coalesce(excluded.usage, usage)`).

### Hydration

`hydrate_turn(turn, log_path)` reads one turn's payloads back by offset. The
turn view calls it after finding the turn and before rendering; nothing else
calls it. On the live log this is ~3 ms for a 7-span turn.

A payload that cannot be re-read — the log rotated between assembly and
render — sets `Span.payload_problem`, which the turn page states on the row
rather than rendering a blank field (ADR 2). What the index kept is still
shown; the row says it is not the whole payload.

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
- Why the ATOF stream, and why no ETL: `docs/design/adr/0001`,
  `docs/design/adr/0002`; why an index of it all the same: `docs/design/adr/0011`
- Producer-side setup: [setup-prompt-timing.md](../running/setup-prompt-timing.md)
