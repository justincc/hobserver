# Changelog

All notable changes to hobserver are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the public surfaces (the plugin, scope-spec,
and provider-spec vocabularies) may still change between releases — see
`docs/design/design-principles.md`.

## [0.3.0] - 2026-08-19

First public release. Earlier `0.x` development happened before publication,
so the history here begins at 0.3.0.

### Added

- **Turns tab** — per-turn latency waterfalls read from the NeMo Relay ATOF
  JSONL stream: where each turn's time went (llm vs tool vs overhead), a span
  timeline per turn, and per-span detail drawn from each tool's payload. The
  whole request or response of any model call — including hermes' own
  background ones — opens on its own page.
- **Mem0 tab** — browses `jmem0_logged.db`, the mem0 event log, newest first,
  with per-event detail and recovery of what an updated or deleted memory said
  before the change.
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

[0.3.0]: https://github.com/justincc/hobserver/releases/tag/v0.3.0
