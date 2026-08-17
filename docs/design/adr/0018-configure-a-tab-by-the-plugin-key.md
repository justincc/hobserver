# 18. Configure a tab by the `plugin` key

Date: 2026-08-17

## Status

Accepted

Amends [ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md).

## Context

[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md) declared
that a tab is configured by an importable path and loaded by importing it. It
named the per-tab config key `module`:

    [[tabs]]
    module = "plugins.turns"

Everything the app calls a tab is a *plugin* — the directory is `plugins/`, the
contract constant is `PLUGIN_API`, the docs are `writing-a-plugin.md`. Against
that, `module` was the one place the config asked the reader to hold a second
word for the same thing, describing *how* the value is loaded (a Python module)
rather than *what* it is (the plugin to serve).

## Decision

Rename the key to `plugin`:

    [[tabs]]
    plugin = "plugins.turns"

The value is unchanged: still an importable dotted path, still loaded by
`importlib.import_module`, in-tree and installed alike. Only the label the user
types changes, `module` → `plugin`.

The word `module` stays everywhere it names an actual Python module — the
imported object, `module_name`, "a plugin is a Python module", and the
`scope_specs` / `provider_specs` values, which are modules but not plugins.

No backward-compatibility shim: hobserver is single-user, so an old `module`
key simply fails the existing "entry has no plugin" check. Nothing translates
the old spelling.

## Consequences

- One vocabulary at the config surface: `plugin` names the plugin, and the only
  survivals of "module" are where a Python module is literally meant.
- A breaking change to the config format. Any existing `hobserver.toml` must
  rename `module` to `plugin`; the tracked `hobserver.example.toml` already
  does.
- `scope_specs` and `provider_specs` keep their names. They are already
  role-named (`_specs`, not `_modules`), so `plugin` sits beside them without
  reintroducing the split ADR 5 had.
