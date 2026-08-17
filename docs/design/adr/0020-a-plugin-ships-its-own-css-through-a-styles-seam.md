# 20. A plugin ships its own CSS through a `STYLES` seam

Date: 2026-08-17

## Status

Accepted

Extends [ADR 8](0008-plugins-may-import-published-host-vocabulary.md).

## Context

`base.html` is a published surface (ADR 8): it defines the CSS classes that
scope specs resolve to, and a test asserts they exist so a renamed class fails
loudly rather than rendering unstyled. Every span a plugin paints on the Turns
tab, and every one of the plugin's own pages, is styled from that one shell
stylesheet.

Two problems followed from the shell being the *only* place span CSS can live:

- **No seam.** A plugin could not add a visual the shell did not already carry.
  `base.html` has no head/style block, and a scope spec on another tab has no
  template of its own — it emits class names into the Turns tab's template. So
  any genuinely new styling meant editing `base.html`. This is the coupling the
  detachability review flagged: a plugin like `mem0` could be moved out of the
  tree in its logic, data, and own pages, but not restyled without a core edit.
- **The shell named a plugin.** The decoration vocabulary in
  `plugins/turns/scope_spec.py` mapped `deco="id"` → class `mem-id` and
  `deco="score"` → `mem-score`, and `base.html` styled and described them in
  `mem0` terms — even though the classes are generic and any plugin uses them.

## Decision

**1. A `STYLES` seam.** A plugin may expose a `STYLES` string of CSS. The shell
collects it the way it already collects `SCOPES`/`SPAN_READERS` (ADR 10, ADR
17): `tabs.py` reads it into `Tab.styles`, `app.py` concatenates every loaded
plugin's `STYLES` in tab order — each block fenced with `/* plugin: <name> */`
— and `base.html` injects the result once into every page's `<head>`. A plugin
can now carry the styling for what it renders, on its own pages and on another
tab's, without the shell holding its classes.

It is injected `|safe`. A plugin is installed Python (SECURITY.md): its CSS is
no more privileged than the code already running, so this widens nothing the
trust model did not already grant.

**2. The shared vocabulary stays in the shell, but plugin-neutral.** The common
decoration classes — used by *any* span-rendering plugin through `deco=` — stay
in `base.html`, still the published surface ADR 8 describes. Only their names
change to stop naming a plugin: `mem-id` → `detail-id`, `mem-score` →
`detail-score`.

So the line is: a class **more than one plugin** draws from is shared shell
vocabulary; a class **one plugin alone** needs (e.g. `mem0`'s `h2 .provenance`
event-page heading) moves into that plugin's `STYLES`.

## Consequences

- A plugin can add genuinely new visuals without patching the shell — the
  detachability barrier is gone. `base.html` no longer names any plugin in its
  styling.
- The seam is **additive**, not a replacement for the published vocabulary.
  ADR 8 still holds: a plugin should reuse the shared classes where they fit and
  ship `STYLES` only for what is genuinely its own. Scattering every plugin's
  CSS into `STYLES` would trade the shell's one coherent stylesheet for many —
  the seam is for what the vocabulary does not already cover.
- It does **not**, by itself, detach an existing plugin's span styling. Most of
  `mem0`'s span classes (`.detail-id`, `.detail-score`, `.mode-tag`, `.prov`)
  are shared vocabulary that the Turns tab and others render too; they are not
  one plugin's to take. Only styling a single plugin owns can move.
- `STYLES` is a new, optional part of the plugin contract and belongs in
  `docs/extending/writing-a-plugin.md` when this lands.
