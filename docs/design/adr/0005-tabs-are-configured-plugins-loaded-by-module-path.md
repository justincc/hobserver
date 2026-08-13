# 5. Tabs are configured plugins, loaded by module path

Date: 2026-07-28

## Status

Accepted

Extends [ADR 3](0003-plugin-views-as-flask-blueprints-with-tabs.md).

## Context

[ADR 3](0003-plugin-views-as-flask-blueprints-with-tabs.md) made each view a
blueprint registered by the shell, and the contract has since grown to three
module attributes — `bp`, `TAB_LABEL`, `URL_PREFIX`. Adding a tab that needs
no data source of its own is already easy: write the module, add it to the
`PLUGINS` tuple.

A tab that reads something is not. The shell knows both existing sources by
name: it imports `check_db` from the memory plugin, sets `DB_PATH` and
`ATOF_PATH` itself, takes them as `create_app` parameters, and enumerates them
in `resolve_sources` and the startup banner. A third tab with its own source
means editing five places in `app.py`, and the file grows a per-plugin
parameter each time. The plugin boundary is real for URLs and templates and
absent for configuration.

Two further wants, neither served today:

- **Turning a tab off** should be one edit, and today it means deleting a line
  of Python from a tuple.
- **Other people adding tabs** without forking this repo. The intent is that
  someone can install hobserver, install or write their own tab —
  another memory provider, a different log — and run both, with no patch
  carried on top of this tree.

The last one rules out a registry that only names in-tree modules, and raises
the question of `importlib.metadata` entry points, the classic Python plugin
mechanism. Entry points answer discovery only. They cannot express order (a
tab bar is ordered, and the first tab serves `/`) and cannot express disable
(installed is loaded), so a configuration file is needed regardless. Adding
entry points on top of it would mean two places to look when a tab does not
appear.

Loading strangers' code also changes what a failure means. Today a bad memory
db exits the process, which is defensible when the app is two tabs the author
wrote and a bad path is almost certainly a typo in the command line. It is not
defensible when a third-party tab is misconfigured and takes the rest of the
app down with it.

## Decision

**Tabs are declared in a TOML config file, and loaded by module path.**

*(Naming note — the one place the tabs' names are tracked, so that an ADR
written under an older one need not be rewritten to stay true. Two renames
have happened since:*

- *right after this ADR, `plugins.timing` and `plugins.memory` became
  `plugins.prompts` and `plugins.mem0`, once the module name became something
  a reader meets in the config file;*
- *on 2026-08-07, the prompts tab became **Turns** — it lists turns, its
  detail page is one turn, and every other tab is named for what it shows
  rather than for what a reader typed.*

*Blueprint names, template directories, packages and endpoints moved with the
label both times. So `memory.index` and `timing.index` in ADRs 3 and 4 are
`mem0.index` and `turns.index` today, and `/timing/`, `/memory/` and
`/prompts/` were all earlier URL prefixes that now simply 404. Later ADRs
have had their file paths, endpoints and URLs updated in place — they are
navigation, not the decision — while what each ADR decided is left as
written. The mechanism below is unchanged; the example module names are as
they were.)*

```toml
[[tabs]]
module = "plugins.timing"
settings = { atof_log = "…" }

[[tabs]]
module = "plugins.memory"
enabled = false
settings = { db = "…" }
```

- **Order in the file is tab order**, and the first enabled tab serves `/`.
- **`enabled = false` is the off switch** — one line, no code edit.
- **`module` is any importable path.** `plugins.timing` (in this tree) and
  `hobserver_zep` (installed from elsewhere) load by exactly the same
  mechanism, so an out-of-tree tab needs no fork and no change here.
- **`settings` is an opaque table** passed to the plugin. The shell never
  interprets it; a plugin validates its own and supplies its own defaults.

TOML because config edited by other people should not be executable, and
`tomllib` is stdlib. This raises the floor to Python 3.11.

### The plugin contract

Required module attributes, unchanged from today plus a version marker:

| attribute | meaning |
| --- | --- |
| `PLUGIN_API` | contract version the plugin was written against |
| `bp` | Flask blueprint, registered under `/<URL_PREFIX>/` |
| `TAB_LABEL` | what the tab reads |
| `URL_PREFIX` | address, may be multi-segment |

Optional hooks, each absent meaning "nothing to do":

| hook | meaning |
| --- | --- |
| `init_app(app, settings)` | stash configuration, publish accessors |
| `sources(settings)` | the files or stores this tab reads, and whether each is usable, for the startup banner and the tab's own error state |

**A plugin imports nothing from the host.** The contract is plain module
attributes and Flask, so a third-party tab does not depend on a
hobserver package or track its version. The one real coupling is
`templates/base.html` — a tab extends it and inherits the tab bar, the
`data-live-poll` convention and the CSS classes — so that surface is
documented as public and changed with the same care as a URL.

*(Relaxed by [ADR 8](0008-plugins-may-import-published-host-vocabulary.md):
a plugin may import the surfaces this app publishes, `base.html` and the
scope-spec vocabulary among them. The prohibition above described the tab
contract, which needs no import, and generalised it too far — the coupling
it named in its own last sentence was always there. What survives unchanged
is that a plugin never imports another plugin.)*

### Failure is per-tab, except for collisions

- A tab that fails to import, fails to register, or reports an unusable
  source **does not stop the app**. It is dropped from the bar or shown with
  its problem, the reason is printed at startup, and every other tab serves.
- **Two tabs claiming the same `URL_PREFIX` or blueprint name is fatal.** The
  app refuses to start and names both. This is the one case where continuing
  would silently serve the wrong thing.
- Only an empty tab bar — no tab loaded at all — is worth exiting for.

## Consequences

- **The Mem0 tab stops exiting the process.** An unusable `jmem0_logged.db`
  currently ends `main` with a message; it becomes a tab-level problem, the
  same way a missing ATOF log is already the Prompts tab's own business. The
  diagnosis that motivated `check_db` (a directory passed as a path, sqlite
  reporting `disk I/O error` per request) is unchanged and still runs — only
  the response to it moves, from `sys.exit` to a tab that says what is wrong.
  Startup must therefore say it loudly enough that a stray argument is still
  obvious.
- **`create_app` stops taking per-plugin paths.** It takes the parsed tab
  list, and tests build one inline instead of passing `db_path` and
  `atof_path`. Test fixtures change shape; what they assert does not.
- **The shell stops importing any plugin.** `from plugins.memory import
  check_db` goes, and with it the shell's knowledge of what a memory db is.
  The startup banner prints whatever the loaded tabs report through
  `sources`, so it grows a line when a tab is added and needs no edit.
- **`PLUGINS` as a static tuple goes away.** Adding a tab to this repo becomes
  a directory plus a config block, the same two steps a stranger takes.
- **The config file becomes a supported interface**, versioned by
  `PLUGIN_API`, and a mistake in it is now a class of startup failure that did
  not exist. Errors name the offending `[[tabs]]` entry.
- **Entry points stay available and stay unused.** If auto-discovery is ever
  wanted, it supplies additional candidate modules while the config keeps
  governing order and enable. No plugin written against this contract would
  need changing.
- **ADR 4 is unaffected.** Cross-plugin access remains a link or a published
  accessor, and a plugin still never opens another's source. Third-party tabs
  make the rule matter more, not less: an accessor absent because its plugin
  is disabled is a state every caller already has to handle.
