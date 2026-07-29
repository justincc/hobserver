# Span rendering on the turn page

What a span shows, scope by scope. The turn page (`templates/prompts/turn.html`)
lists one row per span or mark; the readers behind it are `Span` properties in
`plugins/prompts/assembler.py`.

Example values here are illustrations from one stream, not a contract: hermes'
tools differ between users and versions, and a payload's real shape is
whatever the tool's own signature says (`$h/tools/`, and
`$h/plugins/memory/mem0/` for the mem0 tools). Anything phrased as "in
practice" means observed while building this, and could differ for you.

## The two layouts

Every row has a summary line and a detail layout:

- **Summary line** — the scope's own text trails the span name on one
  ellipsized line. This is what is visible without interacting.
- **Detail** — the same content laid out on its own rows (`.list-item`,
  `.span-detail`), with the parts too long for one line added.

The details switch — a slider on the right of the legend row, off on every
page load and never persisted — turns detail mode on for the whole page.
Clicking a single row toggles just that row independently of the switch
(`tr.detail-open`, remembered by uuid across live-poll swaps); clicks on
links, buttons or a text selection do not toggle.

Whatever the summary line ellipsizes is in the row's `title` attribute, so a
hover shows the full text without switching modes.

The span or mark uuid sits muted after the name with a copy icon: it is the
lookup key into the raw JSONL. `tool_call_id` and `api_request_id` are not
shown.

## What is read

Nearly everything below comes from the span's **start** payload — the call's
input. Output is read in exactly three cases, each because the call is
unreadable without it:

- **failures**, on every scope (see [Failures](#failures));
- **memory**'s char budget, which is the whole story of that tool;
- **mem0_search**'s hits, since a query never says whether the answer was any
  good.

Payloads are opaque and read defensively even where the log has been uniform.

## llm scopes

An llm span carries what the model decided, said and cost — all of it in the
**end** event. The start payload is an empty `headers` dict and a `content`
that is usually empty, so the generic fallback below has nothing to work with;
this is a branch of its own.

- **Finish reason, and the calls it names** — one row. The reason
  (`.mode-tag`) is always visible; `tool_calls` means the spans below are
  what it asked for, and `length` would mean the answer was cut off. The
  calls themselves (`assistant_message.tool_calls`) sit beside it,
  **detail-only and names only**:

  ```
  tool_calls  skill_view · mem0_search · terminal · read_file · read_file · skill_view
  ```

  They ride the reason rather than taking a row of their own, which would
  have needed a `calls` label restating the `tool_calls` next to it. Only
  the names carry `.list-item`, so the summary line keeps the reason alone.
  They are a `.call-list` span and deliberately not a `.path` one: the
  collapsed layout forces `.path` back to `display: inline` with a more
  specific selector than the one `.list-item` hides it with, so the pair
  would have put the names on the summary line.

  The arguments stay on the spans below, each rendered by the branch written
  for that tool; repeating them here would restate the waterfall, which is
  why this was left out to begin with. What it adds is that the calls were
  **one decision**: 441 assistant turns in the log fan out to two or more,
  and one to sixteen — something a column of separate rows does not say.
  Repeats and order are kept, since both are the model's own.

  An entry that cannot be read keeps its slot (`(unreadable)` for a
  non-dict, `(unnamed)` for a missing name) rather than being dropped into a
  shorter, tidier, wrong list — the count is the point. The row also renders
  when there are calls but no reason, so those are never lost. A call with
  no matching span below is still possible; it is treated as an error, not a
  case this page detects.
- **What the model said** — `assistant_message.content`, detail-only. Empty
  whenever the model was calling tools rather than talking, and long enough
  otherwise that the row shows the first 400 characters
  (`LLM_TEXT_PREVIEW_CHARS`) and then says how many there were —
  `start of 2,579 chars`.
- **Tokens** — `usage`, as a tree (`TOKEN_TREE`), one row per count. In the
  detail layout:

  ```
  tokens
      prompt 19,349 (93% cached)
          cache read 17,920
          in 1,429
          cache write 4,096
      out 1,089
          reasoning 303
  requests 1
  ```

  and on the summary line, where the parts are hidden and a `·` divides what
  is left from the finish reason:

  ```
  tool_calls · prompt 20,193 (89% cached) · out 84
  ```

  `cache read` leads the prompt's rows: how much of it the provider already
  had is the question they are usually being read to answer, and `in` reads
  as the remainder — what was new — once it follows.

  **There is no `total` row.** It would be `prompt + out`, both of which are
  here, and it was the one figure the tree could not vouch for — see the
  relations below. The `tokens` label heads the tree in its place, carrying
  no number of its own; it is detail-only, because beside a figure already
  named `prompt` it tells a summary-line reader nothing.

  The indent is arithmetic, not decoration — and both relations behind it
  now hold firmly:

  | relation | strength |
  |---|---|
  | `cache read + in + cache write == prompt` | structural — hermes computes `prompt` as this sum on every emit path (`CanonicalUsage.prompt_tokens`), so it cannot diverge |
  | `reasoning <= out` | observed, not enforced — hence the marking below |

  (The dropped `total` was the exception: `prompt + out == total` held
  throughout the log but was never guaranteed, because hermes' codex path
  prefers the provider's reported total over the computed sum
  — `codex_runtime.py`. Nothing on screen depends on that agreement now.)

  So the rows under `prompt` are its parts by construction. `reasoning` is
  counted *within* `out` rather than alongside it, and is marked `.tok-part`
  (fainter, and its tooltip says so) precisely because it must not be added
  to its siblings.

  `out − reasoning` is **not** the reply. It is everything else the model
  emitted — the reply *and* the arguments of any tool calls — and on most
  calls here it is mostly the latter: 907 of 1,129 llm scopes in the log
  made tool calls, and those usually carry no message content at all, so
  hundreds of output tokens went on arguments that this span deliberately
  does not list (they are on the spans below). Both tooltips say so, because
  the obvious reading of a `reasoning` row nested under `out` is that the
  remainder is what you read.

  **The cache share** — `(93% cached)` — rides the `prompt` figure in both
  layouts. On the summary line it is the only sign the cache was involved at
  all, `cache read` being detail-only; in the detail layout it sits directly
  above the two rows it is read off, where it can be checked. Rounded to
  whole percent, half up rather than to even, since a reader comparing rows
  expects `.5` to go up.

  It never reads `100% cached` unless every last token was: straight
  rounding puts 27 of the 1,129 calls in the log at 100 with hundreds of
  tokens still fresh (129,536 of 130,361 is 99.37%), so the figure caps at
  99 unless `cache read == prompt` exactly. One percentage point of
  imprecision beats a figure that claims a whole prompt was cached when it
  was not.

  `0% cached` **is** shown — a cold prompt (the first call of a session,
  say) is a fact worth stating, and every llm row then reads the same shape
  whether or not the cache was involved. But *absent* is not *zero*: a
  payload that never reported `cache_read_tokens` gets no share at all,
  because this app cannot tell nothing-cached from nothing-said. Every one
  of the 94 cold calls in the log reports the zero explicitly.

  **`cache read` and `in` keep their rows at zero** (`ALWAYS_SHOWN`), where
  every other count loses one. They are the split the share is read off, and
  the detail view is where a reader goes to see it; an absent row leaves
  them working out whether the figure is zero or was never reported. So a
  cold call has the same shape as a warm one, differing only in the numbers:

  ```
  prompt 18,824 (0% cached)        prompt 20,193 (89% cached)
      cache read 0                     cache read 17,920
      in 18,824                        in 2,273
  ```

  `prompt` and `in` then carry one figure an indent apart. That repetition
  is the price of the split always being visible, and `cache read 0` beside
  it is what makes the pair say something a lone `prompt` would not.

  `cache write` is **not** in that set, and its zero is still dropped — see
  below for why its zero is not a reading. Every other count keeps the plain
  rule: zero, no row. Because the leaves partition their parent, dropping
  one still leaves what is shown adding up.

  `cache write` is 0 on every call in the log today, and on the codex route
  that is hard-wired rather than a property of the provider: hermes'
  `codex_runtime.py` builds its usage with `cache_write_tokens=0` outright,
  bypassing the normalizer that would have looked for the field. That is why
  it is not in `ALWAYS_SHOWN` while `cache read` is: a `cache read 0` is
  something the provider reported, but a `cache write 0` would be this app
  asserting a measurement nobody took. The field is filled for real on the
  Anthropic route, from `cache_creation_input_tokens` — the part of a prompt
  the provider stored for later calls to read.

### How the two layouts are split

`.list-item` is this app's detail-only marker (`base.html`) — one rule hides
every element carrying it on a collapsed row. So the split is made by which
rows get the class, not by a mechanism of their own:

- `prompt` and `out` (`summary` in `token_rows`) omit it and survive the
  collapse, `prompt` taking its cache share with it; the `tokens` label, the
  parts and `requests` carry it.
- `requests` is top-level but stays detail-only: it is a count of API calls,
  not of tokens, and does not earn a place on a line being scanned.
- The whole tree inlines onto one clipped line when collapsed, so five more
  labelled figures would be five for the ellipsis to eat — and a clipped
  count reads as a real one.

A `·` divides the buckets from the finish reason and from each other. It is a
real `.gen-key` span the template puts inside the row — the same element
`payload_rows` uses for its own separators, so the two are the same glyph in
the same font at the same colour. A pseudo-element was tried first and was
visibly wrong twice over: it inherited the body's proportional font, where a
middle dot is a lighter mark than the monospace one beside it, and the row's
`margin-left` sat outside it, leaving a wider gap before the dot than after.
A row carrying a dot now takes `.tok-sep`, which drops that margin, so the
dot has one plain space either side.

The template decides where dots go, tracking whether anything precedes — so a
row with nothing to its left never gets a leading one.

Tokens are on the row rather than in the bar's tooltip because a cache read
can be most of a prompt, and is often the difference between a fast call and a
slow one — worth seeing on the span, not on hover.

The leaves — `in`, `cache read`, `cache write`, `out` — are the four counts
the provider reports; the parents are sums the leaves make. An llm payload
carries `usage`, `model`, `finish_reason` and `assistant_message`, and nothing
else, so anything a row says beyond those has to come from somewhere other
than the log.

## Unrecognised scopes

Everything below is a branch written against a hermes tool this app knows. A
scope with no branch — another hermes user's tools, or one added since —
falls back to rendering its start payload directly, so it shows what the call
was for rather than a bare name and duration. Marks with no branch do the same.

The rules (`generic_payload_fields` in `plugins/prompts/atof_reader.py`):

- Keys come in payload order, which is the tool's own argument order.
- The first three scalars ride the summary line; every key gets a detail row.
- A value over 2 KB is named and measured — `conversation_history: list, 412
  items (7.3 MB)` — never printed. Payloads reach megabytes (a turn mark
  carries the whole conversation history), and a live turn page refetches
  itself every 2 s.
- Correlation plumbing is skipped (`session_id`, `turn_id`, `tool_call_id`,
  `telemetry_schema_version`, …): it is already on the row or of no interest.
- So is anything whose key looks like a credential (`headers`,
  `authorization`, `token`, `api_key`, …). Nothing fills those keys today —
  hermes' llm spans carry an empty `headers` dict — but a generic renderer
  must never be what puts a bearer token on a screen.

A scope with its own branch never collects these rows: the fallback is the
`else` of the branch chain, so curated rendering always wins.

## Scopes

### terminal — `command` / `workdir`

Command in monospace, workdir with the home prefix collapsed to `~`. The
command wraps in full in detail mode with line breaks kept — code takes
`pre-wrap`, unlike the `normal` wrap prose details use — so a heredoc or a
multi-command script stays readable.

### file tools — `path`

Left-ellipsized (`.tail`) so the filename end survives a tight line.

### search_files — `pattern` / `file_glob` / `path`

Pattern in monospace, then the glob and the search path.

### patch — two modes

Checked against `patch_tool` in `$h/tools/file_tools.py`. One branch, named by
a `.mode-tag`:

- **replace** (the tool's default) — `path` plus `old_string`/`new_string`.
  Must be matched *before* the plain-path branch it would otherwise fall into.
- **patch** — a V4A multi-file `patch` text and no top-level path.
  `Span.patch_paths` lists the files from its `*** <Op> File:` headers: first
  path plus a "+N more" count.

`Span.patch_mode` prefers the payload's explicit `mode`, falling back to the
keys present (a `patch` text ⇒ patch, else the default, replace).

Detail mode adds what the summary line cannot carry: the two replaced sides
for replace, the whole V4A text for patch. Never the end payload's
`diff`/`files_modified`/`lint` — nothing reads those today.

### web_search, mem0_search — `query`

Monospace; wraps in full in detail mode instead of ellipsizing. mem0_search
also renders what came *back*, see
[mem0_search results](#mem0_search-results).

### session_search — four modes

Checked against `$h/tools/session_search_tool.py`, rendered mode-aware in one
branch.

`Span.session_search_mode` prefers the end payload's explicit `mode`, falling
back to inferring it from the start-payload keys using the tool's own dispatch
precedence — an `around_message_id` anchor ⇒ scroll, else a `session_id` ⇒
read, else a `query` ⇒ discover, else browse — so a still-open span resolves
too. A muted `.mode-tag` names it.

`Span.session_search_summary` is the inline one-liner from the start payload:

| mode | summary |
| --- | --- |
| discover | the query |
| scroll | `session <id> · around msg <n> · window <w>` |
| read | `session <id>` |
| browse | `recent sessions` |

`Span.session_search_stats` is a list of {label, value, tooltip} rows from the
end payload, shown on detail-only `.list-item` rows, one per count the mode
reports:

- **discover** — `count` (sessions actually returned; lower than searched when
  a match is title-only or its anchored view cannot be built) and `sessions
  searched` (distinct matching sessions, deduped by lineage and capped at the
  limit — **not** the corpus scanned, so count ≤ sessions_searched ≤ limit).
- **scroll** — `before` / `after`: messages outside the returned window.
- **read** — `messages` total, plus a `truncated` flag.
- **browse** — `sessions` listed.

Each label's tooltip carries the same explanation.

### web_extract — `urls`

First url plus a "+N more" count inline; every url on its own line in detail
mode.

### execute_code — `code`

First line inline, the whole program in detail mode.

### vision_analyze — `image_url` / `question`

The image path or URL left-ellipsized in the summary; the question added on
its own line in detail mode only.

### mem0_add, mem0_update, mem0_delete — the fact and the id

The four mem0 tools are defined in `$h/plugins/memory/mem0/__init__.py`, i.e.
inside hermes' *memory plugin*, not in `$h/tools/` where the rest live. Do not
confuse them with the [`memory` scope](#memory--the-in-prompt-stores), which
is a different tool.

mem0_add carries the fact as `content` (plain text, not monospace); mem0_update
carries it as `text`.
`Span.memory_content` reads whichever key the scope uses, so the fact leads
the summary line either way, and wraps in full in detail mode.

mem0_update and mem0_delete also carry a `memory_id`, kept to a detail-only
row (faint mono, copy button — the lookup key back to a mem0_search result).
On the summary line it reads as a second uuid beside the span's own; expanding
the row is enough to reach it.

#### The previous text

An update and a delete both show what the memory said *before* the change,
recovered from the local event log by `memory.prior_memory_text` (ADR 4 covers
why it is an `app.extensions` accessor rather than a link; `prior_memory` in
the prompts turn view holds the per-span map). It renders through the same
`diff_rows` macro the patch scopes use — an update gets − old / + new, a
delete only the − side — under a muted `.prov` row naming the source.

mem0 itself is never queried and could not answer: hermes' `Mem0Backend`
exposes only search/add/update/delete (no get, no history), and the platform
cannot return a deleted memory at all.

The log can answer because a mem0_search result carries each hit's text beside
its id, and the agent can only learn an id *from* a search — so every change
is preceded by the search that surfaced it — in practice within the same
session, seconds to minutes earlier.

It is therefore the memory as of that search, not a guaranteed pre-change
snapshot. The row and its tooltip say the text is from the local log, name the
search event and the gap, and state that a change made outside hermes in
between would not show. **Never present it as something mem0 vouched for.**

#### Why a delete leads with it

A mem0_delete *leads* its summary line with the recovered text, the way an add
leads with its `content`: the id is the delete's whole payload, so the row
would otherwise say nothing about what was destroyed even before being opened.

That line is `.list-compact`, so detail mode drops it in favour of the − row
carrying the same text in full. An update's summary line is its own new `text`
and stays put — the old text is the − row's job, and displacing the new one
would hide what the span actually wrote. Deletes whose text was never
recovered fall back to showing only the id.

### memory — the in-prompt stores

The other memory tool (`$h/tools/memory_tool.py`): bounded §-delimited entries
in two char-limited files under `$HERMES_HOME/memories/` — MEMORY.md (the
agent's own notes, `target` "memory") and USER.md (who the user is, `target`
"user"). They are injected into the system prompt as a snapshot at session
start, where mem0 is searched on demand.

Because the stores are small (1375 chars for user by default) most writes are
entries being shortened to fit, and failing the budget and retrying within the
turn is routine rather than exceptional.

Two payload shapes, dispatched in this order: an `operations` list of
{action, content?, old_text?} applied atomically, else a single top-level
{action, content?, old_text?}. `Span.memory_ops` normalizes both to one list.
`Span.memory_action` is the mode tag — "batch" whenever `operations` is
present (it wins over an explicit `action`, matching dispatch, and a staged
batch replayed from the approval queue carries both).

`old_text` is a short unique substring the tool matches by containment, **not**
an id.

Rendering: a `.mode-tag` and the store name always visible, the first op's text
(content, or old_text for a remove) plus "+N more" inline, then per op the same
`diff_rows` macro the patch scopes use — an add shows only +, a remove only −,
a replace both — with a per-op action label only when there is more than one,
since the mode tag already names a lone op.

This is the one scope whose *end* payload is rendered as a matter of course,
because the char budget is the whole story of the tool:

- `Span.memory_stats` — `usage` and `entry_count` on detail-only rows, shown
  verbatim (the tool writes "97% — 1,335/1,375 chars" on success and a bare
  "1,338/1,375" on a rejection).
- `Span.memory_current_entries` — the whole store, which a rejection hands back
  for the model to consolidate before its own entry will fit; its error says
  "see current_entries below". Detail-only: it is the store's state, not this
  write.

### todo — `todos`

First item's content plus a "+N more" count inline; every item on its own line
in detail mode. A todo call *without* `todos` is a read of the current list and
shows nothing.

### delegate_task — `tasks` / `goal` / `context`

First goal plus a count inline; in detail mode every goal in full with its
`context` nested fainter and indented beneath. Covers both shapes: a batch
`tasks` list and a single top-level `goal`/`context`.

`hermes.subagent.start` marks show their `child_goal` the same way;
`subagent.stop` marks show `child_status` (e.g. timeout) plus the echoed goal,
with the session id and `duration_ms` on a detail-only line. Both carry a
per-turn #N tag (in start order) pairing start with stop, since
`child_session_id` is the only key both marks share.

### skill_view, skill_manage — `name` / `file_path` / `action`

Plus, on skill_manage:

- `category`, which a "create" carries — shown before the skill name,
  middot-separated. Absent on patch/write_file, which have no category.
- `absorbed_into`, which a "delete" that merged the skill elsewhere carries —
  rendered "→ absorbed into \<skill\>".
- `old_string`/`new_string`, which a "patch" carries. Note skill_manage patches
  replace text; they never carry a V4A patch text the way the file tools' patch
  scope does in patch mode. The pair matches that scope's *replace* mode
  instead, and both render through the one `diff_rows` macro in
  `templates/prompts/turn.html`.

The two sides render on their own detail-mode-only rows (`.list-item`), marked
− and + — glyph first, tint second, never color alone. `new_string` may be the
empty string, a patch that deletes the matched text (both tools document
passing "" for that), so `Span._new_string` — shared by `skill_new_string` and
`patch_new_string` — reads the payload directly instead of `_start_str`, which
folds "" into None; the empty side renders in words. They stay out of the
summary line because a real `new_string` runs to kilobytes of markdown.

skill_manage's six actions are create/edit/patch/delete/write_file/remove_file
(checked against `$h/tools/skill_manager_tool.py`). Only create, patch and
write_file have turned up in practice so far, so the other three are covered
by test alone.

`file_path` (skill_view, skill_manage write_file and remove_file, and an
optional patch target) is middot-separated from the skill name and
left-ellipsized (`.tail`, like the file tools' path).

## Failures

Every hermes tool reports a failure the same two ways: `metadata.status` is
"error" on the end event, and the end payload carries an `error` string. That
is how the tools in `$h/tools/` report errors, and has held for every failing
call seen here (terminal, patch, read_file, search_files, write_file,
execute_code, web_search, skill_view, skill_manage, memory), so `Span.failed`
and `Span.error` are one generic pair rather than a reader per scope, rendered
after the scope's own branch as a `badge-error` beside the name plus an `.err`
row.

Both stay out of detail mode: failures are common enough — some tools fail
more often than they succeed — that noticing one must never need a click.

## mem0_search results

`Span.mem0_results` reads the end payload —
`{"count": n, "results": [{id, memory, score}, …]}` — which arrives as a JSON
string ranked by score descending. That shape has been uniform in practice
and is still read defensively: payloads are opaque per the ATOF spec.

Only the top three render, on detail-only rows: whole facts would swamp the
summary line. How many is bound once, as `shown` in the template — the same
slice decides whether the link below reads "all N results" or "full result",
so a preview that already covers everything never promises more behind it.

Each hit's score leads its row, then the fact; the memory id sits on its own
faint `.mem-id` row, the same treatment mem0_update/mem0_delete give theirs.
Those spans' ids are a lookup key *for* these, so the pair resolves without
leaving the UI.

### The link to the Mem0 tab

A last row links to the whole ranked list in the Mem0 tab
(`Span.mem0_result_count` names it: "all 10 results"). This is the one place
two plugins meet, and they share no key — ATOF carries no event id, the db
carries no span uuid.

`mem0.search_event` therefore matches on (session_id, query), which has
resolved to exactly one event for every span checked. An optional `ts` (the
span start, in epoch µs) breaks the only tie possible: the same query twice in
one session.

It is a redirect owned by the mem0 plugin, not a lookup at render time — the
Prompts tab never opens the event log, and a turn page polling every 2 s costs
no queries. Unmatched requests 404 saying so: the two logs are written
independently, so either can cover a call the other misses.

## Reading hermes' payloads

Check a tool's signature in the hermes source rather than inferring the shape
from the log alone — the log only shows what has been exercised so far. Paths
above are relative to `$h` (the hermes-agent checkout).

The `platform` kwarg is `webui` today, but hermes has other frontends (a TUI,
for one). Never assume webui.
