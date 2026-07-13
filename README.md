# hermes-observer

A small Flask webapp for observing hermes-agent activity (formerly
jmem0-logged-browser). Views are plugins, shown as horizontal tabs:

- **Memory** — browses `jmem0_logged.db`, the SQLite event log produced by
  the hermes-agent mem0 logging wrapper.
- **Prompt timing** — per-turn latency waterfalls from the NeMo Relay ATOF
  JSONL stream exported by the hermes-agent `observability/nemo_relay`
  plugin. Currently a stub; see `docs/adr/` for the design.

Every view is read-only over a log another process produces, so it is safe
to point at live files while hermes is writing to them.

## Running

```bash
uv run python app.py [path/to/jmem0_logged.db]
```

Then open http://127.0.0.1:5090. The server binds to `0.0.0.0`, so it is
also reachable from other machines on the local network at
`http://<host-ip>:5090`.

The memory database path is resolved in this order:

1. First command-line argument
2. `JMEM0_DB` environment variable
3. Built-in default: the hermes-agent `product/src/config/jmem0_logged.db`

The prompt-timing source is the `ATOF_LOG` environment variable, pointing
at the events JSONL file written by the nemo_relay plugin's ATOF exporter.
If unset, the Prompt timing tab says so rather than showing an empty page.

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
- `/timing/` — the prompt-timing view (stub until the ATOF reader lands).
- `/event/<id>` and `/fragment/events` — pre-plugin URLs, redirect to their
  `/memory/` equivalents.

## Tests

```bash
uv run pytest
```

## Layout

- `app.py` — app shell: app factory `create_app(db_path, atof_path=None)`,
  plugin registration, tab list, root/legacy redirects, CLI entry
- `plugins/` — one module per view; each exposes a Flask blueprint `bp`
  (registered under `/<bp.name>/`) and a `TAB_LABEL`
- `templates/` — `base.html` (shared chrome + tab bar) plus one
  subdirectory per plugin (`memory/`, `timing/`)
- `tests/` — `conftest.py` (shared fixtures), `test_app.py` (shell),
  `test_memory.py`, `test_timing.py`
- `docs/adr/` — architecture decision records
