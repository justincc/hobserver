# jmem0-logged-browser

A small Flask webapp for browsing `jmem0_logged.db`, the SQLite event log
produced by the hermes-agent mem0 logging wrapper.

## Running

```bash
uv run python app.py [path/to/jmem0_logged.db]
```

Then open http://127.0.0.1:5090.

The database path is resolved in this order:

1. First command-line argument
2. `JMEM0_DB` environment variable
3. Built-in default: the hermes-agent `product/src/config/jmem0_logged.db`

The database is opened read-only, so it is safe to point at the live log
while hermes is writing to it.

## Pages

- `/` — all events, newest first, with a truncated query; click an id or
  query to open the event.
- `/event/<id>` — one event: all fields except query/result shown as a
  metadata block at the top, then the full query, the result, and the
  context messages rendered as plaintext. The context messages are the up
  to 10 preceding queries logged for the same session — an approximation
  of the extra conversational context mem0 uses during retrieval — each
  prefixed with its event id, oldest first.

## Tests

```bash
uv run pytest
```

## Layout

- `app.py` — the whole app (app factory `create_app(db_path)` plus CLI entry)
- `templates/` — Jinja templates (`base.html`, `index.html`, `event.html`)
- `tests/test_app.py` — route tests against a temporary database
