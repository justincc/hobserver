# AGENTS.md

## Project
Flask webapp for browsing `jmem0_logged.db` (hermes-agent mem0 event log).
See README.md for pages, layout, and database path resolution.

- Run: `uv run python app.py [db_path]` (serves on port 5090)
- Test: `uv run pytest`
- The database is always opened read-only.
- `app.py` uses an app factory (`create_app(db_path)`) so tests can point it
  at a temporary database.
- Workflow preference: start simple and build functionality up as needed.

## General Instructions
- Documentation must be kept up to date with any relevant changes to the project. 
- Keep documentation organized and concise. A new file in docs/ can be split off and referenced from here when necessary.
- All new functionality must have an accompanying passing unit test.
- Document and code should follow the DRY (Don't Repeat Yourself) principle when reasonable.
- Tests should always be run after making any changes and any fails fixed.

## Documentation
- When you learn something important about this project (build commands, architecture decisions, code conventions, debugging insights, workflow preferences. etc), update this file and other documentation to record it.
