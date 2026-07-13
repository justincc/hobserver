# AGENTS.md

## Project
Flask webapp for browsing hermes-agent logs. Views are plugins rendered as
horizontal tabs: memory (`jmem0_logged.db`, the mem0 event log) and prompt
timing (NeMo Relay ATOF JSONL — stub until the reader lands). See README.md
for pages, layout, and data-source path resolution; see docs/adr/ for the
architecture decisions (ATOF as timing source, direct JSONL reading with no
ETL, blueprints-as-plugins).

- Run: `uv run python app.py [db_path]` (serves on port 5090; timing source
  via the `ATOF_LOG` env var)
- Test: `uv run pytest`
- Every view is read-only over a log produced by another process; the
  browser never owns or mutates data.
- `app.py` is the shell: app factory (`create_app(db_path, atof_path=None)`)
  so tests can point it at temporary sources, plugin registration, tab list,
  and legacy-URL redirects. Plugins live in `plugins/<name>.py`, each
  exposing a blueprint `bp` (registered under `/<bp.name>/`) and a
  `TAB_LABEL`; their templates live in `templates/<name>/`.
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
