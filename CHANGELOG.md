# Changelog

All notable changes to hobserver are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the public surfaces (the plugin, scope-spec,
and provider-spec vocabularies) may still change between releases — see
`docs/design/design-principles.md`.

## [Unreleased]

### Added

- **A skill page shows where the skill came from.** A Summary box, boxed like
  the full prompt/response page's, labels the skill's origin — bundled with
  hermes, hub/URL-installed, agent-created, an external (user-configured) dir,
  or user-added/manually authored — with its creation and last-modified dates
  and any usage the record carries. Origin is read from hermes' own provenance
  sidecars (`.bundled_manifest`, `.hub/lock.json`, `.usage.json`), best-effort:
  an unreadable or unexpected file yields an "Unknown" origin rather than an
  error. hermes marks agent authorship only for its autonomous curator, so a
  skill you asked hermes to write shows as user-added — the box says so. See
  ADR 23 and SECURITY.md.

## [0.3.3] - 2026-08-27

### Added

- **An llm span shows the reasoning effort it was asked for.** A `reasoning`
  row under `response` names the request's `reasoning.effort` — `low`, `medium`
  or `high` on the codex route — the one llm row read from the call's start
  payload rather than its response. A request that named no reasoning level has
  no row: the value is shown as sent, never as an inferred level. See
  docs/design/span-rendering.md.
- **The full prompt/response page now has a two-panel header.** A whole-value
  page (ADR 12) leads with a warm-shaded **Full prompt** / **Full response**
  heading for the value itself (its size and provenance), then a blue
  **Summary** box for the call — its identity and the same detail strip the
  turn row shows (finish_reason, reasoning effort, token tree, tool calls),
  from the same `llm_rows` macro so the two can never drift. See
  docs/design/span-rendering.md.
- **The turns index source line reports the log's size and entry count.** A
  column on the source line shows how large the ATOF log is and how many
  entries it holds.

### Changed

- **The turns source line is laid out as a keys-over-values table** and now
  sits above the assembly-anomalies boxout; the "updates automatically" note
  has been dropped from the turns index.
- **The prompt-timing setup docs now cover hermes' NeMo Relay native-plugin
  cutover** (shipped in Hermes Agent v0.20.5 / v2026.8.19): the ATOF exporter
  is configured by a Relay `plugins.toml` selected with
  `HERMES_NEMO_RELAY_PLUGINS_TOML`, and the old `HERMES_NEMO_RELAY_ATOF_*` env
  vars are ignored. The pre-v0.20.5 bundled-plugin method is kept for older
  hermes. See docs/running/setup-prompt-timing.md.

### Fixed

- **Category-qualified and absolute skill names now resolve** for the "view
  skill" link. A category-qualified name (a skill's path relative to its root,
  e.g. `finance/crypto-analysis`) and an absolute path handed in as a name
  both used to 404 or land on a same-named skill elsewhere; the resolver now
  matches the root-relative path (preferred over the basename) and treats an
  absolute name as a path resolved by containment. See ADR 22.

## [0.3.2] - 2026-08-20

### Added

- **View a skill from its turn.** A `skill_view` / `skill_manage` row now
  carries a "view skill" link that opens the skill on disk — its SKILL.md
  rendered as markdown, with the skill's files listed down the left. Reading is
  confined to the configured skill roots (hermes' `config.yaml`
  `skills.external_dirs` and the standard hermes skill dirs, or a `skill_roots`
  override in `hobserver.toml`), so a path from the log can never reach a file
  outside them. See ADR 22 and SECURITY.md.

## [0.3.1] - 2026-08-20

### Fixed

- Follow mode no longer 404s when it opens a turn mid-race. A turn's `start_us`
  can be revised earlier once its scope and prompt mark are both read, so a URL
  captured before that named a start no turn still carried. The turn route now
  falls back to the turn whose interval holds that instant and redirects to its
  live key.

### Changed

- The "switch to new turns" follow toggle now defaults off — a page stays put
  unless the reader asks it to chase new turns.
- The follow toggle is now remembered per browser tab (sessionStorage) rather
  than across the whole browser (localStorage), so one tab can sit pinned to a
  turn while another follows new ones.
- Console lines printed after the startup banner (the ATOF index report and
  the like) now carry a `[HH:MM:SS]` clock, the same one the error lines
  already have.

## [0.3.0] - 2026-08-19

First public release. Earlier `0.x` development happened before publication,
so the history here begins at 0.3.0.

### Added

- **Turns tab** — per-turn latency waterfalls read from the NeMo Relay ATOF
  JSONL stream: where each turn's time went (llm vs tool vs overhead), a span
  timeline per turn, and per-span detail drawn from each tool's payload. The
  whole request or response of any model call — including hermes' own
  background ones — opens on its own page.
- **Mem0 tab** — browses `jmem0_logged.db`, the SQLite database of mem0 events,
  newest first, with per-event detail and recovery of what an updated or deleted
  memory said before the change.
- **Plugin architecture** — every view is a plugin loaded by module path from
  `hobserver.toml`, so a new tab (another memory system, another log) can live
  in a separate tree with no fork of this repo.
- **Read-only by construction** — every view reads a log another process
  produces; the browser never owns or mutates data, so it is safe to point at
  live files while hermes is writing them.
- **Live pages** — timing views self-update by polling, mark in-flight work,
  and offer a follow mode that opens new turns as they start.
- **Provider-neutral token reading** — LLM provider token shapes live in one
  module, so a new router is added without patching the reader.
- **Runs from a fresh checkout** — `./hobserver` builds its environment through
  `uv` on first use and resolves its own sources under `$HERMES_HOME`, so no
  setup step is required.

### Security

- No authentication by design: binds loopback by default and is meant to run
  only where trusted parties can reach it. Log content is HTML-escaped and
  rendered without raw HTML. See `SECURITY.md` for the full trust model.

[0.3.2]: https://github.com/justincc/hobserver/releases/tag/v0.3.2
[0.3.1]: https://github.com/justincc/hobserver/releases/tag/v0.3.1
[0.3.0]: https://github.com/justincc/hobserver/releases/tag/v0.3.0
