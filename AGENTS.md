# AGENTS.md

## General Instructions
- Keep this generation instructions section and its subsections at the top of the file.
- Document and code should follow the DRY (Don't Repeat Yourself) principle when reasonable.
- In code, docs, tests or examples, don't use locations or names particular to one machine or person.

### Testing
- All new functionality must have an accompanying passing unit test.
- Tests should always be run after making any changes and any fails fixed.

### Documentation
- Documentation must be kept up to date with relevant changes to the project, or when you discover significant things about the project which have not already been recorded. This includes but is not limited to build commands, architecture decisions, code conventions, debugging insights and workflow preferences.
- Keep the documentation organized and concise. Don't reproduce details that a user will see by running the system, or a developer by reading the code.
- Documentation is read by humans as well as agents. A passage that is
  accurate but unscannable is not sufficient: if a person cannot find one fact in
  it without reading the whole thing, it needs breaking up.
- For this file, when a subject outgrows its bullet, split it into docs/<topic>.md and
  leave a single line here naming the file and what it covers.

## Project

hermes-observer (formerly jmem0-logged-browser): a Flask webapp for observing
hermes-agent activity. Views are plugins shown as horizontal tabs — **Turns**
(`/turns/`, per-turn waterfalls from the NeMo Relay ATOF JSONL) and **Mem0**
(`/memory/mem0/`, the mem0 event log in `jmem0_logged.db`).

Every view is read-only over a log produced by another process. The browser
never owns or mutates data.

### Commands

- Run: `uv run python app.py [observer.toml]` — no arguments needed when
  `HERMES_HOME` is exported. Serves on port 5090, on waitress (ADR 14).
  Template edits are picked up without a restart; add `--dev` for `.py` edits,
  which wraps that same server in the Werkzeug reloader.
- Test: `uv run pytest` — collects both roots, the shell's `tests/` and each
  plugin's `plugins/<name>/tests/`. `uv run pytest plugins/mem0` runs one
  plugin's alone.
- Which tabs are served, in which order: `observer.toml`. `enabled = false`
  turns one off; `module` is any importable path, in this tree or installed
  elsewhere.

### Where things are written down

`docs/` is split by who reads it. Put a new document in the directory whose
audience it serves, not the one whose subject it shares.

**`docs/design/`** — why this app is shaped as it is. For anyone changing it.

- [design-principles.md](docs/design/design-principles.md) — standing
  commitments, extensibility above all; **read before designing a change**
- [adr/](docs/design/adr/) — architecture decisions, sequentially numbered
- [atof-reader.md](docs/design/atof-reader.md) — line reader → parser → index
  → assembler; what the index stores, its staleness checks, and hydration
- [providers.py](plugins/turns/providers.py) + [ADR 13](docs/design/adr/0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md)
  — where the assistant's words, tool calls and token counts sit per
  provider. **Nothing else in the app knows one provider from another**;
  put new provider knowledge there, not in the reader
- [span-rendering.md](docs/design/span-rendering.md) — what each tool scope
  shows on the turn page, scope by scope
- [live-pages.md](docs/design/live-pages.md) — polling, liveness, follow mode,
  item navigation, waterfall colors

**`docs/extending/`** — writing against this app from outside it. For plugin
and scope-spec authors, and the record of what is published to them.

- [writing-a-plugin.md](docs/extending/writing-a-plugin.md) — how to add a
  tab, with a whole worked plugin; the contract in full
- [writing-a-scope-spec.md](docs/extending/writing-a-scope-spec.md) — how to
  make the Turns tab display your own hermes tool
- [writing-a-provider-spec.md](docs/extending/writing-a-provider-spec.md) —
  how to make the Turns tab read your own router's token counts
- [plugins-and-urls.md](docs/extending/plugins-and-urls.md) — the plugin
  contract, the config file, what happens when a tab cannot load, URL naming,
  crossing between plugins

**`docs/running/`** — operating the observer. For someone with it installed
and no intention of writing code against it.

- [setup-prompt-timing.md](docs/running/setup-prompt-timing.md) —
  producer-side setup in hermes-agent, so there is a log to read at all
- [startup-and-console.md](docs/running/startup-and-console.md) — source
  resolution, the db check, the startup banner, console noise, `/_status`

- [README.md](README.md) — pages, layout, data-source path resolution

### Conventions

- Start simple and build functionality up as needed.
- Before designing a change, meet the standing commitments in
  [docs/design/design-principles.md](docs/design/design-principles.md): extensibility
  without a fork, how to read a hermes payload, and what may carry identity
  on screen. That file is the record for all three — shape is expensive to
  retrofit, and the payload rules are how this app avoids reading the log as
  if it were a contract.
- Record an architecture decision as a new ADR, not as prose in this file.
