# Design principles

Standing commitments that outlive any one feature. **Read this before
designing a change**, not after — most of it is about shape, which is
expensive to retrofit.

An ADR decides one question once. A principle here is what the ADRs keep
agreeing on. Where the two meet, the ADR is the record and this file is the
pointer: nothing here restates an ADR's reasoning.

## 1. Extension without a fork

**Someone else should be able to run hobserver against their hermes,
display their own tools and stores, and carry no patch on top of this tree.**

hermes' tool set differs between users and versions, and its frontends differ
too. This app is therefore never the authority on what it will be asked to
show. Anything shaped as "the list of things we support" is a fork waiting to
happen — [ADR 5](adr/0005-tabs-are-configured-plugins-loaded-by-module-path.md)
removed one such list (the `PLUGINS` tuple),
[ADR 7](adr/0007-declare-scope-rendering-as-row-specs.md) another (the span
branch chain),
[ADR 13](adr/0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md)
a third (the provider token shapes, which had silently become "the two APIs
we have seen").

### The fork test

For any new extension point, ask: *someone has a tool, store or log this app
has never heard of — what do they do?* If the answer contains "edit a file in
this tree", the design is not finished.

Apply it to the whole path, not the entry point. It is easy to make
registration pluggable and leave a fork one level down — ADR 7's first draft
let a stranger register a scope spec but not read a payload key without
patching `assembler.py`, which is the same fork with an extra step.

### The ownership test

The fork test asks whether a stranger can reach in. This one asks the prior
question: *whose knowledge is this, and which module owns it?*

**Every foreign system this app reads gets exactly one module that knows its
shape — and that module is where its extension point lives.** Ask of any
payload rule: *if this system changed, which single file would I open?* If
the honest answer is "several", or "whichever one happens to touch that
payload", the knowledge has no owner and will drift.

| foreign system | owner | extension point |
|---|---|---|
| hermes' tools | `plugins/turns/scopes.py` | `scope_specs`, and a tab's own `SCOPES` — [ADR 7](adr/0007-declare-scope-rendering-as-row-specs.md), [ADR 10](adr/0010-a-tab-contributes-its-own-scope-specs.md) |
| LLM provider APIs | `plugins/turns/providers.py` | `provider_specs` — [ADR 13](adr/0013-provider-payload-reading-is-its-own-module-and-token-shapes-are-published.md) |
| another log or store entirely | its own tab | `hobserver.toml` — [ADR 5](adr/0005-tabs-are-configured-plugins-loaded-by-module-path.md) |

The two tests fail differently, which is why both are worth running. Failing
the fork test is visible: someone asks for a patch. Failing the ownership
test is silent — knowledge of one system spreads across modules named after
something else, and nothing marks it as contingent. ADR 13 is the worked
example: reading OpenAI's token names lived in a file named for the ATOF
envelope, so *"is this true of every provider, or only the one we have
seen?"* was a question with no home. It was not true of every provider, and
the wrong number went unnoticed until a second router turned up.

A useful tell: **prose about a foreign system may live anywhere; a branch on
one may not.** `assembler.py` explains in a comment why `cache write` is not
in `ALWAYS_SHOWN` (hermes hard-codes it to zero on the codex route) — that
is a display rule reasoning about provider behaviour, and it belongs with the
display. A `if provider == ...` in the same file would not.

### What it commits us to

- **Reach the source, not just our accessors.** An extension must be able to
  read the underlying payload/row/file directly. Curated accessors
  (`Span` properties, and the like) are a convenience for in-tree code, never
  the only road in.
- **One loading mechanism.** `hobserver.toml`: an importable `module` path plus
  an opaque `settings` table, in-tree and installed modules loading
  identically. Do not add a second discovery route beside it — ADR 5 declined
  entry points so there is one place to look when something does not appear.
- **Publish a surface deliberately, then keep it.** What an extension may
  depend on is a named list, not whatever happens to be importable —
  `base.html`'s classes, the scope-spec vocabulary, the tab contract
  ([ADR 8](adr/0008-plugins-may-import-published-host-vocabulary.md)).
  Adding to it is a commitment; the failure is publishing something by
  accident. Note that an import is not the only way to couple: a class name
  in a template binds just as tightly and shows up nowhere in an import list.
- **User definitions override ours.** The in-tree table is a default, not a
  floor. Someone whose payload differs from hermes' replaces our handling of
  it; the override is announced at startup rather than silently applied.
- **Degrade to the generic, per component.** A broken or absent extension
  falls back to the plainest rendering that still says something true, reports
  why at startup, and never takes down a page or the app. Only a total failure
  (no tab at all) is worth exiting for.
- **Config over code for the operator; code for us.** What tabs load and how a
  scope displays is a stranger's choice, expressed as data. It is not a knob
  in `hobserver.toml` for the person merely *running* hobserver — that file
  wires tabs, and app structure does not belong in it.

### What it does not mean

- **Not everything must be pluggable.** The principle applies where hermes'
  variability reaches us: tools, scopes, stores, log dialects. It is not a
  case for abstracting the page layout, the polling, or the shell.
- **Escape hatches stay internal.** Where we keep hand-written code for our
  own hard cases (ADR 7's `render=`), that is legitimate and stays out of
  reach. The rule is only that it must not be the way to do *ordinary* things.
- **Pluggable is not unvalidated.** An extension is imported code and no less
  trusted than a tab, but prefer forms whose reach is narrow by construction —
  data interpreted by us beats a callback that renders itself.

### The cost, and where we are with it

Every extension point published becomes an interface: versioned with
`PLUGIN_API`, and breaking it breaks a stranger's work. So open the smallest
vocabulary that does the job.

**This is provisional until the first public release.** Nothing outside this
tree depends on these surfaces yet, so vocabulary — ADR 7's four rendering
axes especially — is still free to churn, and should be shaken out while that
is true. After release it is not free, and the cheap moment to get it right
will have passed.

## 2. Commitments decided in ADRs

Decided in full in their ADRs; listed here so a design review meets them.

- **Read-only over someone else's log.** The browser never owns or mutates
  data, opens no authenticated network calls, and stores nothing derived —
  [ADR 2](adr/0002-read-atof-jsonl-directly-no-etl.md), bent knowingly twice:
  by [ADR 6](adr/0006-parse-atof-by-declared-schema-era.md), which says where,
  and by [ADR 11](adr/0011-index-the-atof-log-rather-than-hold-it-in-memory.md),
  which does store something derived — an index of the log, rebuildable in
  seconds from it, holding no fact the log does not, and never the thing a
  reader is shown. Anything with the shape "a cache of a source we do not
  own" belongs to that ADR and inherits its rule: **discard on any doubt,
  never migrate.**
- **Fail loudly, never quietly wrong.** An unreadable source, an unknown
  schema or an unresolvable link says so on screen; parse errors and
  anomalies are surfaced, never dropped — ADRs 2 and 5.
- **Plugins share nothing but a link or a published accessor**, and never open
  each other's sources —
  [ADR 4](adr/0004-cross-plugin-access-by-link-or-published-accessor.md).
- **Derived data says where it came from.** Anything reconstructed across
  sources names its source and its age on screen, and is never presented as
  something the origin vouched for — ADR 4;
  [ADR 12](adr/0012-open-a-whole-value-on-its-own-page.md)'s `Full.note`,
  which carries the same rule onto a page that is nothing but one value; and
  [ADR 16](adr/0016-recover-a-matched-store-entry-from-a-listing-in-the-same-turn.md),
  which adds its converse — where the reconstruction fails, the page says
  that too, rather than quietly showing less.

## 3. Reading hermes' payloads

This section is the record; nothing else restates it.

**Check a payload against the tool's own signature in the hermes source, not
only against shapes seen in the ATOF log.** The log shows what has been
exercised so far, which is not the same as what the tool can emit.

Paths are relative to `$HERMES_SOURCE`, **the hermes-agent source checkout** —
wherever you cloned it. It is not `$HERMES_HOME`, which this app reads at
runtime and which points at hermes' *config* directory (`nemo-relay/`,
`memories/`, `jmem0_logged.db`). Source is where the tools are defined; home
is where they wrote. Nothing in this app resolves `$HERMES_SOURCE` — it is
notation for a reader going to look something up, not a variable to export.

- `$HERMES_SOURCE/tools/` — most tools.
- `$HERMES_SOURCE/plugins/memory/mem0/` — the four mem0 tools, which live in
  hermes' memory *plugin* rather than with the rest.

**Read payloads defensively regardless.** ATOF payloads are opaque by
specification; a shape that has been uniform for a whole log is still not a
contract. See [docs/atof-reader.md](atof-reader.md) for the mapping and the
traps.

**A fact absent from the event you expected it on may be reported somewhere
else entirely.** Not only the names differ per provider route — the
*location* does. Token usage is on the llm span's end payload for
`openai_responses` and only on the last `llm.chunk` of the stream for
`openai_chat`, where the end payload says `usage: null`. Before concluding a
producer does not report something, look for it on the other events of the
same call; hobserver showed no token counts for every openrouter turn
while the log held all of them.

**Anything you learn here about one provider goes in `providers.py`**, not in
the module that happened to need it — see the ownership test in §1. The rules
above are about reading a payload; that one is about where the reading lives.

**Never assume the `webui` platform.** The `platform` kwarg is `webui` today,
but hermes has other frontends — a TUI among them — and the value space is
open.

## 4. On screen

**Identity is never carried by colour alone.** A colour always has text
beside it.

**The source's order is the default, not a vow.** Show things in the order
the log holds them, because that order is usually itself a fact — what the
model was sent, when a span ran. Depart from it where grouping genuinely
helps a reader, and when you do, **say so on the page**, not only in a
comment. That is design principle 2's rule applied to arrangement: order is
data like any other, and a rearranged one presented as the origin's is the
thing that rule forbids.

Two departures so far, both earning it:

- the **token tree** turns a flat `usage` object into parents and parts, and
  puts `cache read` before `in` because "how much did it already have" is
  the question the rows are read to answer;
- the **request page** draws each `tool_result` inside the `tool_call` it
  answers, as one card (`Full.note` says so at the top). The wire sends
  every call and then every result — 957 of 957 such requests in the log —
  so five results otherwise arrive as five boxes with nothing tying them to
  the five calls above.

A corollary worth having: **when the layout carries the relation, the labels
should stop carrying it.** The results were first labelled `tool_result 3 ·
read_file` so a reader could pair them by eye; once the card did the
pairing, the number and the name were two more things to read and to keep
true, and went back to a bare `tool_result`.

The test for a departure is whether the original order was carrying meaning
a reader needs. Reordering *spans* on a waterfall would fail it — their
order is their timing. Reordering messages a model received in one block
does not: they were one block.

**Metadata stays inline and faint**, not behind a disclosure and not
right-floated; lookup ids are faint monospace. The detail view is the place
for what a summary line cannot hold —
[docs/span-rendering.md](span-rendering.md) works this through row by row.
