# 21. The span and provider vocabularies live at the repo root

Date: 2026-08-17

## Status

Accepted

Relocates the modules from [ADR 7](0007-declare-scope-rendering-as-row-specs.md)
and [ADR 13](0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md);
extends [ADR 8](0008-plugins-may-import-published-host-vocabulary.md).

## Context

Two published vocabularies lived inside the Turns tab's package:

- `plugins/turns/scope_spec.py` — the row-spec vocabulary a plugin imports to
  say how its spans render (ADR 7).
- `plugins/turns/providers.py` — the token-count shapes and per-provider payload
  reading a plugin imports to add a `provider_spec` (ADR 13, ADR 8).

So the Turns tab's *package* was a de-facto shared library for the whole
span-and-provider extension point. `plugins/memory/mem0/scopes.py` does
`from plugins.turns.scope_spec import …`, and the provider-spec guide tells an
author to `from plugins.turns.providers import …`. A plugin could not contribute
span rendering or a provider shape without importing the turns package — turns
was load-bearing infrastructure, not a peer tab. The detachability review
flagged this (its finding #2): move `plugins.memory.mem0` out of the tree and it still
needs `plugins.turns` present for the vocabulary.

Neither module is the Turns tab's UI, and neither depends on the tab's ATOF
internals: `providers.py` imports nothing from the app, and `scope_spec.py`
only borrowed one eight-line payload helper.

## Decision

Move both to top-level modules at the repo root: `scope_spec.py` and
`providers.py`. The published import path changes:

    from plugins.turns.scope_spec import Field, Row, Scope   # was
    from scope_spec import Field, Row, Scope                 # now

    from plugins.turns.providers import UsageShape           # was
    from providers import UsageShape                         # now

The neutral home is the repo root, **not the shell**. `tabs.py`/`app.py` still
never import either module and stay ignorant of what a scope spec is; these are
shared surfaces owned by no tab, alongside `hermes_paths.py`. The Turns tab
becomes a consumer of them like any other plugin.

`scope_spec.py` carries its own private copy of the `_as_dict` payload helper so
it imports nothing from the app; the Turns tab's `spans.py` keeps its own. A
deliberate eight-line duplication, to buy the vocabulary its standalone-ness.

An import path is a contract, the way a URL is (ADR 5): this breaks any external
plugin that imported the old path. No shim — hobserver is single-user.

## Consequences

- A span- or provider-contributing plugin now depends on a neutral module, not
  on the Turns tab's package. The tab could be renamed or removed and the
  vocabulary others import would still resolve.
- `providers.py` moved whole — both the published shapes and the reading of a
  provider's payload. That is all provider knowledge (ADR 13: "the only module
  that knows one provider from another"), so it belongs together; the Turns tab
  imports the reading functions as a consumer.
- ADR 7's vocabulary and ADR 13's module are unchanged in substance — only their
  location moved. This ADR is the pointer for anyone who finds the old path.
- Tests for the two vocabularies stay under `plugins/turns/tests/` for now: they
  exercise the vocabulary against the Turns tab's own spans. Moving them is a
  later tidy, not part of this decoupling, which is about the production import
  path a stranger writes against.
