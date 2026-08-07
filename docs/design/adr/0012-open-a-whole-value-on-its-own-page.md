# 12. Open a whole value on its own page

Date: 2026-08-06

## Status

Accepted, implemented 2026-08-06

Builds on [ADR 7](0007-declare-scope-rendering-as-row-specs.md) — the whole
value is declared, not branched on — and on
[ADR 11](0011-index-the-atof-log-rather-than-hold-it-in-memory.md), which is
why the value is not in memory to begin with.

## Context

Every row on a turn page is an excerpt. That is deliberate: a waterfall of
fifty spans cannot carry a 40,000-character assistant message, so
`llm_text` shows 400 characters and says how many there were, and the
generic renderer cuts a payload value at 2,000. Until now the sentence after
the cut was *"the whole message is in the ATOF log"* — true, and useless: the
log is 1.19 GB of JSONL, and finding one span in it means knowing its uuid,
grepping a gigabyte and reading a payload by hand. This was done twice in one
session to answer "what did that compaction call actually say", which is what
prompted this.

Three things were missing at once.

- **A prompt that belongs to no turn.** A turn page shows `turn.user_message`
  at the top — the prompt the *turn* ran on. hermes' own background calls
  have no such message: a context compaction is `call_role:
  auxiliary:compression`, its instruction is a single 26,000-character user
  message on the wire, and it appeared nowhere on this site. Nor did a
  subagent's `delegated` call, or a `fallback` retry. The one kind of call
  whose prompt the page did show was the ordinary one.
- **Nowhere to put a long value.** The turn page's layouts are a summary line
  and a detail block, both inside a table cell beside a bar chart. Neither is
  a place to render 225,000 characters of conversation.
- **No way for a scope to say it has one.** ADR 7 made rendering declarative,
  and a spec from outside this tree can name any payload key — but everything
  it could declare had to fit on a row.

## Decision

**A scope declares the complete values it holds; each one is a page of its
own, addressed by span uuid and key, and reached by an icon at the end of the
excerpt.**

    Scope(render="llm", fulls=[
        Full(key="prompt", source="llm_request_messages", render="sections",
             title=const("Prompt"), note=const("…")),
        Full(key="response", source="llm_response_text", render="markdown",
             title=const("Response"), note=const("…")),
    ])

    Field(payload("output"), full="output")      # …and a row that links to one

Five decisions inside that.

**One declaration, referenced from wherever the excerpt appears.** `fulls`
lives on the `Scope`, so a `Field(full=…)` and a hand-written macro asking
`scope_full(span, "prompt")` and the route serving `/turns/span/<uuid>/
<key>` all read the same list. The alternative — a `Full` inline on the
`Field` — would have left the route with nothing to resolve for a `render=`
scope, and two places to keep in step for every other one. A `Field` naming a
key the scope does not declare is reported by `check_table` at load, beside
the other spec faults.

**Addressed by span uuid, not through its turn.** The uuid is already on the
row, already has a copy button, and does not change when the assembly does.
It also reaches a span that landed in no turn — an llm call parented to the
session scope rather than to a `hermes.turn`, which happens, and whose prompt
was otherwise unreachable by construction.

**The icon is unconditional, and on the llm rows the excerpt is the link.**
The glyph is drawn whether or not the excerpt was cut short. A reader cannot see that a value fits, only that it ends, so an
affordance that appears only past some threshold is one to hunt for rather
than reach for. Deciding otherwise would also mean resolving the value on the
render path to measure it — reading a megabyte out of the log, per span, on a
page that polls every 2 s, which is precisely the cost ADR 11 exists to
avoid. Where the value is one paragraph of prose (`prompt`, `response`) the
whole of it is the anchor and the glyph rides inside, since a 12-pixel target
at the end of 400 characters is a small thing to ask a reader to hit. A
declared `Field` keeps the glyph alone: its value can carry a copy button,
and an anchor wrapped around a button is not markup worth writing.

**Markdown, rendered server-side, with raw HTML off.** What these values hold
is markdown: hermes' system prompt, a model's answer, a compaction summary
with its `## Historical Task Snapshot` headings. Rendering it as
`<pre>` was the honest minimum and not what a reader wants. `markdown-it-py`
is therefore the second runtime dependency, configured `html: False` — the
"commonmark" preset turns raw HTML *on*, so a `<script>` in a prompt would
otherwise be a `<script>` on the page. A missing or raising renderer degrades
that page to its text form and says why; it does not fail. Every page also
links to `?raw=1`, because markdown is a *reading* of the text and whitespace
survives nowhere else.

**A request is a list of labelled sections, not one document.**
`annotated_request` holds five kinds of message — `user`, `assistant`,
`tool_call`, `tool_result`, `provider_native` — and only two of them use
`content`. The first cut wrote a `## user` heading into the markdown ahead of
each one, distinguished from the content's own headings by being in
backticks. That failed the only test that matters: the first reader asked
whether the `user` under the header box was part of what was sent to the
model. It was not, and nothing on the page said so with enough force.

So `render="sections"` takes a source resolving to `[{"label", "text"}]` and
renders each text separately, under a label drawn as page furniture — outside
the markdown, in the header panel's own colours, above a bordered box holding
nothing but wire content. The split is this app's and every `text` is
verbatim, which is exactly the boundary the page now draws. `Full.note` still
states the provenance (principle 2), but the layout no longer depends on
anyone reading it.

**Amended 2026-08-07: the sections may be regrouped, and one kind is.** A
part may carry `"nested": True` and the part it belongs to `"nests": True`,
and the request page uses the pair to move each `tool_result` into the
`tool_call` it answers, matched by `call_id`. The wire sends every call and
then every result, so five results otherwise arrived as five boxes with
nothing tying them to the five calls above.

The two flags exist because the pair is drawn as **one card** rather than as
an indented box: the call has to know to run into what follows it, not only
the result to know it is inside something. That is also what lets the
result's label stay a bare `tool_result` — the card says which call it
answers, so the label does not have to. An earlier draft numbered both
labels instead, and the layout made the numbering redundant.

A `sections` value also gets a **contents list** down the left, sticky, one
entry per part, labelled with the parts' own labels and anchored by
position. It follows from the same fact that made sections the right shape:
the parts are separately addressable things, so they can be listed and
jumped to. It is not shown for a single part, nor for a value that is not
sections at all.

That is a departure from the order sent, on the page whose subject is what
was sent, so it is exactly the case `Full.note` exists for: the note names
the regrouping in words and the indent shows it. Recorded as a standing rule
under "the source's order is the default, not a vow" in
[design-principles.md](../design-principles.md). A result whose `call_id`
matches no call in the request stays where it arrived and keeps a bare
label — never observed in the log (26,944 results, none orphaned), but a
reader seeing an ungrouped result should be able to trust it was unpaired.

## Consequences

- **The vocabulary grows by one concept, not by a fifth axis.** `Full` is a
  declaration beside `rows`/`render`, like `Link` was a row kind beside
  `Diff`. What a *field* is stays closed: `full=` on a `Field` is a key, not
  a renderer. `profile()` joins `payload()` / `payload_end()` as a source,
  because the values worth opening — request and response — are in neither
  payload, and without it a contributed spec could reach them only through a
  `Span` property it cannot add (the fork test).
- **Every model call now shows what it was asked.** The llm scope is keyed by
  *category*, so a compaction, a delegated subagent call and a fallback retry
  get the same two links as an ordinary one. This is the change that answers
  the question that prompted the ADR.
- **A second runtime dependency.** The project had exactly one (flask), and
  the bar for a second is now set by this: a pure-Python library with no
  transitive dependencies of its own, doing something this tree should not
  hand-roll, behind a degradation path. Vendoring a renderer was the
  alternative and would have been our correctness problem forever.
- **A page can now be one span's payload.** `hydrate_span` reads one span
  rather than a turn's forty — a new entry point into ADR 11's hydration, and
  the reason opening a 225 KB request costs a 0.6 s render rather than a
  turn's worth of log reads.
- **The page carries no tab bar.** It is opened from a span icon into its own
  tab to read one thing, so tabs offering to navigate away are chrome for a
  place the reader never came from. Its nav row holds the two places it owes
  a reader — back to the turn the span ran in, and the raw text of the value
  — grouped left like the turn page's own. The turn list is deliberately not
  among them, for the same reason: it would strand a reader in the tab they
  opened to read one prompt. `base.html` gained a `tabbar` block for it,
  published in
  [writing-a-plugin.md](../../extending/writing-a-plugin.md) with the rule
  that a page reached by *navigating* always keeps the bar.
- **`|safe` appears in a template for the first time.** Exactly once, on the
  renderer's output, with the reasoning in `fulltext.py`. Everything else on
  the page is escaped as ever. This is the thing to re-check if the renderer
  is ever swapped.
- **The excerpt and the full value must stay the same value.** `llm_text` and
  `llm_response_text` read one key of one payload for that reason; an excerpt
  that is not a prefix of what its icon opens would be a quiet lie. A test
  holds them together.
- **Not applied to the generic payload renderer.** A scope with no spec still
  says "too large to show here" for an oversize value. Doing it there means
  addressing a value by payload key rather than by declared name, which is a
  second addressing scheme; it is worth doing only if the ordinary case turns
  out to be reading unknown tools' payloads.

## Related

- [docs/design/span-rendering.md](../span-rendering.md) — what the llm scope
  shows, row by row, including the `prompt` row this added
- [docs/extending/writing-a-scope-spec.md](../../extending/writing-a-scope-spec.md)
  — `Full` in the vocabulary a contributed spec may use
- [docs/extending/plugins-and-urls.md](../../extending/plugins-and-urls.md) —
  `/turns/span/<uuid>/<key>` among this app's URLs
