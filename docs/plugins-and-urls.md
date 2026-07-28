# Plugins, tabs and URLs

## The shell

`app.py` is the shell: the app factory `create_app(db_path, atof_path=None)`
(so tests can point it at temporary sources), plugin registration, the tab
list, and the root redirect.

## The plugin contract

A plugin is a module or package in `plugins/<name>` exposing:

| name | what it is |
| --- | --- |
| `bp` | the Flask blueprint, registered under `/<URL_PREFIX>/` |
| `TAB_LABEL` | UI copy — what the tab reads |
| `URL_PREFIX` | the address; may be multi-segment |

Templates live in `templates/<name>/`, keyed by blueprint name.

These three names are deliberately independent. `bp.name` is the code
identifier (`url_for`, template directory) and does not move; the label and the
prefix are free to change without touching a single `url_for` call. **Do not
collapse them back together** — that separation is what made renaming and
re-addressing the tabs cheap.

## The tabs

`plugins.PLUGINS` order is tab order, left to right, and its first entry is
where `/` lands.

| tab | blueprint | URL | source |
| --- | --- | --- | --- |
| Prompts | `timing` | `/prompts/` | NeMo Relay ATOF JSONL |
| Mem0 | `memory` | `/memory/mem0/` | `jmem0_logged.db` |

Prompts leads because a turn is the unit of activity — memory calls included —
where the mem0 log covers one tool.

## Naming URLs

Mem0 is named for the provider, not for memory in general, and namespaced under
`memory/` because it is one memory system of several to come: hermes' own
in-prompt stores (MEMORY.md / USER.md) and any other external provider tried
later get `/memory/<name>/` beside it rather than being folded in. `/memory/`
itself stays free to become an index over them.

`memory/mem0` rather than a punctuated single segment (`memory:mem0`) because
a colon in the first segment of a *relative* URL parses as a scheme.

Moving a URL is cheap and stays that way: no redirect from an old address is
kept, and none should be added. This app has one user, who adapts, so
compatibility routes are pure reading cost. (`/timing/` and `/memory/` were the
earlier prefixes; both simply 404 now.)

## Crossing between plugins

Per ADR 4, a plugin may link to another's page, or call an accessor the other
publishes in `app.extensions`. It never opens another plugin's data source.

Both directions are in use today, memory → timing:

- `memory_prior_text` (the accessor) — what a memory said before a change.
- `/memory/mem0/search-event` (the link) — a mem0_search span to its logged
  event.

Details of both are in [span-rendering.md](span-rendering.md).
