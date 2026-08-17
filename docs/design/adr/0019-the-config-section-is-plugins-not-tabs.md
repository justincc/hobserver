# 19. The config section is `[[plugins]]`, not `[[tabs]]`

Date: 2026-08-17

## Status

Accepted

Follows [ADR 18](0018-configure-a-tab-by-the-plugin-key.md).

## Context

[ADR 18](0018-configure-a-tab-by-the-plugin-key.md) renamed the per-entry key
to `plugin`. That left the array-of-tables still called `[[tabs]]`:

    [[tabs]]
    plugin = "plugins.turns"

The config surface is where a user declares what to load, and what they load is
a plugin — the `plugins/` directory, `PLUGIN_API`, `writing-a-plugin.md`. The
section heading was the last place the config spoke of "tabs" instead.

## Decision

Rename the section to `[[plugins]]`:

    [[plugins]]
    plugin = "plugins.turns"

Each entry reads as a singular `plugin`, so `[[plugins]]` with a `plugin` line
inside is a plural-of-singulars, not a redundancy.

**This is the whole of the change: the config surface, and nothing behind it.**
The one functional edit is `data.get("tabs")` → `data.get("plugins")` in
`tabs.py`; the rest is the config text a user sees (the file, the example, the
`ConfigError` messages, the docs).

Deliberately *not* renamed: everything internal that models the **tab** — the
`Tab` and `TabSpec` classes, `load_tabs`, `TAB_LABEL`, the tab bar, the
`tabs.py` filename. A **plugin** is what you configure and the shell imports; a
**tab** is the displayed unit the shell builds from it. `tabs.py`'s public
product is a `list[Tab]`, and a plugin author still sets `TAB_LABEL` for the
text in the bar. The two words name two layers, so both survive — one at the
config surface, one in the code and UI.

## Consequences

- A breaking change to the config format, on top of ADR 18: an existing
  `hobserver.toml` renames `[[tabs]]` to `[[plugins]]`. No shim — single-user
  tool. The tracked `hobserver.example.toml` already does.
- The config vocabulary is now uniformly "plugin"; "tab" survives only where a
  displayed tab is meant. A reader who meets `[[plugins]]` in the config and
  `Tab` in the code is seeing the layer boundary, not an inconsistency.
- `tabs.py` keeps its name: it is the tabs subsystem, and loads plugins because
  a tab is a loaded plugin (the file-rename question, and why the obvious
  `plugins.py` collides with the `plugins/` package, was weighed and declined).
