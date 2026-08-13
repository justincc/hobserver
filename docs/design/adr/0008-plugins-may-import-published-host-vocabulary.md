# 8. Plugins may import the host's published vocabulary

Date: 2026-08-05

## Status

Accepted

Amends [ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md).

## Context

[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md) said **"a
plugin imports nothing from the host"**, reasoning that the contract is plain
module attributes and Flask, "so a third-party tab does not depend on a
hobserver package or track its version".

That was a true *description* of the tab contract, written down as a
*prohibition*. A tab needs `PLUGIN_API`, `bp`, `TAB_LABEL`, `URL_PREFIX` and
some Flask — no import from this app is required, so none happened, and the
absence got recorded as a rule.

Two things show the rule was drawn in the wrong place.

**It does not describe what a plugin actually depends on.** ADR 5 conceded
one coupling in the same breath — `templates/base.html`, whose classes,
`data-live-poll` convention and tab bar every tab inherits. A tab that renders
a `.span-detail` row is bound to this app's CSS as surely as one that imports
a name, and renaming a class breaks it just as hard. The same is true of the
ATOF envelope a reader plugin parses. The import statement was never the
coupling; it was only the *visible* form of one, and forbidding it hid the
rest rather than removing them.

**And [ADR 7](0007-declare-scope-rendering-as-row-specs.md) needs one.** A
scope spec is written in a vocabulary — `Field`, `Row`, `Scope`,
`payload()` — and must import it from `plugins.turns.scope_spec`. The only
way to honour ADR 5 literally would be a mini-language of plain dicts: a
parser to write, a worse thing to read, and no less version-coupled for
having no import statement in it.

The distinction that matters was never import-or-not. It is whether the thing
being depended on is **published** — documented, and changed with the care a
public surface deserves — or whether it is this app's internals.

## Decision

**A plugin may import from hobserver, from the surfaces this app
publishes. What is forbidden is depending on internals, and on other
plugins.**

Published surfaces, changed with the same care as a URL:

| surface | for |
| --- | --- |
| `templates/base.html` — classes, the tab bar, `data-live-poll` | any tab that renders a page |
| `plugins.turns.scope_spec` — the spec vocabulary | anyone declaring a scope ([ADR 7](0007-declare-scope-rendering-as-row-specs.md)) |
| the tab contract attributes and hooks | any tab |

Not published, and not to be imported: everything else — `assembler`
internals, `atof_reader`, a tab's own helpers.

**Unchanged, and the rule that was actually doing the work:** a plugin never
imports *another plugin*, and never opens another plugin's source.
[ADR 4](0004-cross-plugin-access-by-link-or-published-accessor.md) governs
that, by link or published accessor, and this decision does not touch it.

**Importing nothing is still the lightest thing a plugin can do**, and where
a tab needs nothing it should keep taking nothing. The point is that reaching
for a published name is a legitimate choice rather than a violation.

## Consequences

- **Version coupling becomes explicit rather than denied.** A plugin that
  imports `scope_spec` tracks this app's version; so, less visibly, did every
  plugin that ever used a `base.html` class. `PLUGIN_API` is what says which
  contract a plugin was written against, and published surfaces move with it.
- **`docs/writing-a-plugin.md`'s checklist changes** from "imports nothing
  from hobserver" to importing only published surfaces, and
  `docs/plugins-and-urls.md` no longer says a plugin imports nothing from
  this app.
- **The list above is now a thing to maintain.** Adding a module to it is a
  commitment; the failure mode is publishing something by accident, which is
  why it is a short table in an ADR rather than an implication of what
  happens to be importable.
- **ADR 7's contributed specs stop being an exception.** They were recorded
  there as breaking ADR 5; they are ordinary under this one.
- **Nothing in the code changes.** This decision only says that what ADR 7
  already implemented is allowed, and corrects two documents that told people
  otherwise.
