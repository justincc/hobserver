# Writing a provider spec

How to make the Turns tab read token counts from a router it has never heard
of.

A provider spec is a Python module holding a `USAGE_SHAPES` tuple. It can
live in this repo (`plugins/turns/providers.py` is one) or in a package of
your own installed alongside — both load the same way, so a new router needs
no fork and no patch carried on top of this tree.

You need one when an llm span on a turn page shows a duration and a model but
**no `tokens` block**, while the provider's own payload plainly has the
figures in it. That means no shape claimed the usage payload.

- [span-rendering.md](../design/span-rendering.md) is what the counts look
  like once they arrive — the token tree, the cache share, the two layouts.
- [atof-reader.md](../design/atof-reader.md#the-token-counts) is the mapping
  as this tree already implements it, including the built-in shapes yours
  will sit in front of.
- [ADR 13](../design/adr/0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md)
  is why this is the published half and assistant text is not.

## First: check whether you need one

Most routers do not need a spec. The built-in `openai_compatible` shape
claims any payload with a recognisable OpenAI-ish key, and reads several
spellings of each count — nested `prompt_tokens_details.cached_tokens`,
nested `input_tokens_details.cached_tokens`, top-level
`cache_read_input_tokens`, flattened `cache_read_tokens`. A router being
"OpenAI-compatible" in the usual sense is already covered.

Write a spec when your provider names its counts something else entirely, or
when it uses Anthropic's input convention without Anthropic's key names.

## A whole spec module

Say the acme router reports usage as
`{"acme_prompt_total": ..., "acme_cached": ..., "acme_generated": ...}`.

```python
"""Provider specs for the acme router."""

from plugins.turns.providers import COMMON_COUNTS, WHOLE, UsageShape


def _is_acme(usage):
    # Probe on a key only this router writes. Never on the route name in
    # `metadata` — that is a label, not a schema declaration.
    return "acme_prompt_total" in usage


ACME_ROUTER = UsageShape(
    name="acme_router",
    convention=WHOLE,          # acme_prompt_total includes the cached part
    counts=(
        ("prompt_tokens", ("acme_prompt_total",)),
        ("cache_read_tokens", ("acme_cached",)),
        ("output_tokens", ("acme_generated",)),
    ) + COMMON_COUNTS,         # keep the standard spellings as fallbacks
    matches=_is_acme,
)

USAGE_SHAPES = (ACME_ROUTER,)
```

Then in `hobserver.toml`:

```toml
[[tabs]]
module = "plugins.turns"
settings = { provider_specs = ["hobserver_acme.providers"] }
```

That is the whole contract. The tab reads the module at startup, puts your
shapes in front of the built-ins, and names them in the startup banner.

## The four fields

| field | what it is |
|---|---|
| `name` | short identifier, shown in the banner. Yours, not the provider's marketing name |
| `convention` | `WHOLE` or `PARTS` — see below. Get this wrong and you get a plausible wrong number |
| `counts` | `((canonical_key, (step, ...)), ...)`; the first source that yields a number wins |
| `matches` | `usage_dict -> bool`, probed on the payload's own keys |

The canonical keys are `prompt_tokens`, `input_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`.
Map any subset; an absent count stays absent, because absent and zero mean
different things to every row that reads these.

## The one thing to get right: the convention

Providers disagree about what their "input" figure counts, and reading one as
the other is a **wrong number rather than a missing one**.

| convention | the figure means | map it to | examples |
|---|---|---|---|
| `WHOLE` | the entire prompt, cache included | `prompt_tokens` | OpenAI Chat Completions and Responses, most proxies |
| `PARTS` | only what was sent fresh, cache counted alongside | `input_tokens` | Anthropic Messages |

`cache read + in + cache write == prompt` holds either way. Your table
supplies one side of it and `complete_the_prompt` derives the other, so the
convention is expressed by **which canonical key you map the figure to** —
`check_shapes` rejects a shape whose declared convention and mapped key
disagree, rather than letting it derive backwards in silence.

A `PARTS` shape therefore looks like:

```python
from plugins.turns.providers import COMMON_COUNTS, FRESH_INPUT_COUNTS, PARTS

BESPOKE = UsageShape(
    name="bespoke_messages", convention=PARTS,
    counts=FRESH_INPUT_COUNTS + COMMON_COUNTS,
    matches=lambda usage: "bespoke_fresh_input" in usage,
)
```

**Anthropic's cache key names do not imply Anthropic's convention.**
OpenAI-compatible proxies routing Claude expose `cache_read_input_tokens`
beside a `prompt_tokens` that is still the whole prompt. If your router does
that, it is `WHOLE`.

## Where your shape sits

Contributed shapes are tried **before** the built-ins, in the order your
module lists them, and the first whose `matches` returns true wins. This
matters: `openai_compatible` is deliberately broad and would otherwise claim
your payload first. Being asked first is how a more specific shape wins.

It also means you can override a built-in reading — probe for whatever
distinguishes your deployment's payload and yours is used instead.

## When it breaks

Failures are startup-time and named, never silent:

- **Module will not import** — reported in the banner, skipped. The tab still
  serves; your provider's counts fall back to the built-in reading.
- **No `USAGE_SHAPES`** — same.
- **Malformed shape** — `check_shapes` runs at startup and names the fault:
  a convention that contradicts the table, a count that is not canonical, a
  non-callable probe, a malformed `(key, path)` pair. The module is skipped
  whole rather than half-loaded.
- **A probe that raises at read time** — that shape is passed over for that
  payload and the next one is tried. One bad module costs its own provider's
  rows, not everyone else's.

Check the startup banner: every contributed module is listed there beside
the log it reads, with its problem if it has one.

## Two things to know about the index

- **Editing your module invalidates the index.** Your shape decides what the
  stored token counts *mean*, so its source is hashed into the index
  fingerprint. Change it and the next request rebuilds — seconds, once.
- **Counts may not be in the end payload.** Providers on the
  chat-completions route report usage only on the *final chunk of the
  stream*, leaving the span's own end payload with `usage: null`. This is
  handled for you: chunks are folded into the index and `Span.usage` falls
  back to them. Your shape reads whichever payload carries the counts,
  without knowing which one it was.

## What is not extensible

- **Assistant text and tool calls.** hermes' `annotated_response` carries
  both, uniformly across routes, and leads over the raw payload — so a new
  provider's text usually works already. See ADR 13 for the reasoning.
- **The token tree.** A count with no row in `assembler.TOKEN_TREE` maps
  fine and then displays nowhere. If your provider reports a kind of count
  this app has no row for, that needs an in-tree change; open it as a
  question rather than working around it in a spec.
