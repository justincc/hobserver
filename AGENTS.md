# AGENTS.md

## General Instructions
- Keep this generation instructions section and subsections at the top of the file, except of a short description of the project if applicable.
- Document and code should follow the DRY (Don't Repeat Yourself) principle when reasonable.

### Testing
- All new functionality must have an accompanying passing unit test.
- Tests should always be run after making any changes and any fails fixed.

### Documentation
- Documentation must be kept up to date with relevant changes to the project, or when you discover significant things about the project which have not already been recorded. This includes but is not limited to build commands, architecture decisions, code conventions, debugging insights and workflow preferences.
- Keep the documentation organized and concise. 
- For this file, when a subject outgrows its bullet, split it into docs/<topic>.md and
  leave a single line here naming the file and what it covers. Prefer this
  to writing more compactly.
- Documentation is read by humans as well as agents. A passage that is
  accurate but unscannable is not sufficient: if a person cannot find one fact in
  it without reading the whole thing, it needs breaking up.

## Project

hermes-observer (formerly jmem0-logged-browser): a Flask webapp for observing
hermes-agent activity. Views are plugins shown as horizontal tabs — **Prompts**
(`/prompts/`, per-turn waterfalls from the NeMo Relay ATOF JSONL) and **Mem0**
(`/memory/mem0/`, the mem0 event log in `jmem0_logged.db`).

Every view is read-only over a log produced by another process. The browser
never owns or mutates data.

### Commands

- Run: `uv run python app.py` — no arguments needed when `HERMES_HOME` is
  exported. Serves on port 5090; template and `.py` edits are picked up
  without a restart.
- Test: `uv run pytest`

### Where things are written down

- [README.md](README.md) — pages, layout, data-source path resolution
- [docs/plugins-and-urls.md](docs/plugins-and-urls.md) — the plugin contract
  (`bp` / `TAB_LABEL` / `URL_PREFIX`), tab order, URL naming, crossing between
  plugins
- [docs/startup-and-console.md](docs/startup-and-console.md) — source
  resolution, the db check, the startup banner, console noise, `/_status`
- [docs/atof-reader.md](docs/atof-reader.md) — tailer → parser → assembler
- [docs/span-rendering.md](docs/span-rendering.md) — what each tool scope shows
  on the turn page, scope by scope
- [docs/live-pages.md](docs/live-pages.md) — polling, liveness, follow mode,
  item navigation, waterfall colors
- [docs/setup-prompt-timing.md](docs/setup-prompt-timing.md) — producer-side
  setup in hermes-agent
- [docs/adr/](docs/adr/) — architecture decisions, sequentially numbered

### Conventions

- Start simple and build functionality up as needed.
- Record an architecture decision as a new ADR, not as prose in this file.
- Check a hermes payload against the tool's signature in the hermes source
  (`$h/tools/`, and `$h/plugins/memory/mem0/` for the mem0 tools), not only
  against shapes seen in the ATOF log. Read payloads defensively regardless.
- The `platform` kwarg is `webui` today, but hermes has other frontends (a TUI,
  for one) — never assume webui.
- Identity is never carried by color alone; a color always has text beside it.
