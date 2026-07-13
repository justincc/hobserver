# AGENTS.md

## Project
hermes-observer (formerly jmem0-logged-browser): Flask webapp for observing
hermes-agent activity. Views are plugins rendered as horizontal tabs: memory (`jmem0_logged.db`, the mem0 event log) and prompt
timing (per-turn waterfalls from the NeMo Relay ATOF JSONL). See README.md
for pages, layout, and data-source path resolution; see docs/adr/ for the
architecture decisions (ATOF as timing source, direct JSONL reading with no
ETL, blueprints-as-plugins).

- Run: `uv run python app.py [db_path]` (serves on port 5090; timing source
  via the `ATOF_LOG` env var). Producer-side setup (nemo-relay install,
  plugin enable, `HERMES_NEMO_RELAY_*` in `~/.hermes/.env`) is documented
  in docs/setup-prompt-timing.md.
- Test: `uv run pytest`
- Every view is read-only over a log produced by another process; the
  browser never owns or mutates data.
- `app.py` is the shell: app factory (`create_app(db_path, atof_path=None)`)
  so tests can point it at temporary sources, plugin registration, tab list,
  and legacy-URL redirects. Plugins live in `plugins/<name>` (module or
  package), each exposing a blueprint `bp` (registered under `/<bp.name>/`)
  and a `TAB_LABEL`; their templates live in `templates/<name>/`. The ATOF
  reader is three layers in `plugins/timing/`: `tailer.py` (byte-offset
  incremental read; app-lifetime instance in `app.extensions`),
  `atof_reader.py` (parser; fixtures include the verbatim example lines
  from the ATOF v0.1 spec) and `assembler.py` (span pairing by uuid, turns
  bounded by hermes.turn.start/end marks, span→turn assignment by turn_id
  with a timestamp-containment fallback, session via metadata or
  parent_uuid). Waterfall series colors were validated with the dataviz
  six-checks palette validator against the light surface: llm `#2a78d6`,
  tool `#eb6834`, other `#4a3aa7`; span identity is always also in text,
  never color alone.
- Workflow preference: start simple and build functionality up as needed.
- Architecture decisions are recorded as ADRs in docs/adr/ (sequentially
  numbered markdown files).

## General Instructions
- Documentation must be kept up to date with any relevant changes to the project. 
- Keep documentation organized and concise. A new file in docs/ can be split off and referenced from here when necessary.
- All new functionality must have an accompanying passing unit test.
- Document and code should follow the DRY (Don't Repeat Yourself) principle when reasonable.
- Tests should always be run after making any changes and any fails fixed.

## Documentation
- When you learn something important about this project (build commands, architecture decisions, code conventions, debugging insights, workflow preferences. etc), update this file and other documentation to record it.
