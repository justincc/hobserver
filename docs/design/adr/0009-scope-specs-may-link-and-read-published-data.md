# 9. Scope specs may link and read published data

Date: 2026-08-05

## Status

Accepted — implemented 2026-08-05.

What the implementation settled, beyond the decision below:

- **`Diff` needed a `when=` gate.** A `mem0_update` with no recovered
  previous text was showing a lone `+` row repeating the text already above
  it: the pair exists *because* there is something to compare against. The
  old macro got this from an enclosing `if`; a declared spec has to say it.
- **A bare string is a source, never a literal.** `Link(text="…")` first
  fell back to treating an unresolved string as literal text, which put a
  source's own name on the page. `const()` is how a literal is written, and
  the same rule already applied to `Field(title=…)` — where three tooltips
  had been silently resolving to nothing.
- **The mem0 write scopes split by name.** `mem0_add`, `mem0_update` and
  `mem0_delete` became three specs rather than one conditional macro, which
  removed the branch the macro existed for.

Amends [ADR 7](0007-declare-scope-rendering-as-row-specs.md). Extends
[ADR 4](0004-cross-plugin-access-by-link-or-published-accessor.md) into the
spec vocabulary.

## Context

[ADR 7](0007-declare-scope-rendering-as-row-specs.md) kept three scopes
hand-written behind `render=`, and said that hatch stays internal: "a third
party cannot name a macro in turn.html, and should not be able to."

That reads as principled. It is not — it is an accident of where the mem0
plugin happens to live.

**Two of the three exceptions are one plugin's span rendering.**
`mem0_search_rows` and `mem0_write_rows` show mem0's data on a Prompts-tab
row: a link into the Mem0 tab, and the text a memory held before a write.
They are hand-written because ADR 7 found no way to declare them — and they
are *allowed* to be hand-written only because mem0 is in this tree.
[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md) exists so
that `plugins.mem0` could be replaced by an out-of-tree package. If it were,
its span rendering could not come with it.

**So the rule and its own example contradict each other.** Someone
contributing a Zep tab today can declare a plain `zep_search` scope through
`scope_specs` and needs no fork. But they cannot link from that span row into
their own tab, and cannot show anything from their own store beside it —
which is precisely what mem0 does, twice.

The gap runs deeper than the macros. `_prior_memory_texts` in the Prompts
plugin reaches `app.extensions["mem0_prior_text"]` **by name**, and keys the
lookup on `span.memory_id`, a mem0-shaped property in this app's assembler.
The accessor mechanism ADR 4 published is, in practice, reachable by one
plugin that this tab knows about.

**What is missing is not mem0-shaped.** It is exactly ADR 4's two shapes — a
link, and a published accessor — which the spec vocabulary has no words for.
ADR 7 said a fourth exception would mean the vocabulary was missing
something; the diagnosis arrives early, because two of the first three are
the same missing thing.

## Decision

**Give the vocabulary ADR 4's two shapes, so cross-plugin span rendering is
declared rather than hand-written.**

**1. `Link` — a row kind, resolved through `url_for`.**

```python
Link("mem0.search_event",
     params={"session": "session_id", "query": "search_query",
             "ts": "start_us"},
     text="mem0_result_count", transform=_link_text,
     title=const("Open this search in the Mem0 tab: …"),
     layer="detail")
```

An endpoint name and parameters, never a raw href. The target owns its own
URL, Flask fails loudly on an unknown endpoint, and the spec cannot invent an
address — the same three properties ADR 4 wanted from a link. Parameters take
sources, so they read from the span like any field. `text` is a source too —
`const()` for a literal — and takes a `transform` for the case where the
wording depends on what is being linked to. A link whose text does not
resolve does not render, so an in-flight call never shows a link to a result
that does not exist yet.

**2. `accessor(name, key=...)` — a source reading another plugin's published
lookup.**

```python
Field(attr(accessor("mem0_prior_text", payload("memory_id")), "text"),
      clip="wide-wrap")
```

Resolved from `app.extensions[name]`, called `fn(key, span_start_seconds)` —
the value to look up, and the moment on the timeline to read it as of.
`attr()` reads a field off whatever record comes back. An absent accessor resolves to nothing and its row drops, which is ADR
4's "degrade to doing without" already stated as the rule for callers.

`key` is a source, so it may be a payload key rather than a `Span` property.
This matters: a property is something only this tree can add, so an
out-of-tree plugin keying on one would be no better off than today.

**3. The mem0-specific plumbing goes.** `_prior_memory_texts` stops naming
mem0 and stops keying on `span.memory_id`; specs declare what they need. The
Prompts tab returns to knowing nothing about any particular store.

**4. `render=` shrinks to what genuinely cannot be declared.** The expectation
is that both mem0 scopes become declarative and `llm` alone remains — its
token tree runs a separator state machine, which is a different kind of
problem and is not addressed here.

`RENDER_MACROS` stays internal, and that becomes honest rather than
convenient: with links and accessors declarable, the reason a contributor
wanted a macro is gone.

## Consequences

- **The in-tree privilege closes.** mem0 becomes an ordinary contributor to
  the spec table, and the test of this decision is that its own rendering
  survives the move without a macro. If either scope resists, that is
  information worth recording here rather than forcing.
- **The four axes are unchanged.** `Link` is a row kind beside `Diff` and
  `Items`; `accessor` is a source beside `payload`. This grows the vocabulary
  without growing what a *field* is, which is the thing ADR 7 asked to keep
  closed.
- **Accessor calls land on the render path.** ADR 4 already set the bar — one
  indexed row per span, 0.014 ms measured, most turns having none — and a
  spec can now put a call there without the Prompts tab reviewing it. A turn
  page holds many spans and polls every 2 s, so an expensive accessor is a
  slow page. The bound is documented; it is not enforced, and a spec author
  who ignores it is the one who pays.
- **A spec can now emit an `<a>`.** Still not markup injection — an endpoint
  name and parameters, escaped by the template as ever — but it is a real
  widening of what a data-only spec produces, and worth saying plainly rather
  than discovering later.
- **Two plugins can now be coupled through a third party's spec.** A spec
  module may link to any registered endpoint and call any published accessor.
  Both are already public surface, so this adds no new authority; it does
  mean a spec module is a place where cross-plugin coupling can appear
  without either plugin knowing.
- **The conditional link text is the awkward residue.** "all 10 results"
  versus "full result" depends on comparing a count with the preview size. A
  `transform` on the text source carries it, at the cost of a callable in
  what is otherwise data. If that turns out to read badly, the honest
  alternative is leaving mem0_search on `render=` and saying why.
- **This is not speculative generality.** Two things make it concrete before
  any third party appears: it removes an inconsistency that exists in-tree
  today, and it deletes hand-written template code this repo already
  maintains. The author also expects to write plugins against this app
  himself — so "a contributor" is not a hypothetical the design is being bent
  around, and the cost of getting it wrong is paid here rather than by
  someone else.
- **ADR 4 is unchanged in principle.** Its two shapes are simply now available
  declaratively as well as in Python, and the rule that a plugin never opens
  another's source still holds — an accessor is still the owner's own
  function over its own store.

## Related

- [docs/writing-a-scope-spec.md](../../extending/writing-a-scope-spec.md) — would gain the
  cross-plugin case, which is currently the one thing it tells a contributor
  they cannot do
- [docs/span-rendering.md](../span-rendering.md) — the three exceptions table
  would drop to one
