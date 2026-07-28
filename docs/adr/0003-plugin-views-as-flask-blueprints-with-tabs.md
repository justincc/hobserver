# 3. Plugin views as Flask blueprints, presented as horizontal tabs

Date: 2026-07-13

## Status

Accepted

Implements the restructuring anticipated in
[ADR 1](0001-consume-nemo-relay-atof-for-prompt-timing.md) and
[ADR 2](0002-read-atof-jsonl-directly-no-etl.md).

## Context

The browser started as a single-view app over `jmem0_logged.db`. ADRs 1–2
add a second view (prompt timing over the ATOF stream), so the repository
needs a structure where views are independent plugins. There is a vague
longer-term idea that memory-access information and prompt timing might be
combined in one display, but it is not yet known whether that is feasible or
useful — so the UI should start with the simplest separation that keeps the
option open.

The repository's stated workflow preference is to start simple and build up
as needed.

## Decision

A plugin is a module in `plugins/` exposing a Flask blueprint named `bp`
and a `TAB_LABEL` string. The app shell (`app.py`) registers each plugin's
blueprint under `/<bp.name>/`, injects the tab list into all templates, and
renders one horizontal tab per plugin in `base.html`, with the active tab
derived from `request.blueprint`. Registration is a static `PLUGINS` tuple
in `plugins/__init__.py` — no dynamic discovery until plugins multiply
enough to warrant it.

Conventions:

- Each plugin owns a read-only view over a data source produced by another
  process (ADR 2): memory reads `jmem0_logged.db` (`DB_PATH`), timing reads
  the ATOF JSONL (`ATOF_PATH`, from the `ATOF_LOG` environment variable).
  Data-source paths live in the shell's app config.
- Each plugin's templates live in `templates/<name>/`; shared chrome and
  styling stay in `templates/base.html`.
- Each plugin's `/` route is its tab landing page, endpoint `<name>.index`.
- The shell keeps pre-plugin URLs working via redirects (`/` → first tab,
  `/event/<id>` and `/fragment/events` → their `/memory/` equivalents).
  *(No longer true: the compatibility redirects were dropped later, along
  with the prefixes themselves — a plugin is now served under its own
  `URL_PREFIX`, `/prompts/` and `/memory/mem0/` today. With one user there
  is nothing to keep working. Only `/` → first tab remains.)*

The UI starts as separate tabs, one per plugin. A combined
memory-plus-timing display, if it proves feasible, would arrive as its own
plugin (a third tab reading both sources) rather than by merging the
existing two.

## Consequences

- The mem0 view's behaviour is unchanged but its URLs move under
  `/memory/`; old bookmarks and open pages are handled by redirects.
- Cross-plugin display experiments do not destabilise existing views: a
  combined view is additive, and plugins share nothing but `base.html` and
  the app config.
- The page title becomes the generic "hermes agent log browser"; the
  repository name (`jmem0-logged-browser`) now understates its scope, and a
  rename can be decided separately without code consequences.
- Blueprint endpoints are namespaced (`memory.index`, `timing.index`), so
  templates must use qualified `url_for` names.
