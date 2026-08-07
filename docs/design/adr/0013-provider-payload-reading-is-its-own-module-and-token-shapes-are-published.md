# 13. Provider payload reading is its own module, and token shapes are published

Date: 2026-08-07

## Status

Accepted

Extends [ADR 6](0006-parse-atof-by-declared-schema-era.md)'s normalization and
[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md)'s
"extensible without a fork" rule to the provider axis.

## Context

`atof_reader` was mapping two different things at once and the second had
gone unnoticed:

- **Per exporter.** hermes has written this stream through two owners, and
  ADR 6 made that a per-event dial. That is what the module was built for.
- **Per provider.** Which API answered — OpenAI Responses, OpenAI Chat
  Completions, Anthropic Messages — decides where the assistant's words, its
  tool calls and its token counts sit. This had accreted into the same file
  as a set of `if "output" in data` branches.

Two failures made the second axis worth separating, both found in one
sitting while chasing "why does openrouter show no tokens":

- **A whole route showed no token counts at all.** `_canonical_usage` read
  only the Responses shape. Every openrouter call in the log — kimi-k3 among
  them — rendered no token rows, while the log held every figure.
- **A latent wrong number.** Anthropic's `input_tokens` is what was sent
  *fresh*, with the cache read counted alongside it; OpenAI's is the whole
  prompt with the cache inside it. The reader assumed the second
  unconditionally, so an Anthropic payload would have reported a 20,100-token
  prompt as 1,200. hermes itself forks on this in
  `agent/usage_pricing.py` — the `anthropic_messages` arm adds where the
  others subtract — so it is a real fork and not a quirk of one payload.

Both are the same shape of mistake: a rule learned from the one provider
this app had seen, written where nothing marked it as provider-specific.
[design-principles.md](../design-principles.md) already says a payload shape
uniform across a whole log is not a contract; nothing was enforcing where
that knowledge lived.

And it could not be fixed from outside. A stranger can add a tab (ADR 5), a
scope spec (ADR 7), or a whole tab's worth of span rendering (ADR 10)
without touching this tree. A stranger on a router this tree has never heard
of had to patch `atof_reader.py`.

## Decision

**`plugins/turns/providers.py` is the only module that knows one provider
from another**, and **its token-count shapes are a published extension
point.**

### The module split

Everything that depends on *which provider answered* moves there: assistant
text, tool calls, the token-count tables, and the `normalize_llm_end` that
assembles them. `atof_reader` keeps the ATOF envelope — timestamps, the
schema-era dial, the event dataclass, generic payload rendering — and calls
into `providers` for the payload inside it.

The test suites split the same way: `test_providers.py` owns the shapes,
`test_atof_reader.py` keeps the seam (that a chat-shaped payload arriving
through `parse_line` comes out with counts on it).

### A `UsageShape` is a probe plus a table

```python
UsageShape(name, convention, counts, matches)
```

`matches(usage)` is probed on the payload's own keys, never on the route
name in `metadata`: `data` is opaque per the ATOF spec and the route name is
a label, not a schema declaration.

`counts` maps canonical names to paths, `(canonical_key, (step, ...))`,
earlier entries winning. Most counts mean the same thing everywhere and only
the spelling varies, so `COMMON_COUNTS` covers them for every provider and a
contributed shape reuses it.

`convention` is `WHOLE` or `PARTS` — the one count whose *meaning* varies.
**It is not carried as a flag on the count**: it is which canonical key the
table maps the provider's input figure to, and `complete_the_prompt` derives
whichever of `prompt` / `in` is missing from the other. So `cache read + in +
cache write == prompt` is the *definition* of the derived figure in both
directions, and `check_shapes` rejects a table whose declared convention and
mapped key disagree — the exact fault that is otherwise silent.

### Contributed shapes are tried first, not merged

`provider_specs = ["hermes_observer_acme.providers"]` names modules
exposing `USAGE_SHAPES`. They go in **front** of the built-ins.

Not merged, because a shape is a probe rather than a table entry, and the
built-in `openai_compatible` deliberately claims any payload with a
recognisable OpenAI-ish key. Something more specific can only win by being
asked first. That is the same "most explicit wins" outcome `scope_specs`
gets by merging, reached the way a probe list allows.

Validated at startup by `check_shapes` and reported through `sources()`, so
a malformed shape is named in the banner rather than raising from inside the
parser on some later line of a 1.2 GB log. A module that fails to import,
offers nothing, or offers something malformed is skipped: its provider's
counts fall back to the built-in reading, which is what they were before it
was installed.

### The index fingerprint covers contributed modules

A third-party `UsageShape` decides what the index's stored token counts mean
exactly as much as this tree does, so `code_fingerprint` hashes the source
of every module a live shape came from. Editing one invalidates an index
built before the edit. Without this the counts would be stale in the one way
[ADR 11](0011-index-the-atof-log-rather-than-hold-it-in-memory.md) does not
permit a cache to be.

The shape table is therefore threaded explicitly — `parse_line`,
`AtofIndex`, `hydrate_turn` all take it — rather than read from a global. An
index and the shapes that filled it belong together, and hydration
re-derives counts on re-read: a hydrated span disagreeing with the indexed
one would be this app contradicting itself between two renders of the same
span.

### Assistant text and tool calls are *not* published

Deliberately, and on evidence rather than effort. hermes'
`annotated_response` carries both, uniformly across every route in the log,
and leads over the raw payload — so a new provider's text usually works
already. Token counts are where providers actually diverge. Reading text is
also a walk over typed content parts rather than a lookup, and publishing it
would mean inventing a mini-language for something nothing has yet needed.

`UsageShape` is a dataclass; gaining an optional `assistant_text` callable
later is a backwards-compatible change if a router ever needs one.

## Consequences

- **A new router is a config line, not a patch.** The case that prompted
  this — a token shape nothing in-tree recognises — is now the worked
  example in
  [writing-a-provider-spec.md](../../extending/writing-a-provider-spec.md).
- **`atof_reader` shrank by about 200 lines** and states one thing. The
  "where does provider knowledge live" question now has a file for an
  answer, which is what stopped the Anthropic convention being noticed.
- **The convention fork is enforced rather than trusted.** `check_shapes`
  catches a contributed shape that would derive in the wrong direction —
  which produces a plausible wrong number, the worst failure mode available
  here.
- **A contributed shape can hurt only its own provider.** A probe that
  raises is skipped rather than propagated, so one bad module costs its own
  rows and not everyone else's.
- **`provider_specs` is a third thing to keep in step with hermes.** It joins
  `scope_specs` and the tab list. The banner naming all three at startup is
  what keeps that manageable.
- **This does not publish the token *tree*.** A provider reporting a count
  with no row in `assembler.TOKEN_TREE` — Anthropic's per-TTL cache tiers,
  say — still needs an in-tree change to display it. Mapping is extensible;
  what the page shows is not. Recorded so that is a known edge and not a
  surprise.
