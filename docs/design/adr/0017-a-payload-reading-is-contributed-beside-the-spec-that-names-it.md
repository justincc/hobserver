# 17. A payload reading is contributed beside the spec that names it

Date: 2026-08-14

## Status

Accepted, implemented 2026-08-14

Completes [ADR 7](0007-declare-scope-rendering-as-row-specs.md) and
[ADR 10](0010-a-tab-contributes-its-own-scope-specs.md): a tab could already
say how its spans *show*, and now says how they are *read*.

## Context

ADR 7 made rendering declarative and its own first draft failed the fork test
one level down — a stranger could register a spec but not read a payload key
without patching `assembler.py`. `payload()` fixed the *lookup* case: a
`Field(payload("service"))` needs nothing in this tree.

It did not fix the *reading* case. Where a value has to be worked out rather
than looked up — decoded from a JSON string, walked, ranked, counted,
type-guarded item by item — the spec has to name a `Span` property, and
`Span` is in `plugins/turns/assembler.py`. So the reading lived here even when
the tool did not.

mem0 was the worked example, and an uncomfortable one. mem0 has a tab of its
own, and that tab owns everything else about mem0: the store, the event log,
the pages, and — since ADR 10 — the scope specs. But `Span.mem0_results`,
which knows that a mem0 search result is a JSON string holding
`{"count": n, "results": [{id, memory, score}]}`, sat in the Turns tab's
assembler, which is named for the ATOF envelope and has no business knowing
that. It failed the ownership test in
[design-principles.md](../design-principles.md) §1: *if mem0's result shape
changed, which single file would I open?* The honest answer was two.

Anyone outside this tree had it worse: their tool could be displayed only as
far as its payload could be read by lookup alone.

## Decision

**A contribution may carry `SPAN_READERS` beside its `SCOPES` — `{name:
fn(span) -> value}` — and a spec's bare-string source resolves against those
readers before the `Span` object.**

    # plugins/memory/mem0/spans.py
    def mem0_results(span): ...
    SPAN_READERS = {"mem0_results": mem0_results}

    # plugins/memory/mem0/scopes.py — unchanged by this ADR
    Each("mem0_results", [...])

Four decisions inside that.

**A reader gets the span, not an extract of it.** It reads `start_data`,
`end_data`, the metadata, the timings — the values as they came, not this
app's curated accessors. Design principle 1 requires that of an extension
point: reach the source, or the next reading nobody anticipated is another
patch on this tree.

**Readers travel with specs, and die with them.** They are one contribution
from one module: a `SPEC_ATTRS` entry the shell carries without looking
inside, merged by the same three layers in the same order — this tree, then
the loaded tabs, then modules named in settings. A fault in either half skips
the whole contribution, because rows naming readings that never loaded are
worse than the payload dump they replace.

**A reader beats a `Span` property of the same name**, and the startup line
says so (`overriding Span.command`). The in-tree table is a default and not a
floor, which is already the rule for specs; a property is only a reading with
a shorter path to it. This is the one override nothing else would show — the
rows keep rendering, with different values in them — so it is named
explicitly rather than left to be noticed.

**A raising reader costs its own value, nothing else.** It is somebody else's
code reading somebody else's payload, the case `accessor()` already treats
this way. One field resolves to nothing; the row, the span and the page
survive.

In-tree readings stay `Span` properties. The point is not to move them —
`patch_mode`, `memory_ops` and the rest are the Turns tab reading hermes'
tools, which is its own job — but that **a foreign system's reading now has
somewhere else to live**, and mem0's has moved there
(`plugins/memory/mem0/spans.py`, with its tests).

## Consequences

- **The Turns tab no longer knows what mem0 is.** `assembler.py` has no mem0
  branch left; disabling the Mem0 tab now takes the reading of its spans with
  the rows, and a mem0_search span renders as the payload dump any unknown
  tool gets. That is the ADR 10 lifetime rule finally applying to the whole
  of a contribution.
- **Two dead properties went with it.** `memory_content` and `memory_id`
  survived ADR 9's move of the mem0 specs to `payload()` sources with nothing
  naming them — mem0 knowledge kept alive in the assembler by nothing but
  inertia. Their tests were the only thing reading them.
- **A stranger can now contribute a whole tool.** How its payload is read and
  how it shows, neither in this tree, which is what the fork test asks for
  end to end.
- **A third published surface.** `SPAN_READERS` joins `SCOPES` and the spec
  vocabulary as something a stranger may depend on, and the reader signature
  — `fn(span) -> value` — is now an interface. It is deliberately the
  smallest one that does the job: no registration call, no base class, no
  access to anything but the span.
- **The index is not involved.** Readers run at render time, on payloads
  hydrated for the page being shown (ADR 11). A reader is not a place to put
  something the *assembly* needs; `project()` is still the only path for
  that, and still deliberately short.
