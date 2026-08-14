# Span rendering on the turn page

What a span shows, scope by scope.

**Where it is decided** (ADR 7): each scope's rows are declared as data in
`plugins/turns/scopes.py`, keyed by the hermes tool's own scope name;
`plugins/turns/scope_spec.py` holds the vocabulary those declarations are
written in; `plugins/turns/templates/turns/_macros.html` paints whatever they resolve to
and knows about no particular tool, and `turn.html` is the page around it.
The values come from `Span` properties in `plugins/turns/assembler.py`, or
straight from the payload.

One scope, llm, is not declarative and keeps hand-written Jinja, reached
through `render=` — see [the one exception](#the-one-exception).

**Adding a scope** is one entry in the `SCOPES` table. Order does not matter:
each scope names its own rows, so nothing has to precede anything. A scope
this app has no entry for renders its payload through the generic fallback,
and a spec module named in `hobserver.toml` can add or replace entries without
forking this tree — see
[design-principles.md](design-principles.md#1-extension-without-a-fork).

Example values here are illustrations from one stream, not a contract: hermes'
tools differ between users and versions, and a payload's real shape is
whatever the tool's own signature says (`$HERMES_SOURCE/tools/`, and
`$HERMES_SOURCE/plugins/memory/mem0/` for the mem0 tools). Anything phrased as
"in practice" means observed while building this, and could differ for you.

## The two layouts

Every row has a summary line and a detail layout:

- **Summary line** — the scope's own text trails the span name on one
  ellipsized line. This is what is visible without interacting.
- **Detail** — the same content laid out on its own rows (`.list-item`,
  `.span-detail`), with the parts too long for one line added.

The details switch — labelled **show all span details**, a slider on the
right of the legend row, off on every page load and never persisted — turns
detail mode on for the whole page. The label says what flipping it does to
the page rather than naming the switch: a lone `details` sitting beside three
colour chips read as a fourth legend entry.
Clicking a single row toggles just that row independently of the switch
(`tr.detail-open`, remembered by uuid across live-poll swaps); clicks on
links, buttons or a text selection do not toggle.

Whatever the summary line ellipsizes is in the row's `title` attribute, so a
hover shows the full text without switching modes.

The span or mark uuid sits muted after the name with a copy icon: it is the
lookup key into the raw JSONL. `tool_call_id` and `api_request_id` are not
shown.

## What is read

Nearly everything below comes from the span's **start** payload — the call's
input. Output is read in exactly three cases, each because the call is
unreadable without it:

- **failures**, on every scope (see [Failures](#failures));
- **memory**'s char budget, which is the whole story of that tool;
- **mem0_search**'s hits, since a query never says whether the answer was any
  good.

Payloads are opaque and read defensively even where the log has been uniform.

## llm scopes

An llm span carries what the model decided, said and cost — all of it in the
**end** event. The start payload is an empty `headers` dict and a `content`
that is usually empty, so the generic fallback below has nothing to work with.
This is one of the three scopes that keeps hand-written Jinja (`render="llm"`)
— its token tree runs a separator state machine the spec vocabulary should not
grow to absorb.

The rows split by where the value comes from. `call_role` and `retry` are the
span's **metadata** and stay `.mode-tag` chips on one line — faint monospace,
no keys, because each is a word that names itself. The four below them are the
**payload's** own values, and share the key column: `finish_reason`, `prompt`,
`response`, `tool_calls`.

**One rule holds down the whole span: a key is faint monospace, a value reads
in the row's own font.** `.mode-tag` and `.gen-key` are keys; `.row-value` is
the short-value counterpart, taken by the finish reason and by every figure in
the token tree. The token rows had label and figure in one `.mode-tag` until
the finish reason grew a key of its own and made them the odd rows out — a
reader should not have to learn a second convention between one row and the
next. `.row-value` sets no colour, taking whatever shade its row is in, and
carries `tabular-nums` — the one thing monospace was buying a column of
figures compared down the page.

**Every figure in the tree is one colour**, parts included. `.tok-part` used
to shade its row `#8a8a8a` — exactly `.mode-tag`'s own colour — so once the
figures became values, `reasoning 303` was the single pair on the span whose
key and value matched, and the row read as the only one the change had
missed. The class carries no shade now: it is a marker in the markup, and
what it means is left to the tooltip that was always carrying it.

- **What the call was for** — a `.mode-tag` leading the chip line,
  from `metadata.call_role`, shown only when the call was *not* the ordinary
  one. Its values are `delegated` (a subagent's call), `fallback` (the
  configured model failed and a backup answered), `iteration_summary`, and
  `auxiliary:<task>` for hermes' own background work — `auxiliary:compression`
  is the conversation being compacted to fit the context window.

  `primary` is withheld: it is 216 of the 218 calls in the log, and a tag
  every row carries is one no row is read for. Its absence is what says the
  call was ordinary. The auxiliary tasks are open-ended — hermes exposes
  `register_auxiliary_task` to plugins — so the value is rendered as it
  arrives rather than matched against a list kept here.

  hermes stamps `auxiliary_task` beside it, which is deliberately **not**
  read: both are built from one value in the same dict literal
  (`$HERMES_SOURCE/agent/auxiliary_client.py`), `call_role` being the f-string
  `"auxiliary:{task}"`. They cannot disagree, and only `call_role` is
  present on the other four roles.

- **`retry N`** — from `metadata.retry_count`, a 0-based attempt index, so
  it appears only when the call was retried or walked its fallback chain.
  Often the reason a span is slow. Zero is withheld for the reason a zero
  cache write is: it is every row, and says nothing.

- **`finish_reason`** — the first of the keyed rows, always visible, passed
  through as the exporter gives it. On the current exporter this is
  `complete` whether or not the model asked for tools; the older one said
  `tool_calls` or `stop`, the openrouter route says `tool_use`, and `length`
  would mean the answer was cut off.

  ```
  finish_reason  complete
  prompt         Have another look at hobserver's llm rows …
  response       The finish reason now carries its key …
  tool_calls     read_file · read_file · terminal
  ```

  **It carries its key, and only in the detail view.** The value used to
  stand alone everywhere, which worked no better than the unlabelled
  `tool_calls` row below: `complete` is a bare word in a row of bare words,
  saying nothing about what it is the answer to. Worse, a reader who took it
  to the log found `status: "completed"` on the same event — `data.status`
  and `annotated_response.api_specific.status`, the OpenAI Responses API's
  own field — and concluded the page had reworded it.

  On the summary line the value keeps standing alone. There it sits among
  token figures with nothing else it could be confused for, and a 14-column
  label would be 14 more characters for the cell's ellipsis to eat. So the
  **key** carries `.list-item`, not the row — the row has to survive the
  collapse for the value to. The waterfall bar's tooltip is a summary-line
  surface too, and shows the bare value for the same reason.

  The value is not a `.mode-tag`. It takes `.row-value`, reading in the
  proportional font of the payload values around it and leaving monospace to
  the key — with the two side by side, the font is what separates them, and
  the metadata chips above stay visibly a different kind of thing.

  It had not. The two are different fields with different value spaces, and
  hermes' is the one shown: `category_profile.annotated_response.finish_reason`
  on the current exporter, `data.finish_reason` on the old envelope. The key
  is `finish_reason` and not `status` for two reasons — it is hermes' own
  word for this value on every provider route, and `status` is already taken
  here: `metadata.status == "error"` is what makes a span read as failed
  (`Span.failed`), so the word would mean two things on one span.

- **`tool_calls`** — its own detail-only row, under `response`, because both
  are what came back: the words, then the calls. Names only
  (`assistant_message.tool_calls`):

  ```
  tool_calls  skill_view · mem0_search · terminal · read_file · read_file
  ```

  **The label is not decoration.** These names used to ride the finish
  reason unlabelled, which worked while that reason *was* the word
  `tool_calls` — it named the list, and a `calls` label would have restated
  it. ADR 6's core runtime reports `complete` instead, so the row read
  `complete  read_file` on 958 of the log's tool-calling spans and nothing
  said what the names were. The old exporter's 1,008 tool-calling spans said
  `tool_calls`; the split is exactly at the changeover. A rendering that
  depended on a payload value outlived the value.

  On those older spans the word now appears twice in the detail view — as
  the `finish_reason` row's value and as this row's key — which is the cost
  of a label that is reliable rather than one that happens to be supplied by
  a neighbouring field. Accepted knowingly: it affects only spans already
  written, the two are different rows in the same column, and one of them is
  collapsed away on the summary line.

  `tool_calls` is hermes' own key and uniform across every provider route,
  so it takes the log's word (like `response`, unlike `prompt`). The wire
  spellings — `output[type=function_call]` on Responses,
  `choices[].message.tool_calls` on Chat Completions,
  `content[type=tool_use]` on Anthropic Messages — are normalized in
  `providers.py` (ADR 13) and none of them reaches this page.

  The names are a `.call-list` span, which wraps rather than ellipsizing: the
  row's point is that the calls were **one decision** — 441 assistant turns
  in the log fan out to two or more, and one to sixteen — and a fan-out cut
  off at one line does not say that. `.list-item` sits on the row, so the
  names stay off the summary line; a hidden row hides its children whatever
  class they carry, which is why the span no longer has to avoid `.path` to
  stay hidden.

  The arguments stay on the spans below, each rendered by the spec written
  for that tool; repeating them here would restate the waterfall.
  Repeats and order are kept, since both are the model's own.

  An entry that cannot be read keeps its slot (`(unreadable)` for a
  non-dict, `(unnamed)` for a missing name) rather than being dropped into a
  shorter, tidier, wrong list — the count is the point. The row also renders
  when there are calls but no reason, so those are never lost. A call with
  no matching span below is still possible; it is treated as an error, not a
  case this page detects.
- **What the call was asked** — `prompt`, detail-only, the first 400
  characters of `request_prompt`: the last user message of *this call's own*
  request, with hermes' wrappers off. `prompt` is this app's word — no
  payload key names this value, and the value is a reconstruction rather
  than the wire text, so the label is ours for the same reason the value is
  (the tooltip says as much). `response` below takes the log's own word
  because that one is verbatim.

  The token tree further down carries a `prompt` row as well, counting the
  whole request where this one shows its last message. The word means the
  same thing in both — what the model was sent — and the rows are different
  kinds: the token one is a bare key indented under `tokens`, and always has
  a figure on it. For a turn's ordinary call that is the
  prompt already at the top of the page; for every other kind it is the only
  place the instruction appears at all. A compaction's is hermes talking to
  itself (*"You are a summarization agent creating a context
  checkpoint…"*), and before this row there was nowhere on the site to read
  it.
- **What the model said** — `response`, detail-only, from
  `assistant_message.content`. Empty whenever the model was calling tools
  rather than talking, and long enough otherwise that the row shows the
  first 400 characters (`LLM_TEXT_PREVIEW_CHARS`) and an ellipsis. There
  used to be a `start of 2,579 chars` note under it, from when the rest of
  the message lived in the log and nowhere else; the ellipsis says there is
  more and the text itself opens it, so the figure was one nobody acted on.

  It carries a key for the same reason `prompt` and `tokens` do: prose
  starting mid-row with nothing in front of it has to be identified before
  it can be read. `response` is the log's own word — `annotated_response` on
  the end event, whose `message` is this same text — and the name of the
  page its icon opens.

  These keys carry `.key-col`, which reserves one column so the values start
  at the same edge — they are read down as a group, and a ragged left edge is
  what makes that hard. `finish_reason` and `tool_calls` are in that column
  too, and the values carry `.wide` as theirs do, so they share a right edge
  as well.

  The width is in `ch` and the key is monospace, so it is exactly the longest
  label plus a space — `finish_reason` at 13, hence 14ch. **A key longer than
  the column is the one row whose value does not line up**, and nothing but
  the column width prevents it, so a test asserts the reserved width still
  exceeds every label using it. That test matches `key-col` as a prefix and
  not as the whole class string: `finish_reason` carries `.list-item` beside
  it, and an exact match silently dropped the longest label from the check.

  It is not part of the spec vocabulary: a declared `Field` cannot ask for
  it, and the hand-written macro is its only user.
- **The whole of either** — an open-in-a-new-tab icon at the end of both
  rows, leading to `/turns/span/<uuid>/prompt` and `…/response`
  ([ADR 12](adr/0012-open-a-whole-value-on-its-own-page.md)). The request
  page is every message of `annotated_request` — system instructions, the
  conversation, each tool call and its result — one labelled box per message,
  where the label is this app's and the box holds nothing but what went on
  the wire.

  **Each `tool_result` is drawn inside its `tool_call`'s card**, which is not
  the order the wire sent: that sends every call and then every result, so
  the results arrived as a block with nothing tying them to the calls above.
  They are paired by `call_id`.

  The pair is one card, not two boxes: `.msg-nests` on the call drops its
  bottom border and margin, `.msg-nested` on the result drops its top, and
  both share a ground and a firmer border than a lone message's hairline.
  **That is why the result's label is a bare `tool_result`** — no name, no
  number. The card says which call it answers, and a label repeating what
  the box around it already shows is one more thing to read and to keep
  true. (Labels did carry an ordinal at first, which is what the layout
  replaced.)

  Three separations do the work, and they are three because they answer
  three different questions:

  | question | answer |
  |---|---|
  | which two boxes are a pair? | a continuous left **spine** down both halves — the accent rail sits on the box, so joining the boxes joins the rail |
  | which half is which? | both bands solid, the result's a step lighter, plus the `↳` and its indented label text |
  | where does one pair end? | **proximity** — no gap inside a pair, 2rem after it |

  **Every message label is a solid band** (`#33406b`, light text). It got
  there because the call had to be the heavier of the pair — the call is the
  parent, the result sits inside its card, and weight belongs to the thing
  that owns the other — and a call is an ordinary label, so making it solid
  made them all solid.

  ADR 12 put the labels "in the header panel's colours"; that still holds,
  but as the accent *filled in* rather than the header's tint repeated. A
  label sits directly above content competing for the eye, where the header
  sits alone at the top of the page. The accent rule moved from the label to
  the box, where it runs the message's full height and stays visible against
  the band instead of disappearing into it — and where joining two boxes
  joins their rails into the pair's spine for free.

  **Four cuts, and the measurements are the record.** Band against band:

  | | contrast | outcome |
  |---|---|---|
  | 3% lighter tint | 1.06 : 1 | one surface |
  | violet at matched luminance | 1.01 : 1 | one surface |
  | light band under a dark one | 8.74 : 1 | separated, but weight on the wrong half |
  | solid band a step lighter | 1.65 : 1 | with `↳` and indent, enough |

  The lesson in the first two: **hue without a luminance step does not
  separate two large flat areas.** The second cut moved 35° of hue and
  measured no better than the first, whose rails were the same colour at two
  lightnesses.

  The last row is lower than the third by choice. The two dark bands are
  never adjacent — the call's body sits between them — so they read as
  stripes rather than as two surfaces being compared, and a modest step does
  the job the `↳` and the indent are mostly already doing. Pushing further
  is not free: the label text is light on both bands, and by `#6b7392` it
  fails AA against its own band.

  Nothing here uses green or red: plenty of these results carry an error
  this app has not inspected, and no colour should imply an outcome it has
  not checked. The `↳` carries the relation in greyscale and for a
  colourblind reader, where none of the above would.

  The page's `note` states the regrouping in words; see "the source's order
  is the default, not a vow" in
  [design-principles.md](design-principles.md) and ADR 12's amendment.

  **A contents list runs down the left**, one entry per message, sticky so
  it stays with what it lists. A request is a dozen messages and a system
  prompt alone can be thousands of lines, so the way to the one a reader
  wants should not be the scrollbar.

  - **The entries are the section labels verbatim.** Two vocabularies for
    one page is how a nav drifts from what it names; a test asserts the
    lists match.
  - **Anchors are positional** (`m1`, `m2`), not slugs. Labels repeat —
    five `tool_result`s on the span this was built for — so a slug would
    need the position anyway.
  - **Nested results are indented in the list too**, which is what keeps a
    column of identical `tool_result` entries readable: each sits under the
    call that names it.
  - **Not shown for a single message**, which would name the thing the
    reader is already looking at, nor on a value that is not sections —
    the response page is one document.
  - **`↑ top` heads the list**, ruled off from it because it is not one of
    the messages. It is named for where it goes rather than for what is
    there: the page's own title was tried and reads as one more part, since
    every entry below it is a part of the Prompt.

    The anchor is on the **`hobserver` heading in `base.html`**, not
    on anything this page renders. Everything the page renders is below
    that heading, so an anchor on any of it lands short of the top — which
    reads as a bug rather than as a choice. A test asserts nothing renders
    between `<body>` and the anchor.

    Being on the first element is still not the top: an anchor jump pins
    that element to the viewport edge, scrolling the page's own top margin
    off above it, and a "top" you can scroll up from is not the top. `#top`
    therefore carries a `scroll-margin-top` larger than its real offset.
    Overshooting is free — a scroll position cannot go below zero, so any
    generous value clamps there, and a figure tuned to the body margin
    would need re-tuning with it.
  - Below 70rem the two columns do not fit, and the list becomes a wrapped
    block above the messages rather than a squeezed column beside them.

  Anchor jumps animate (`scroll-behavior: smooth`, off under
  `prefers-reduced-motion`). One click here can move a reader thousands of
  lines, and an instant cut leaves them unsure whether the page moved or was
  replaced — the scroll is what says which.

  **The messages carry no reading-measure cap.** They fill the column beside
  the contents list, out to the same gutter the page keeps on the left. The
  62rem the rest of the app reads at is for prose; a message box holds JSON,
  tool arguments and file contents far more often, and for those the width
  is what saves scrolling — the cap left a third of a wide window empty.
  `.full-sections` bounds them instead, and its `min-width: 0` is what keeps
  one long unbroken line from pushing the column past the page.

  The header keeps step: `.full-head.wide` on a sections page, because a
  panel narrower than what it heads reads as a mistake rather than as a
  measure. Over a value that is one document — the response page — the prose
  keeps 62rem and so does the header.

  The response page is the assistant message whole. Both are declared as
  `Full`s on the llm scope, which is keyed by *category*, so a compaction, a
  delegated subagent call and a fallback retry each carry the same pair.

  The icon does not wait for a value to be truncated: a reader cannot see
  that a value fits, only that it ends. The response icon is absent only
  when the model said nothing at all, since then there is nothing behind it.

  On these two rows the **whole excerpt is the link**, not just the glyph —
  clicking anywhere on the text opens it. The key beside it stays outside
  the anchor: it names the row, it is not a way into it. The text keeps the
  row's own colour rather than a link's, since a paragraph of someone's
  prompt in link blue would read as something quoted from elsewhere; the
  hover shade and the glyph are what say it is a link. A declared `Field`
  with `full=` still gets the glyph alone, because its value may carry a
  copy button and an anchor holding a button is not markup worth writing.
- **Tokens** — `usage`, as a tree (`TOKEN_TREE`), one row per count. In the
  detail layout:

  ```
  tokens
      prompt 19,349 (93% cached)
          cache read 17,920
          in 1,429
          cache write 4,096
      out 1,089
          reasoning 303
  requests 1
  ```

  and on the summary line, where the parts are hidden and a `·` divides what
  is left from the finish reason:

  ```
  complete · prompt 20,193 (89% cached) · out 84
  ```

  The reason is bare here — its key is detail-only, per the row above.

  `cache read` leads the prompt's rows: how much of it the provider already
  had is the question they are usually being read to answer, and `in` reads
  as the remainder — what was new — once it follows.

  **There is no `total` row.** It would be `prompt + out`, both of which are
  here, and it was the one figure the tree could not vouch for — see the
  relations below. The `tokens` label heads the tree in its place, carrying
  no number of its own; it is detail-only, because beside a figure already
  named `prompt` it tells a summary-line reader nothing.

  The indent is arithmetic, not decoration — and both relations behind it
  now hold firmly:

  | relation | strength |
  |---|---|
  | `cache read + in + cache write == prompt` | structural — whichever of `prompt` and `in` the producer did not report is *defined* as this relation by `atof_reader`, so it cannot diverge |
  | `reasoning <= out` | observed, not enforced — hence the marking below |

  (The dropped `total` was the exception: `prompt + out == total` held
  throughout the log but was never guaranteed, because hermes' codex path
  prefers the provider's reported total over the computed sum
  — `codex_runtime.py`. Nothing on screen depends on that agreement now.)

  So the rows under `prompt` are its parts by construction. `reasoning` is
  counted *within* `out` rather than alongside it, and is marked `.tok-part`
  precisely because it must not be added to its siblings. Its tooltip is what
  says so — the class shades nothing.

  `out − reasoning` is **not** the reply. It is everything else the model
  emitted — the reply *and* the arguments of any tool calls — and on most
  calls here it is mostly the latter: 907 of 1,129 llm scopes in the log
  made tool calls, and those usually carry no message content at all, so
  hundreds of output tokens went on arguments that this span deliberately
  does not list (they are on the spans below). Both tooltips say so, because
  the obvious reading of a `reasoning` row nested under `out` is that the
  remainder is what you read.

  **The cache share** — `(93% cached)` — rides the `prompt` figure in both
  layouts. On the summary line it is the only sign the cache was involved at
  all, `cache read` being detail-only; in the detail layout it sits directly
  above the two rows it is read off, where it can be checked. Rounded to
  whole percent, half up rather than to even, since a reader comparing rows
  expects `.5` to go up.

  It reads with the figure it rides — same font, same colour, sharing
  `.row-value`'s rule rather than restating it. It was faint monospace when
  every token row was; keeping it so once the figures became values would
  have set one part of `prompt 20,193 (89% cached)` in a different hand, for
  a distinction a reader has no use for — both halves are what the prompt
  cost. It keeps its own class name because it is a different thing to find
  in the markup, not because it looks different.

  Its gap from the figure used to be stated in
  `base.html` as a `1ch` collapsed margin, matching the space inside a
  single-span `prompt 20,193`; now that the row has its own key/figure gap,
  both come from the literal spaces in the template — ignored between flex
  items in the detail layout, rendered when the row goes inline — so one
  mechanism keeps them equal instead of two being kept in step by hand.

  It never reads `100% cached` unless every last token was: straight
  rounding puts 27 of the 1,129 calls in the log at 100 with hundreds of
  tokens still fresh (129,536 of 130,361 is 99.37%), so the figure caps at
  99 unless `cache read == prompt` exactly. One percentage point of
  imprecision beats a figure that claims a whole prompt was cached when it
  was not.

  `0% cached` **is** shown — a cold prompt (the first call of a session,
  say) is a fact worth stating, and every llm row then reads the same shape
  whether or not the cache was involved. But *absent* is not *zero*: a
  payload that never reported `cache_read_tokens` gets no share at all,
  because this app cannot tell nothing-cached from nothing-said. Every one
  of the 94 cold calls in the log reports the zero explicitly.

  **`cache read` and `in` keep their rows at zero** (`ALWAYS_SHOWN`), where
  every other count loses one. They are the split the share is read off, and
  the detail view is where a reader goes to see it; an absent row leaves
  them working out whether the figure is zero or was never reported. So a
  cold call has the same shape as a warm one, differing only in the numbers:

  ```
  prompt 18,824 (0% cached)        prompt 20,193 (89% cached)
      cache read 0                     cache read 17,920
      in 18,824                        in 2,273
  ```

  `prompt` and `in` then carry one figure an indent apart. That repetition
  is the price of the split always being visible, and `cache read 0` beside
  it is what makes the pair say something a lone `prompt` would not.

  `cache write` is **not** in that set, and its zero is still dropped — see
  below for why its zero is not a reading. Every other count keeps the plain
  rule: zero, no row. Because the leaves partition their parent, dropping
  one still leaves what is shown adding up.

  `cache write` is 0 on every call in the log today, and on the codex route
  that is hard-wired rather than a property of the provider: hermes'
  `codex_runtime.py` builds its usage with `cache_write_tokens=0` outright,
  bypassing the normalizer that would have looked for the field. That is why
  it is not in `ALWAYS_SHOWN` while `cache read` is: a `cache read 0` is
  something the provider reported, but a `cache write 0` would be this app
  asserting a measurement nobody took. The field is filled for real on the
  Anthropic route, from `cache_creation_input_tokens` — the part of a prompt
  the provider stored for later calls to read.

### How the two layouts are split

`.list-item` is this app's detail-only marker (`base.html`) — one rule hides
every element carrying it on a collapsed row. So the split is made by which
rows get the class, not by a mechanism of their own:

- `prompt` and `out` (`summary` in `token_rows`) omit it and survive the
  collapse, `prompt` taking its cache share with it; the `tokens` label, the
  parts and `requests` carry it.
- `requests` is top-level but stays detail-only: it is a count of API calls,
  not of tokens, and does not earn a place on a line being scanned.
- The whole tree inlines onto one clipped line when collapsed, so five more
  labelled figures would be five for the ellipsis to eat — and a clipped
  count reads as a real one.

A `·` divides the buckets from the finish reason and from each other. It is a
real `.gen-key` span the template puts inside the row — the same element
`payload_rows` uses for its own separators, so the two are the same glyph in
the same font at the same colour. A pseudo-element was tried first and was
visibly wrong twice over: it inherited the body's proportional font, where a
middle dot is a lighter mark than the monospace one beside it, and the row's
`margin-left` sat outside it, leaving a wider gap before the dot than after.
A row carrying a dot now takes `.tok-sep`, which drops that margin, so the
dot has one plain space either side.

The template decides where dots go, tracking whether anything precedes — so a
row with nothing to its left never gets a leading one.

Tokens are on the row rather than in the bar's tooltip because a cache read
can be most of a prompt, and is often the difference between a fast call and a
slow one — worth seeing on the span, not on hover.

The bar's tooltip still repeats the two buckets (`Span.usage_summary`, built
from the `summary` rows so it can never disagree with them), and says nothing
at all when neither was reported. It used to interpolate a `?` for a missing
figure, which read as a count the provider had withheld rather than one this
app had gone looking for in the wrong place — which, for every
chat-completions call, is exactly what had happened.

**Which figures are reported and which are derived is per-provider**, and
the intuitive reading — leaves reported, parents summed — does not hold.
OpenAI-shaped payloads report `prompt` and this app derives `in` by
subtraction; Anthropic-shaped ones report `in` and it derives `prompt` by
addition. Either way the derivation runs only where the cache read was
reported, so a route silent on caching shows `prompt` and `out` alone. A
missing row means the payload did not say, which is not the same as a zero
— see [atof-reader.md](atof-reader.md) for the mapping.

**Where the counts come from is per-route, and not always the end payload.**
`openai_responses` (openai-codex) reports usage on the llm span's end event;
`openai_chat` (openrouter, and so every non-codex model) reports it only on
the last `llm.chunk` of the stream and leaves the end event's `usage` null.
`Span.usage` reads the end payload first and falls back to what the index
kept from that final chunk, so the tree above looks the same either way.

An llm payload the reader presents carries `usage`, `model`,
`finish_reason` and `assistant_message`, so anything a row says beyond
those has to come from somewhere other than the payload — the `.mode-tag`
role and retry above come from the span's metadata.

## Unrecognised scopes

Everything below is a spec written against a hermes tool this app knows. A
scope with no spec — another hermes user's tools, or one added since — falls
back to rendering its start payload directly, so it shows what the call was
for rather than a bare name and duration. Marks with no branch do the same.

This is the default, not a failure: it is also where a scope lands whose spec
resolved to nothing, or whose spec raised. A contributed spec that is wrong
costs its own rows and nothing else — a turn page holds many spans and polls
every 2 s, so one bad entry must never take the page down.

The rules (`generic_payload_fields` in `plugins/turns/atof_reader.py`):

- Keys come in payload order, which is the tool's own argument order.
- The first three scalars ride the summary line; every key gets a detail row.
- A value over 2 KB is named and measured — `conversation_history: list, 412
  items (7.3 MB)` — never printed. Payloads reach megabytes (a turn mark
  carries the whole conversation history), and a live turn page refetches
  itself every 2 s. A scope with a spec can put such a value behind a `Full`
  instead (below); the generic fallback deliberately does not, since that
  would mean addressing a value by payload key rather than by a name its
  scope declared — see
  [ADR 12](adr/0012-open-a-whole-value-on-its-own-page.md).
- Correlation plumbing is skipped (`session_id`, `turn_id`, `tool_call_id`,
  `telemetry_schema_version`, …): it is already on the row or of no interest.
- So is anything whose key looks like a credential (`headers`,
  `authorization`, `token`, `api_key`, …). Nothing fills those keys today —
  hermes' llm spans carry an empty `headers` dict — but a generic renderer
  must never be what puts a bearer token on a screen.

A scope with a spec never collects these rows: the fallback runs only when the
lookup finds nothing, so a declared rendering always wins.

## Writing a spec

The vocabulary is below. For getting a module of your own loaded — from a
package installed anywhere — see
[writing-a-scope-spec.md](../extending/writing-a-scope-spec.md), which works one through
end to end.

A spec is a list of row descriptors. The vocabulary is four orthogonal axes,
and a scope that seems to need a fifth usually wants `render=` instead:

| axis | on | what it decides |
| --- | --- | --- |
| `font` | `Field` | `<span>` or `<code>` |
| `clip` | `Field` | how it survives a narrow line |
| `deco` | `Field` | what kind of thing it is, which picks the class |
| `layer` | `Row` | which layout the row belongs to |

**The values each one takes, and every other parameter, are documented on the
classes themselves in `plugins/turns/scope_spec.py`** — that is the
reference, and it is what an editor shows you while you type. This section is
the shape; the docstrings are the detail.

A `Field` reads either a payload key (`Field(payload("command"))`) or a named
value of the span (`Field("command")`). Use the payload for a lookup, and a
named value where the reading does real work — `patch_mode`'s inference,
`memory_ops`' normalization of two payload shapes.

A bare string resolves against the **span readers** first and the `Span`
object second ([ADR 17](adr/0017-a-payload-reading-is-contributed-beside-the-spec-that-names-it.md)).
A reader is `fn(span) -> value`, contributed as `SPAN_READERS` beside the
`SCOPES` that name it, and it gets the whole span — both payloads, the
metadata, the timings. That is what lets a whole tool come from outside this
tree: `payload=` covers the keys, readers cover everything that has to be
worked out. A reader of the same name stands in front of a `Span` property,
and the startup line says so.

`plugins/mem0/spans.py` is the worked example: mem0's payload shapes are read
by the mem0 tab, not by this one, and go away with it.

Row kinds: `Row` (fields on a line), `Diff` (a − / + pair), `Items` (a list as
first-plus-count then one row each), `Each` (rows repeated per list entry,
with `item("key")` reading the entry and `Row(when_many=True)` holding back a
label a lone entry does not need). `Alt` picks the first field that resolves.

A scope may also declare the **complete values** behind its excerpts, each of
which becomes a page of its own
([ADR 12](adr/0012-open-a-whole-value-on-its-own-page.md)):

```python
Scope(rows=[Row([Field(payload("output"), full="output")])],
      fulls=[Full(key="output", source=payload("output"), render="markdown",
                  title=const("the whole output"))])
```

`fulls` is declared on the `Scope` and named by key from wherever the excerpt
appears — a `Field(full=…)`, or by hand from a `render=` macro. One
declaration, because the icon and the route that serves it have to agree; a
`Field` naming a key that is not declared is reported at load. The icon is
drawn whether or not the excerpt was cut short.

Two rules the machinery enforces so a spec cannot get them wrong:

- **A row with nothing on it does not render.** Every field resolving to
  nothing means no row, so a spec never emits an empty line for an absent key.
- **Absent is not empty.** A payload key present with `""` is a value — a
  patch that deletes its matched text passes one — so a `Diff` shows that
  side. A `Field` still drops it, because there is nothing to put on a row.

## The one exception

**llm** keeps hand-written Jinja, in `plugins/turns/templates/turns/_scope_llm.html`,
reached by `render="llm"` in the spec table and dispatched from the turn
page. Its token tree runs a separator state machine — tracking whether
anything precedes a row so one with nothing to its left takes no leading `·`
— and that is not a shape the vocabulary should learn.

ADR 7 shipped three. The other two were mem0's, and were hand-written only
because that plugin lives in this tree: what they needed was a link into
another tab and a value from its published accessor, which
[ADR 9](adr/0009-scope-specs-may-link-and-read-published-data.md) put in the
vocabulary as `Link` and `accessor()`. Both scopes are now declared like any
other — in `plugins/mem0/scopes.py`, the plugin that owns them, contributed to
this tab when that one loads
([ADR 10](adr/0010-a-tab-contributes-its-own-scope-specs.md)) — and anyone
can write their own the same way, see
[writing-a-scope-spec.md](../extending/writing-a-scope-spec.md).

A second exception would mean the same question is worth asking again.

## Scopes

### terminal — `command` / `workdir`

Command in monospace, workdir with the home prefix collapsed to `~`. The
command wraps in full in detail mode with line breaks kept — code takes
`pre-wrap`, unlike the `normal` wrap prose details use — so a heredoc or a
multi-command script stays readable.

### file tools — `path`

Left-ellipsized (`.tail`) so the filename end survives a tight line.

### search_files — `pattern` / `file_glob` / `path`

Pattern in monospace, then the glob and the search path.

### patch — two modes

Checked against `patch_tool` in `$HERMES_SOURCE/tools/file_tools.py`. One spec,
named by a `.mode-tag`:

- **replace** (the tool's default) — `path` plus `old_string`/`new_string`.
  Its own spec names both, so it no longer depends on being matched before
  the plain-path scopes (`read_file`, `write_file`) it once fell into.
- **patch** — a V4A multi-file `patch` text and no top-level path.
  `Span.patch_paths` lists the files from its `*** <Op> File:` headers: first
  path plus a "+N more" count.

`Span.patch_mode` prefers the payload's explicit `mode`, falling back to the
keys present (a `patch` text ⇒ patch, else the default, replace).

Detail mode adds what the summary line cannot carry: the two replaced sides
for replace, the whole V4A text for patch. Never the end payload's
`diff`/`files_modified`/`lint` — nothing reads those today.

### web_search, mem0_search — `query`

Monospace; wraps in full in detail mode instead of ellipsizing. mem0_search
also renders what came *back*, see
[mem0_search results](#mem0_search-results).

### session_search — four modes

Checked against `$HERMES_SOURCE/tools/session_search_tool.py`, rendered
mode-aware in one spec.

`Span.session_search_mode` prefers the end payload's explicit `mode`, falling
back to inferring it from the start-payload keys using the tool's own dispatch
precedence — an `around_message_id` anchor ⇒ scroll, else a `session_id` ⇒
read, else a `query` ⇒ discover, else browse — so a still-open span resolves
too. A muted `.mode-tag` names it.

`Span.session_search_summary` is the inline one-liner from the start payload:

| mode | summary |
| --- | --- |
| discover | the query |
| scroll | `session <id> · around msg <n> · window <w>` |
| read | `session <id>` |
| browse | `recent sessions` |

`Span.session_search_stats` is a list of {label, value, tooltip} rows from the
end payload, shown on detail-only `.list-item` rows, one per count the mode
reports:

- **discover** — `count` (sessions actually returned; lower than searched when
  a match is title-only or its anchored view cannot be built) and `sessions
  searched` (distinct matching sessions, deduped by lineage and capped at the
  limit — **not** the corpus scanned, so count ≤ sessions_searched ≤ limit).
- **scroll** — `before` / `after`: messages outside the returned window.
- **read** — `messages` total, plus a `truncated` flag.
- **browse** — `sessions` listed.

Each label's tooltip carries the same explanation.

### web_extract — `urls`

First url plus a "+N more" count inline; every url on its own line in detail
mode.

### execute_code — `code`

First line inline, the whole program in detail mode.

### vision_analyze — `image_url` / `question`

The image path or URL left-ellipsized in the summary; the question added on
its own line in detail mode only.

### mem0_add, mem0_update, mem0_delete — the fact and the id

The four mem0 tools are defined in
`$HERMES_SOURCE/plugins/memory/mem0/__init__.py`, i.e. inside hermes' *memory
plugin*, not in `$HERMES_SOURCE/tools/` where the rest live. Do not confuse
them with the [`memory` scope](#memory--the-in-prompt-stores), which is a
different tool.

mem0_add carries the fact as `content` (plain text, not monospace); mem0_update
carries it as `text`.
`Span.memory_content` reads whichever key the scope uses, so the fact leads
the summary line either way, and wraps in full in detail mode.

mem0_update and mem0_delete also carry a `memory_id`, kept to a detail-only
row (faint mono, copy button — the lookup key back to a mem0_search result).
On the summary line it reads as a second uuid beside the span's own; expanding
the row is enough to reach it.

#### The previous text

An update and a delete both show what the memory said *before* the change,
recovered from the local event log by `memory.prior_memory_text` (ADR 4 covers
why it is an `app.extensions` accessor rather than a link; `prior_memory` in
the turns turn view holds the per-span map). It renders through the same
`diff_rows` macro the patch scopes use — an update gets − old / + new, a
delete only the − side — under a muted `.prov` row naming the source.

mem0 itself is never queried and could not answer: hermes' `Mem0Backend`
exposes only search/add/update/delete (no get, no history), and the platform
cannot return a deleted memory at all.

The log can answer because a mem0_search result carries each hit's text beside
its id, and the agent can only learn an id *from* a search — so every change
is preceded by the search that surfaced it — in practice within the same
session, seconds to minutes earlier.

It is therefore the memory as of that search, not a guaranteed pre-change
snapshot. The row and its tooltip say the text is from the local log, name the
search event and the gap, and state that a change made outside hermes in
between would not show. **Never present it as something mem0 vouched for.**

#### Why a delete leads with it

A mem0_delete *leads* its summary line with the recovered text, the way an add
leads with its `content`: the id is the delete's whole payload, so the row
would otherwise say nothing about what was destroyed even before being opened.

That line is `.list-compact`, so detail mode drops it in favour of the − row
carrying the same text in full. An update's summary line is its own new `text`
and stays put — the old text is the − row's job, and displacing the new one
would hide what the span actually wrote. Deletes whose text was never
recovered fall back to showing only the id.

### memory — the in-prompt stores

The other memory tool (`$HERMES_SOURCE/tools/memory_tool.py`): bounded
§-delimited entries in two char-limited files under `$HERMES_HOME/memories/` —
MEMORY.md (the agent's own notes, `target` "memory") and USER.md (who the user
is, `target` "user"). They are injected into the system prompt as a snapshot at
session start, where mem0 is searched on demand.

Because the stores are small (1375 chars for user by default) most writes are
entries being shortened to fit, and failing the budget and retrying within the
turn is routine rather than exceptional.

Two payload shapes, dispatched in this order: an `operations` list of
{action, content?, old_text?} applied atomically, else a single top-level
{action, content?, old_text?}. `Span.memory_ops` normalizes both to one list.
`Span.memory_action` is the mode tag — "batch" whenever `operations` is
present (it wins over an explicit `action`, matching dispatch, and a staged
batch replayed from the approval queue carries both).

`old_text` is a short unique substring the tool matches by containment, **not**
an id.

Rendering: a `.mode-tag` and the store name always visible, the first op's text
(content, or old_text for a remove) plus "+N more" inline, then per op the same
`diff_rows` macro the patch scopes use — an add shows only +, a remove only −,
a replace both — with a per-op action label only when there is more than one,
since the mode tag already names a lone op.

#### The entry behind the fragment

The − side is `old_shown`, not `old_text`: the log holds the fragment the
tool matched on, and the reader wants the entry it matched. `Span.memory_ops`
carries both, plus the note that has to appear with them —
`assembler.resolve_memory_entries` fills them in and
[ADR 16](adr/0016-recover-a-matched-store-entry-from-a-listing-in-the-same-turn.md)
is the reasoning:

| key | what it is |
|---|---|
| `old_text` | the fragment, exactly as the payload has it |
| `old_entry` | the whole entry it resolved to, or None |
| `old_shown` | the − side: the entry where there is one, else the fragment |
| `old_entry_note` | the `.prov` row — provenance when resolved, the reason when not |

The evidence is `current_entries`, which only a rejected write returns (see
below), so a listing is available exactly where consolidation happens. It
answers for the call that returned it and for later calls to the same store,
until a write **lands** — a success reports its char count, never the store,
so the listing is dropped there rather than replayed forward. Inside one span
the ops *are* replayed, since a batch is atomic and every op is on the span.

Unresolved is stated, never silent: "no entry in the store listing 800 ms
earlier in this turn contains this text", or "2 entries … — not resolved to
one". A fragment that is already the whole entry adds no note at all, having
nothing to add.

This is the one scope whose *end* payload is rendered as a matter of course,
because the char budget is the whole story of the tool:

- `Span.memory_stats` — `usage` and `entry_count` on detail-only rows, shown
  verbatim (the tool writes "97% — 1,335/1,375 chars" on success and a bare
  "1,338/1,375" on a rejection).
- `Span.memory_current_entries` — the whole store, which a rejection hands back
  for the model to consolidate before its own entry will fit; its error says
  "see current_entries below". Detail-only: it is the store's state, not this
  write. It is also the evidence the section above resolves fragments
  against.

### todo — `todos`

First item's content plus a "+N more" count inline; every item on its own line
in detail mode. A todo call *without* `todos` is a read of the current list and
shows nothing.

### delegate_task — `tasks` / `goal` / `context`

First goal plus a count inline; in detail mode every goal in full with its
`context` nested fainter and indented beneath. Covers both shapes: a batch
`tasks` list and a single top-level `goal`/`context`.

`hermes.subagent.start` marks show their `child_goal` the same way;
`subagent.stop` marks show `child_status` (e.g. timeout) plus the echoed goal,
with the session id and `duration_ms` on a detail-only line. Both carry a
per-turn #N tag (in start order) pairing start with stop, since
`child_session_id` is the only key both marks share.

### skill_view, skill_manage — `name` / `file_path` / `action`

Plus, on skill_manage:

- `category`, which a "create" carries — shown before the skill name,
  middot-separated. Absent on patch/write_file, which have no category.
- `absorbed_into`, which a "delete" that merged the skill elsewhere carries —
  rendered "→ absorbed into \<skill\>".
- `old_string`/`new_string`, which a "patch" carries. Note skill_manage patches
  replace text; they never carry a V4A patch text the way the file tools' patch
  scope does in patch mode. The pair matches that scope's *replace* mode
  instead, and both render through the one `diff_rows` macro in
  `plugins/turns/templates/turns/_macros.html`.

The two sides render on their own detail-mode-only rows (`.list-item`), marked
− and + — glyph first, tint second, never color alone. `new_string` may be the
empty string, a patch that deletes the matched text (both tools document
passing "" for that), so `Span._new_string` — shared by `skill_new_string` and
`patch_new_string` — reads the payload directly instead of `_start_str`, which
folds "" into None; the empty side renders in words. They stay out of the
summary line because a real `new_string` runs to kilobytes of markdown.

skill_manage's six actions are create/edit/patch/delete/write_file/remove_file
(checked against `$HERMES_SOURCE/tools/skill_manager_tool.py`). Only create,
patch and write_file have turned up in practice so far, so the other three are
covered by test alone.

`file_path` (skill_view, skill_manage write_file and remove_file, and an
optional patch target) is middot-separated from the skill name and
left-ellipsized (`.tail`, like the file tools' path).

## Failures

Every hermes tool reports a failure the same two ways: `metadata.status` is
"error" on the end event, and the end payload carries an `error` string. That
is how the tools in `$HERMES_SOURCE/tools/` report errors, and has held for
every failing call seen here (terminal, patch, read_file, search_files,
write_file, execute_code, web_search, skill_view, skill_manage, memory), so
`Span.failed` and `Span.error` are one generic pair rather than a reader per
scope, rendered after the scope's own rows as a `badge-error` beside the name
plus an `.err` row.

Both stay out of detail mode: failures are common enough — some tools fail
more often than they succeed — that noticing one must never need a click.

## mem0_search results

`mem0_results` reads the end payload —
`{"count": n, "results": [{id, memory, score}, …]}` — which arrives as a JSON
string ranked by score descending. That shape has been uniform in practice
and is still read defensively: payloads are opaque per the ATOF spec.

It is a **span reader** in `plugins/mem0/spans.py`, not a `Span` property:
the shape is mem0's, so the reading belongs to mem0's tab
([ADR 17](adr/0017-a-payload-reading-is-contributed-beside-the-spec-that-names-it.md)).
Turn the Mem0 tab off and these spans render as a payload dump, rows and
reading together.

Only the top three render, on detail-only rows: whole facts would swamp the
summary line. How many is bound once, as `shown` in the template — the same
slice decides whether the link below reads "all N results" or "full result",
so a preview that already covers everything never promises more behind it.

Each hit's score leads its row, then the fact; the memory id sits on its own
faint `.mem-id` row, the same treatment mem0_update/mem0_delete give theirs.
Those spans' ids are a lookup key *for* these, so the pair resolves without
leaving the UI.

### The link to the Mem0 tab

A last row links to the whole ranked list in the Mem0 tab
(`Span.mem0_result_count` names it: "all 10 results"). This is the one place
two plugins meet, and they share no key — ATOF carries no event id, the db
carries no span uuid.

`mem0.search_event` therefore matches on (session_id, query), which has
resolved to exactly one event for every span checked. An optional `ts` (the
span start, in epoch µs) breaks the only tie possible: the same query twice in
one session.

It is a redirect owned by the mem0 plugin, not a lookup at render time — the
Turns tab never opens the event log, and a turn page polling every 2 s costs
no queries. Unmatched requests 404 saying so: the two logs are written
independently, so either can cover a call the other misses.

## Reading hermes' payloads

Paths above are relative to `$HERMES_SOURCE`, the hermes-agent source
checkout — not `$HERMES_HOME`, which is hermes' config directory and the one
this app actually reads. The rules for
reading a payload — check the tool's signature rather than the log, read
defensively anyway, never assume the `webui` platform — are in
[design-principles.md](design-principles.md#3-reading-hermes-payloads).
