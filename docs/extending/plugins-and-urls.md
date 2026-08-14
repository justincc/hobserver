# Plugins, tabs and URLs

## The shell

`app.py` is the shell: the app factory `create_app(tabs)`, tab registration,
the tab bar, and the root redirect. It imports no plugin and knows nothing
about any tab's data source. `tabs.py` reads the configuration and loads what
it names; `hermes_paths.py` is a helper the in-tree plugins use to default
their paths.

## The plugin contract

A plugin is any importable module exposing:

| name | required | what it is |
| --- | --- | --- |
| `PLUGIN_API` | yes | contract version the plugin was written against (`1`) |
| `bp` | yes | the Flask blueprint, registered under `/<URL_PREFIX>/` |
| `TAB_LABEL` | yes | UI copy — what the tab reads |
| `URL_PREFIX` | yes | the address; may be multi-segment |
| `init_app(app, settings)` | no | called once at registration |
| `sources(settings)` | no | what the tab reads, for the banner and error states |
| `SCOPES` / `SCOPES_BY_CATEGORY` | no | how this tab's own hermes spans render on a tab that paints spans (ADR 10) |
| `SPAN_READERS` | no | how this tab's own span payloads are read, named as sources by those specs (ADR 17) |

In-tree plugins live in `plugins/<name>/`, templates included:
`plugins/<name>/templates/<name>/`, carried by the blueprint's own
`template_folder` and still keyed by blueprint name. Nothing about the
contract depends on being in this tree, so an out-of-tree tab is the same
thing in a different directory — see
[writing-a-plugin.md](writing-a-plugin.md).

The app's own `templates/` holds only what the shell owns and every tab
shares: `base.html`, `_item_nav.html`, and the `unavailable.html` served in
place of a tab that could not load. It is searched before any blueprint's
folder, so a template of the app's own name wins — which is the escape hatch
for overriding a plugin's page without editing it.

Most tabs import nothing from this app; those that need to may import the
surfaces it publishes — `base.html`'s classes and conventions, and the
scope-spec vocabulary — but never another plugin
([ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md),
[ADR 4](../design/adr/0004-cross-plugin-access-by-link-or-published-accessor.md)).

`bp.name`, `TAB_LABEL` and `URL_PREFIX` are deliberately independent. The code
identifier (`url_for`, template directory) does not move; the label and the
prefix are free to change without touching a single `url_for` call. **Do not
collapse them back together** — that separation is what made renaming and
re-addressing the tabs cheap.

## The configuration

`hobserver.toml` lists the tabs, in tab order:

```toml
[[tabs]]
module = "plugins.turns"
settings = { atof_log = "$HERMES_HOME/nemo-relay/atof/hermes-atof.jsonl" }

[[tabs]]
module = "plugins.mem0"
enabled = false
```

- File order is tab order; the first enabled tab is where `/` lands.
- `enabled = false` turns a tab off in one line.
- `module` is any importable path, in this tree or installed elsewhere.
- `settings` is passed to the plugin untouched, with `~` and `$VARS` expanded.

The file is found via the first command-line argument, else `$HOBSERVER_CONFIG`,
else `./hobserver.toml`. If none exists the built-in default list is used, so a
fresh checkout runs with no setup.

## When a tab cannot load

A tab that will not import, is missing part of the contract, declares a
different `PLUGIN_API`, or reports an unusable **required** source is taken out
of service: the shell serves a 503 page naming the problem, the tab bar marks
it, and every other tab carries on. A tab whose source problem is *not* marked
required stays in service and explains itself — that is how the Turns tab
handles a missing ATOF log.

Two tabs claiming the same `URL_PREFIX` or blueprint name is fatal: the app
refuses to start and names both, because either could answer a request and
nothing in the output would say which did.

Only an empty tab bar — nothing loaded at all — exits.

## The tabs shipped here

| tab | blueprint | URL | source |
| --- | --- | --- | --- |
| Turns | `turns` | `/turns/` | NeMo Relay ATOF JSONL |
| Mem0 | `mem0` | `/memory/mem0/` | `jmem0_logged.db` |

Turns leads because a turn is the unit of activity — memory calls included —
where the mem0 log covers one tool.

## Naming URLs

Inside a tab, a page is named for the thing it shows:

| URL | shows |
| --- | --- |
| `/turns/` | every turn, newest first |
| `/turns/turn/<session>/<start µs>` | one turn's waterfall |
| `/turns/span/<span uuid>/<key>` | one whole value of one span — the `key` is a `Full` its scope declared ([ADR 12](../design/adr/0012-open-a-whole-value-on-its-own-page.md)) |
| `/memory/mem0/search-event` | one logged mem0 search |

A turn is addressed by the pair that identifies it in the assembly; a span by
its uuid alone, which is already on its row and reaches a span that landed in
no turn. Neither is a database id — nothing here has one — and both survive a
rebuilt index, which is what makes a link worth pasting somewhere.

Mem0 is named for the provider, not for memory in general, and namespaced under
`memory/` because it is one memory system of several to come: hermes' own
in-prompt stores (MEMORY.md / USER.md) and any other external provider tried
later get `/memory/<name>/` beside it rather than being folded in. `/memory/`
itself stays free to become an index over them.

`memory/mem0` rather than a punctuated single segment (`memory:mem0`) because
a colon in the first segment of a *relative* URL parses as a scheme.

Moving a URL is cheap and stays that way: no redirect from an old address is
kept, and none should be added. This app has one user, who adapts, so
compatibility routes are pure reading cost. (`/timing/`, `/memory/` and
`/prompts/` were all earlier prefixes; every one of them simply 404s now.)

## Crossing between plugins

Per ADR 4, a plugin may link to another's page, or call an accessor the other
publishes in `app.extensions`. It never opens another plugin's data source.

Both directions are in use today, mem0 → turns:

- `mem0_prior_text` (the accessor) — what a memory said before a change.
- `/memory/mem0/search-event` (the link) — a mem0_search span to its logged
  event.

Details of both are in [span-rendering.md](../design/span-rendering.md).
