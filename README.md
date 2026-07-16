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

The memory database path is resolved in this order:

1. First command-line argument
2. `JMEM0_DB` environment variable
3. Built-in default: the hermes-agent `product/src/config/jmem0_logged.db`

The prompt-timing source is the `ATOF_LOG` environment variable, pointing
at the events JSONL file written by the nemo_relay plugin's ATOF exporter.
If unset, the Prompt timing tab says so rather than showing an empty page.
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
  context messages rendered as plaintext. Results that are JSON (e.g.
  `mem0_search` output) are pretty-printed; others (e.g. prefetch
  markdown) are shown as-is. The context messages are the up
  to 10 preceding prefetch queries logged for the same session (tool-call
  events are excluded — they are not user messages) — an approximation
  of the extra conversational context mem0 uses during retrieval — each
  prefixed with its event id, oldest first.
- `/timing/` — all turns, newest first: start time, session, a one-line
  prompt snippet (from the turn-start mark's `user_message`; ellipsized so
  long prompts never widen the table, em dash when absent), total / llm /
  tool / overhead durations (overhead is the residual), model-call and span
  counts. In-flight turns are marked. Parse errors and assembly anomalies
  are shown above the table, never dropped. The page updates itself every
  3 s (the tailer reads only what the exporter appended since the last
  request), so new turns appear without a manual reload.
- `/timing/turn/<session>/<start_us>` — one turn: the in-flight strip,
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
  to `~`, both ellipsized with the full text in the title attribute;
  skill scopes (`skill_view`/`skill_manage`) likewise show the skill's
  `name`, the `file_path` within the skill when one is targeted, and (for
  skill_manage) the `action` inline; file tool scopes (patch, read_file,
  write_file, …) show their `path`, ellipsized from the
  left so the end of a long path stays visible; search_files scopes show
  the `pattern` (monospace), `file_glob` and search `path`; web_search
  and mem0_search scopes show the `query` (monospace); web_extract scopes show the first
  of their `urls` plus a "+N more" count, with the full list in the
  title attribute; execute_code scopes show the first line of their
  `code`, with the full program in the title attribute; mem0_add scopes
  show the remembered `content` (plain text, not monospace). By default this extra info trails the span name on a
  single ellipsized line so every row stays one line tall; a "details"
  slider switch on the right of the legend row — off on every page load,
  never persisted — expands it onto its own line under the name and
  reveals the span uuids. Each
  span line carries the span's ATOF uuid — the key for finding its
  start/end lines in the raw JSONL — in small muted monospace right after
  the name, so the span name stays prominent (other correlation ids like
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
prompt. A "follow new turns" toggle (persisted in localStorage) auto-opens
a turn's waterfall when a new turn starts, with two guards: it never
navigates away while you are watching a turn that is still in flight
(concurrent turns just appear in the strip for manual switching), and it
never follows a stale entry. With follow on, a finished turn's page keeps
polling slowly so the next turn start is noticed.
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
  blueprint `bp` (registered under `/<bp.name>/`) and a `TAB_LABEL`. The
  timing plugin is a package holding the full ATOF reader (ADR 2):
  `plugins/timing/tailer.py` (byte-offset incremental file read),
  `plugins/timing/atof_reader.py` (JSONL line → typed event, fail-soft) and
  `plugins/timing/assembler.py` (events → sessions → turns → waterfall,
  with overhead as the residual of turn duration minus llm and tool time)
- `templates/` — `base.html` (shared chrome + tab bar) plus one
  subdirectory per plugin (`memory/`, `timing/`)
- `tests/` — `conftest.py` (shared fixtures), `test_app.py` (shell),
  `test_memory.py`, `test_timing.py`, `test_atof_reader.py`,
  `test_assembler.py`, `test_tailer.py`
- `docs/adr/` — architecture decision records
