# 10. A tab contributes its own scope specs

Date: 2026-08-05

## Status

Accepted

Amends [ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md)'s
plugin contract. Completes
[ADR 9](0009-scope-specs-may-link-and-read-published-data.md).

## Context

[ADR 9](0009-scope-specs-may-link-and-read-published-data.md) made mem0's span
rendering declarable, so it stopped needing a hand-written macro. It left the
declarations where the macros had been: in the Prompts tab's own table,
`plugins/turns/scopes.py`.

That is still the wrong owner. The specs describe mem0's spans, link to mem0's
pages and call mem0's accessor; the only thing tying them to the Prompts tab
is that the Prompts tab paints the rows. ADR 9 removed the *privilege* of
being in-tree without moving the code that had it.

Two things follow from leaving them there:

- **mem0 still could not move out of this tree intact.** ADR 5's goal was that
  `plugins.memory.mem0` be replaceable by an installed package; its span rendering
  would have stayed behind.
- **A link can outlive its target.** `url_for` raises `BuildError` for an
  endpoint nothing serves — verified, not assumed. With the mem0 tab disabled
  in `hobserver.toml` and its specs still loaded, every turn page holding a
  mem0 span would 500. Nothing in the design prevented that; it simply had not
  happened yet because the specs and the tab were the same deployment unit by
  accident.

The existing `scope_specs` setting can load a spec module from anywhere, so
the specs *could* be named there. But that puts the wiring on the **Prompts**
tab, away from the plugin it belongs to, and makes enabling a tab and
enabling its spans two edits that have to be kept in step — with the dangling
link as the failure mode when they drift.

## Decision

**A tab may expose `SCOPES` and `SCOPES_BY_CATEGORY`, and the shell hands them
to whichever tab paints spans.**

Optional, like `init_app` and `sources`; a tab with no spans of its own
exposes nothing and is unaffected.

```python
# plugins/memory/mem0/__init__.py
PLUGIN_API = 1
bp = Blueprint("mem0", __name__, template_folder="templates")
TAB_LABEL, URL_PREFIX = "Mem0", "memory/mem0"

from plugins.memory.mem0.scopes import SCOPES    # how its spans show elsewhere
```

**The shell carries them without understanding them.** `tabs.py` reads the two
attributes onto `Tab.scopes` and never looks inside, exactly as it does with
`settings` — it does not know what a scope spec is, and
[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md)'s "the
shell stops importing any plugin" holds unchanged. `create_app` collects them
into `app.extensions["tab_scopes"]` **before registering any tab**, because a
painting tab resolves its table in `init_app`, which runs during registration
and would otherwise see only the tabs listed before it.

**Precedence is three layers, each overriding the last:** the painting tab's
own defaults, then what other tabs contribute, then modules named in
`scope_specs`. Most explicit wins, and every override is named in the startup
banner.

**`scope_specs` stays.** It is the route for a spec module with no tab behind
it — someone with hermes tools to render and no page of their own — and it
remains the only way to override a scope another tab contributed.

**And a link whose endpoint nothing serves drops its row.** `spec_link()`
replaces `url_for` in the template and returns None on `BuildError`. Spec
lifetime is the first defence and the reason the dangling case is now rare;
this is the second, for a `scope_specs` module pointing at an absent tab, and
for the ordinary typo.

## Consequences

- **mem0 becomes a package**, `plugins/memory/mem0/__init__.py` plus
  `plugins/memory/mem0/scopes.py`, matching `plugins/turns/` file for file. Its rendering now
  travels with it: lifting the directory into an installed package takes the
  tab, its templates, its accessor and its span rendering together. (The
  templates were still in the app's `templates/mem0/` when this was written,
  which made the sentence aspirational; they moved into the plugin
  immediately after, which is what made it true.)
- **Disabling a tab removes its spans.** They fall back to the generic payload
  dump — which is what they showed before that plugin existed — and the page
  serves. Verified both ways.
- **The Prompts tab knows no other plugin by name.** It reads
  `app.extensions`, and the mem0-specific `_prior_memory_texts` is gone. The
  last hardcoded mention of one plugin inside another went with it.
- **`Tab.scopes` is opaque state the shell carries for someone else.** That is
  a real if small widening of what the shell holds; the alternative was the
  shell importing spec machinery to validate it, which would couple it to the
  Prompts tab. Validation stays with the tab that understands it — a
  contributed table that fails `check_table` is reported in the banner and
  skipped, not merged half-broken.
- **Two tabs could contribute the same scope name.** Last loaded wins, in
  config order, and the banner names the override. This is the same rule
  ADR 7 set for `scope_specs` and deliberately not ADR 5's fatal collision,
  because a display choice is not a mis-routed URL.
- **A tab now has two ways to affect another tab's page**: contributing specs,
  and publishing an accessor those specs call. Both are declared, both are
  visible at startup, and both stop when the tab is disabled.

## Related

- [docs/writing-a-plugin.md](../../extending/writing-a-plugin.md) — the contract, now with
  the optional spec attributes
- [docs/writing-a-scope-spec.md](../../extending/writing-a-scope-spec.md) — which of the
  two routes to use, and when
