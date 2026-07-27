# hermes-observer

A small Flask webapp for observing hermes-agent activity (formerly
jmem0-logged-browser). Views are plugins, shown as horizontal tabs:

- **Memory** — browses `jmem0_logged.db`, the SQLite event log produced by
  the hermes-agent mem0 logging wrapper.
- **Prompt timing** — per-turn latency waterfalls from the NeMo Relay ATOF
  JSONL stream exported by the hermes-agent `observability/nemo_relay`
  plugin: where each turn's time went (llm vs tool vs overhead), with a
  span timeline per turn. See `docs/adr/` for the design.

Every view is read-only over a log another process produces, so it is safe
to point at live files while hermes is writing to them.

## Running

```bash
uv run python app.py [path/to/jmem0_logged.db]
```

Then open http://127.0.0.1:5090. The server binds to `0.0.0.0`, so it is
also reachable from other machines on the local network at
`http://<host-ip>:5090`.

Edits are picked up without a manual restart: templates re-render from
disk on the next request (`TEMPLATES_AUTO_RELOAD`), and the Werkzeug
reloader restarts the server when a `.py` file changes. Debug mode stays
off — the Werkzeug interactive debugger allows arbitrary code execution
and must never be exposed on `0.0.0.0`.

Both data sources default to files under the hermes-agent config directory,
so with `HERMES_HOME` exported neither has to be passed: `uv run python
app.py` is enough. `HERMES_HOME` is normalized, since the agent conventionally
exports it as `<checkout>/hermes-agent/../config`; if it is unset, a built-in
literal path stands in.

The memory database path is resolved in this order:

1. First command-line argument
2. `JMEM0_DB` environment variable
3. `$HERMES_HOME/jmem0_logged.db`

The prompt-timing source is resolved as:

1. `ATOF_LOG` environment variable
2. `$HERMES_HOME/nemo-relay/atof/hermes-atof.jsonl`

pointing at the events JSONL file written by the nemo_relay plugin's ATOF
exporter. If that file does not exist, the Prompt timing tab says so and
names the path it tried, rather than showing an empty page.

On startup the app prints what it resolved — `HERMES_HOME`, the config
directory, both source paths with whether each is usable and which rule
supplied it, and the listening URL — because a missing source is the usual
reason a tab looks empty. It prints once at launch, not on reloader
restarts.

The memory database is checked before serving, and the app exits naming the
problem if it fails: the path must exist, be a regular file, and yield a row
from an `events` table when opened read-only. Existence alone was not
enough — a stray argument (`app.py .`, the current directory) passed an
exists check and then failed per request with a bare sqlite `disk I/O
error`, which reads like a failing disk rather than a wrong path. The ATOF
log is not checked this way: it is allowed to be absent, and the timing tab
reports that itself.

### Console noise and observer status

An **observer status** link sits at the right of the tab row on every page,
opening `/_status` in a new tab — new rather than in place because the tally
is for checking *while* a page sits there polling. It is deliberately not
called "requests": every other view in this app shows the agent's LLM and
tool requests, and this one shows none of that, only HTTP traffic to the web
app itself. The page repeats the distinction in its own header.


The live-poll pages refetch every 2-3 s, which would bury the console. So
each polled path logs its **first** successful request, then one line
pointing at `/_status`, then nothing. Ordinary page visits, and every
non-2xx/3xx response, keep logging as usual — a quiet log must never hide
an error.

The running tally lives at `/_status` (plain text, so it reads the same
curled or in a browser): per path, request count, how long since the last
one, and a status-code breakdown. Fetch it when a page stops updating —
it separates the three failure modes at a glance. No response means the
server is down; a stale *last* time means the browser stopped polling;
non-200 counts mean polls are arriving but failing.

It can be left open: a `Refresh` header re-requests it every 3 s (curl
ignores the header, so the body stays plain text and needs no template),
and the header line carries a clock, since counts alone look identical
whether the page is live or frozen. Its own requests are treated like any
other poll — logged once, then silent — and are excluded from the tally,
so watching the page never looks like traffic or refreshes its own
last-seen time.
End-to-end setup — enabling the exporter in hermes-agent (producing) and
pointing this tool at its output (consuming) — is in
[docs/setup-prompt-timing.md](docs/setup-prompt-timing.md).

## Pages

- `/` — redirects to the first tab (`/memory/`).
- `/memory/` — all mem0 events, newest first, with a truncated query; click
  an id or query to open the event. The page tails the log: it polls
  `/memory/fragment/events?since=<last id>` every 3 seconds and prepends
  any new rows to the top of the table.
- `/memory/fragment/events?since=<id>` — rendered table rows for events
  newer than `<id>`, newest first (used by the index poll).
- `/memory/event/<id>` — one event: all fields except query/result shown as
  a metadata block at the top, then the full query, the result, and the
  context messages rendered as plaintext. The `query` heading is named for
  what the column actually holds on that event type — "Query" on a
  `prefetch` or `mem0_search`, but "Added text" on a `mem0_add`, "New text"
  on a `mem0_update` and "Deleted memory id" on a `mem0_delete`, which store
  the written fact or the bare id there. An update or delete also gets a
  "Previous text" section: what the memory said before the change,
  recovered from the local event log (see below), with the search event it
  came from and how long before. Results that are JSON (e.g.
  `mem0_search` output) are pretty-printed; others (e.g. prefetch
  markdown) are shown as-is. The context messages are the up
  to 10 preceding prefetch queries logged for the same session (tool-call
  events are excluded — they are not user messages) — an approximation
  of the extra conversational context mem0 uses during retrieval — each
  prefixed with its event id, oldest first.
- `/memory/search-event?session=<id>&query=<q>[&ts=<µs>]` — redirects to the
  `/memory/event/<id>` page for one `mem0_search` call. This is how a
  mem0_search span in the timing tab hands off to the memory tab: the two
  logs record the same call but share no key (ATOF has no event id, the
  event log has no span uuid), so the pair is matched on session and query,
  with the optional `ts` (the span's start, epoch microseconds) breaking the
  only tie that can arise — the same query run twice in one session. A
  redirect rather than a lookup while rendering the turn page, so a page of
  spans polling every 2 s costs no database queries. 404s when the event log
  has no matching call: the two logs are written independently, so either
  can cover a call the other does not.
- `/timing/` — all turns, newest first: start time, session, a one-line
  prompt snippet (from the turn-start mark's `user_message`; ellipsized so
  long prompts never widen the table, em dash when absent), total / llm /
  tool / overhead durations (overhead is the residual), model-call and span
  counts. In-flight turns are marked. Parse errors and assembly anomalies
  are shown above the table (folded closed, on this page only), never
  dropped. The page updates itself every
  3 s (the tailer reads only what the exporter appended since the last
  request), so new turns appear without a manual reload.
- `/timing/turn/<session>/<start_us>` — one turn: the nav row ("all turns"
  followed, muted a shade, by "« prev" / "next »" stepping one turn older
  / newer through the
  same interleaved-by-start-time ordering the turn list uses, so they cross
  session boundaries just as it does; each titled with the neighbour's
  prompt, greyed out at either end), the in-flight strip,
  then the turn id / session / started heading (a muted icon beside the
  turn id copies it to the clipboard), the prompt (shown whole
  when short; collapsed to its first couple of lines with the full text a
  click away when long), summary stats,
  then the span waterfall (llm blue,
  tool orange, other violet; open spans faded) with offsets and durations
  as text. The turn's non-boundary marks (approvals, session end) are
  instantaneous, so they interleave with the spans as rows whose bar is a
  zero-width tick at their offset (clamped to the track — session end
  fires just after the turn-end mark). Spans whose start payload carries a
  `command`/`workdir` (terminal tool scopes) show them inline under the
  span name — command in monospace, workdir with the home prefix collapsed
  to `~`, both ellipsized with the full text in the title attribute; in
  detail mode the command wraps out whole, its line breaks preserved
  (`pre-wrap`, not the `normal` the other wrapping details use), so a
  heredoc or a multi-command script stays readable;
  skill scopes (`skill_view`/`skill_manage`) likewise show the skill's
  `name`, the `file_path` within the skill when one is targeted, and (for
  skill_manage) the `action` inline, and — for a skill_manage `patch` —
  the replaced text as two extra rows shown only in detail mode, the
  `old_string` marked − and the `new_string` marked +, each wrapping out
  in full (an empty `new_string` is a deletion and says so in words);
  file tool scopes (patch, read_file,
  write_file, …) show their `path`, ellipsized from the
  left so the end of a long path stays visible; patch scopes in patch
  mode carry no top-level `path` — the touched files are extracted from
  the V4A `patch` text's `*** <Op> File:` headers and shown as the first
  path plus a "+N more" count, all paths in the title attribute;
  search_files scopes show
  the `pattern` (monospace), `file_glob` and search `path`; web_search
  and mem0_search scopes show the `query` (monospace; wraps out in
  full in detail mode instead of ellipsizing), and a mem0_search adds
  what it retrieved: with the details switch on, the top three of its
  ranked `results` — relevance score, then the remembered fact, with the
  memory id on a faint row beneath (the same id a mem0_update or
  mem0_delete names, so the two can be matched by eye), followed by a
  link into the Memory tab for the whole ranked list ("all 10 results
  in Memory →"); web_extract scopes show the first
  of their `urls` plus a "+N more" count, with the full list in the
  title attribute and every url on its own line in detail mode;
  execute_code scopes show the first line of their
  `code`, with the full program in the title attribute and, in detail
  mode, laid out in full the same way the command is; mem0_add scopes
  show the remembered `content` (plain text, not monospace; wraps out
  in full in detail mode); mem0_update and mem0_delete scopes show the
  `memory_id` they name and, in detail mode, what that memory said before
  the change — an update as a − old / + new pair, a delete as the − side
  alone, since its payload is only an id and this is the sole record of
  what was lost. That previous text is recovered from this app's own event
  log, never fetched from mem0 (which cannot supply it: hermes' mem0
  backend has no get or history call, and a deleted memory is gone), so
  the row says "previous text from the local log", links to the
  `mem0_search` event it came from and how long before, and its tooltip
  spells out that mem0 is never queried and that a change made outside
  hermes in between would not be reflected; todo scopes
  show the first of their `todos` items' `content` plus a "+N more"
  count, all items in the title attribute, and with the details switch
  on show every item on its own line instead (a todo call without
  `todos` is a read of the current list and shows nothing);
  delegate_task scopes show the first subagent brief's `goal` plus a
  "+N more" count, and in detail mode every goal in full with its
  `context` nested fainter and indented beneath it (both batch-mode
  `tasks` lists and single-mode top-level `goal`/`context` payloads);
  hermes.subagent.start marks likewise show their `child_goal` —
  ellipsized inline, in full in detail mode. Each subagent also gets a
  per-turn tag (#1, #2, … in start order) shown on both its start and
  stop rows so the pair can be matched by eye in the one-line view;
  the stop row shows the `child_status` (e.g. timeout) and echoes its
  start's goal, pairing internally via `child_session_id` — the only
  key both marks carry — with the session id and `duration_ms` on a
  detail-only line. By default this extra info trails the span name on a
  single ellipsized line so every row stays one line tall; a "details"
  slider switch on the right of the legend row — off on every page load,
  never persisted — expands it onto its own line under the name and
  reveals the span uuids. Clicking a span or mark row toggles that one
  row between the collapsed and detailed layouts independently of the
  switch (clicks on links, buttons, or a text selection don't toggle);
  the open rows are remembered by uuid so they survive the in-flight
  page's self-updates. Each
  span and mark line carries its ATOF uuid — the key for finding its
  lines in the raw JSONL — in small muted monospace right after
  the name with a copy-to-clipboard icon (like the turn id's), so the
  name stays prominent (other correlation ids like
  tool_call_id are not shown). While the
  turn is in flight the page updates itself every 2 s; once it ends the
  page is static.

Timing pages self-update via a small poll-and-swap script in
`templates/base.html`: any element with `data-live-poll="<ms>"` is
refetched from the current URL on that interval and swapped in place
(`"0"` means static). Polling pauses while the tab is hidden.

Both timing pages show an in-flight strip listing every currently running
turn (newest first, with a short prompt snippet, elapsed time and span
count) linking to its waterfall; the turn being viewed is marked. An in-flight turn silent for
over 10 minutes is flagged stale — probably a lost end mark, not a running
prompt.

Hermes drops `hermes.turn.end` marks often enough that an open turn is poor
evidence of work in progress, so "still running" (`Turn.is_live`) means open
*and* not proven finished. Two proofs override an open turn: a later turn
starting in the same session (`Turn.superseded` — a session runs one turn at
a time), and, for a subagent, the parent's `hermes.subagent.stop` mark. Both
beat any staleness clock, being exact and immediate. No `end_us` is invented
for such a turn: its duration was never observed, so the turn table and its
page show "no end mark" rather than a made-up figure. Only liveness is
affected — the turns stay in the table and keep their waterfall pages.

This matters for more than clutter: a turn that looks in flight forever also
freezes follow mode, since follow refuses to navigate away from a live turn
and such a turn never stops being live. A "follow new turns" toggle (persisted in localStorage) auto-opens
a turn's waterfall when a new turn starts, with two guards: it never
navigates away while you are watching a turn that is still in flight
(concurrent turns just appear in the strip for manual switching), and it
never follows a stale entry. With follow on, a finished turn's page keeps
polling slowly so the next turn start is noticed. On a turn page the toggle
sits at the right of the all/prev/next row — it is navigation too, just
automatic; on the index it stands alone above the turn table.
- `/event/<id>` and `/fragment/events` — pre-plugin URLs, redirect to their
  `/memory/` equivalents.

## Tests

```bash
uv run pytest
```

## Layout

- `app.py` — app shell: app factory `create_app(db_path, atof_path=None)`,
  plugin registration, tab list, root/legacy redirects, CLI entry
- `plugins/` — one module or package per view; each exposes a Flask
  blueprint `bp` (registered under `/<bp.name>/`) and a `TAB_LABEL`.
  Plugins reach each other only by link or by an accessor the owner
  publishes on `app.extensions` — never by opening another's data source
  (ADR 4): the memory plugin publishes `memory_prior_text` and serves the
  `/memory/search-event` redirect, and the timing tab uses both while
  holding no database handle of its own. The
  timing plugin is a package holding the full ATOF reader (ADR 2):
  `plugins/timing/tailer.py` (byte-offset incremental file read; records
  are split on `"\n"` alone — never `str.splitlines()`, which also breaks
  on U+0085/U+2028/U+2029, characters JSON leaves unescaped and hermes'
  assistant text really contains),
  `plugins/timing/atof_reader.py` (JSONL line → typed event, fail-soft) and
  `plugins/timing/assembler.py` (events → sessions → turns → waterfall,
  with overhead as the residual of turn duration minus llm and tool time)
- `templates/` — `base.html` (shared chrome + tab bar), `_item_nav.html`
  (the `item_nav` macro every detail page uses for its "← all X" link and
  muted « prev / next » steppers, so item-by-item navigation looks and
  reads the same in every plugin; a page needing more on that row — the
  timing turn page's follow toggle — passes it through `{% call %}`), plus
  one subdirectory per plugin (`memory/`, `timing/`)
- `tests/` — `conftest.py` (shared fixtures), `test_app.py` (shell),
  `test_memory.py`, `test_timing.py`, `test_atof_reader.py`,
  `test_assembler.py`, `test_tailer.py`
- `docs/adr/` — architecture decision records
