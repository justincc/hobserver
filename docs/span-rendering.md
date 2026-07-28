# Span rendering on the turn page

What a span shows, scope by scope. The turn page (`templates/timing/turn.html`)
lists one row per span or mark; the readers behind it are `Span` properties in
`plugins/timing/assembler.py`.

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
the timing turn view holds the per-span map). It renders through the same
`diff_rows` macro the patch scopes use — an update gets − old / + new, a
delete only the − side — under a muted `.prov` row naming the source.

mem0 itself is never queried and could not answer: hermes' `Mem0Backend`
exposes only search/add/update/delete (no get, no history), and the platform
cannot return a deleted memory at all.

The log can answer because a mem0_search result carries each hit's text beside
its id, and the agent can only learn an id *from* a search — so every change
is preceded by the search that surfaced it. All 19 changes in the log follow
that pattern, same session, median 30 s earlier (13 s–2.9 min).

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
entries being shortened to fit, and roughly half the calls in the log error on
the budget and get retried within the turn.

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
  `templates/timing/turn.html`.

The two sides render on their own detail-mode-only rows (`.list-item`), marked
− and + — glyph first, tint second, never color alone. `new_string` may be the
empty string, a patch that deletes the matched text (both tools document
passing "" for that), so `Span._new_string` — shared by `skill_new_string` and
`patch_new_string` — reads the payload directly instead of `_start_str`, which
folds "" into None; the empty side renders in words. They stay out of the
summary line because a real `new_string` runs to kilobytes of markdown.

skill_manage's six actions are create/edit/patch/delete/write_file/remove_file
(checked against `$h/tools/skill_manager_tool.py`). The ATOF log to date only
exercises create/patch/write_file, so edit/delete/remove_file rendering is
covered by test alone.

`file_path` (skill_view, skill_manage write_file and remove_file, and an
optional patch target) is middot-separated from the skill name and
left-ellipsized (`.tail`, like the file tools' path).

## Failures

Every hermes tool reports a failure the same two ways: `metadata.status` is
"error" on the end event, and the end payload carries an `error` string. That
holds across every non-ok tool end in the log (terminal, patch, read_file,
search_files, write_file, execute_code, web_search, skill_view, skill_manage,
memory), so `Span.failed` and `Span.error` are one generic pair rather than a
reader per scope, rendered after the scope's own branch as a `badge-error`
beside the name plus an `.err` row.

Both stay out of detail mode: a failed call must not need a click to notice,
and ~7% of tool calls fail (skill_manage more than half).

## mem0_search results

`Span.mem0_results` reads the end payload —
`{"count": n, "results": [{id, memory, score}, …]}` — which arrives as a JSON
string ranked by score descending. That shape is uniform across all 102
mem0_search ends in the log (1080 results, no key ever missing) and is still
read defensively.

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

`memory.search_event` therefore matches on (session_id, query), which resolved
to exactly one event for all 104 mem0_search spans in the log. An optional `ts`
(the span start, in epoch µs) breaks the only tie possible: the same query
twice in one session.

It is a redirect owned by the memory plugin, not a lookup at render time — the
Prompts tab never opens the event log, and a turn page polling every 2 s costs
no queries. Unmatched requests 404 saying so: the two logs are written
independently, so either can cover a call the other misses.

## Reading hermes' payloads

Check a tool's signature in the hermes source rather than inferring the shape
from the log alone — the log only shows what has been exercised so far. Paths
above are relative to `$h` (the hermes-agent checkout).

The `platform` kwarg is `webui` today, but hermes has other frontends (a TUI,
for one). Never assume webui.
