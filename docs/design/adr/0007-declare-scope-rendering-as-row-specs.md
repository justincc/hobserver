# 7. Declare scope rendering as row specs, with named exceptions

Date: 2026-08-05

## Status

Accepted — implemented 2026-08-05.

Two things the implementation settled that this ADR had left open:

- **`Alt`, `Each` and `when_many` earned their place.** The vocabulary needed
  a first-that-resolves (patch's path *or* its V4A files), a per-entry
  repeat, and a way to hold back a label a lone entry does not need. All
  three fell out of migrating the thirteen; none is a fifth axis.
- **A payload key present with `""` is a value, not an absence.** The first
  cut folded it into None, which would have lost the empty `new_string` a
  patch passes to delete its matched text — the trap `Span._new_string`
  already exists to avoid on the property side.

The escape hatch stayed at the three named below.

## Context

The turn page renders one row per span. What a given scope shows — on the
summary line and in the detail view — is currently spread across three
places:

- **65 `Span` properties** in `plugins/prompts/assembler.py` read the payload
  and return `None`/`[]` when a key is absent.
- **A 278-line `{% if %}/{% elif %}` chain** in `templates/prompts/turn.html`
  (`:175`–`:452`) dispatches on *which property is truthy* and writes the
  markup — 16 branches, ending in the generic payload fallback.
- **CSS classes in `base.html`** decide which layout a row belongs to:
  `.list-item` is detail-only (`:230`), `.list-compact` summary-only (`:229`),
  neither means both. 28 and 7 occurrences respectively.

Adding a scope therefore means editing three files and finding the right slot
in an ordered chain. The ordering is load-bearing and silently so: `patch_mode`
must precede `path` (`:359` before `:372`) or a replace-mode patch renders as a
bare filename.

**The mapping from scope to content already exists twice.** Roughly 20 of the
65 properties gate on the scope name internally — `self.name == "patch"`
(`:509`, `:951`), `"search_files"` (`:532`), `"execute_code"` (`:838`),
`_is_skill_scope` (`:912`), `"memory"`, `"todo"`, `"web_extract"`,
`"delegate_task"`, `"vision_analyze"`. The template then rediscovers the same
scope by asking which of those properties came back non-empty. One fact,
written as a name guard in Python and as an ordered truthiness test in Jinja.
Only `command`, `workdir` and `path` are genuinely shared across scopes, and
`is_llm` keys off category rather than name.

**Three branches are already data-driven, and they are the complex ones.**
`generic_fields` → `payload_rows` (`turn.html:43`), `token_rows` from
`TOKEN_TREE` (`assembler.py:112`), `session_search_stats` and `memory_ops`
all follow the same shape: Python computes a list of row dicts, Jinja paints
them in a loop. The pattern works and is under test.

**The vocabulary those rows draw on is small.** Across ~40 rows, every one is
a point in four orthogonal axes:

| axis | values |
|---|---|
| font | mono (`<code>`) / prose (`<span>`) |
| clip | ellipsis-right, ellipsis-left (`.tail`), wrap-in-detail (`.wrap-detail`), full-width (`.wide`) |
| layer | both / summary-only / detail-only |
| decoration | `.mode-tag`, `.gen-key` label, `.mem-id` faint + copy button, `.skill-action`, plain |

**Three branches resist declaration**, and each for a different reason:

- **llm** (`:376`–`:447`) runs a separator state machine — `ns.prior` (`:435`)
  tracks whether anything precedes a token row so a leading `·` is suppressed
  — and carries tooltips that are paragraphs.
- **mem0_search** (`:204`–`:234`) builds a cross-plugin `url_for` and lets the
  length of a `[:3]` slice decide whether the link reads "all N results" or
  "full result".
- **mem0 previous-text** (`:255`–`:297`) reads an out-of-band `prior_memory`
  map keyed by span uuid, and its provenance tooltip contains an `<a>`
  element.

Finally, the comments in `turn.html` are load-bearing. Why `.call-list` and
not `.path` (`:397`), why a delete leads with recovered text (`:267`), why the
`tokens` label is detail-only (`:420`) — this is the reasoning
[docs/span-rendering.md](../span-rendering.md) narrates. A refactor that
leaves nowhere to put it destroys more than it tidies.

### Scope rendering is the last thing you must fork to extend

[ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md) set out
that someone should be able to install hermes-observer, install or write their
own tab, and run both "with no patch carried on top of this tree". That holds
at tab granularity. It does not hold inside the Prompts tab.

hermes' tool set differs between users and versions — the premise the generic
fallback exists to serve. Someone running hermes with their own tools gets a
readable payload dump and no way to improve on it short of forking, and their
scopes are precisely the ones this repo will never curate. A spec table that
is a module-level dict in this tree reproduces exactly the `PLUGINS` tuple
that ADR 5 removed, one level down.

## Decision

**Express each scope's non-generic rendering as a spec — a list of row
descriptors keyed by scope name — in one module, painted by one template
loop. Keep a named escape hatch for the branches that are not declarative.**

Three parts:

**1. The spec is Python data, not an external config file.** A module-level
table (`plugins/prompts/scope_spec.py`) of dataclass literals, values being
property names or callables:

```python
"terminal": Scope(
    rows=[Row(Field(prop="command", font="mono", clip="wrap"),
              Field(prop="workdir", prefix="in ", transform=tilde))],
),
"patch": Scope(
    rows=[Row(Field(prop="patch_mode", deco="mode-tag"),
              Field(first_of("path", "patch_paths"), clip="tail",
                    transform=tilde, more_count=True))],
    macros=[Diff("patch_old_string", "patch_new_string"),
            Row(Field(prop="patch_text", font="mono", clip="wrap"),
                layer="detail")],
),
```

**A `Field` reads either a `Span` property or a payload key.** `Field(prop=…)`
names a property, for the scopes where one does real work — `patch_mode`'s
inference, `memory_ops`' normalization of two payload shapes,
`session_search_mode`'s dispatch. `Field(payload="urls")` reads the start
payload directly, through the same defensive machinery
`generic_payload_fields` already uses (`atof_reader.py:104`), with
`payload_end=` for the three cases that read output.

This is what makes a scope declarable by someone who cannot add properties,
and it is why the choice belongs in this ADR rather than in the
implementation: a `Field` that only names properties makes every third-party
scope a fork of `assembler.py`, and that cannot be retrofitted once specs are
written against it.

TOML or YAML was considered and rejected. It cannot call `tilde` or
`url_for`, and `url_for('mem0.search_event', …)` is the ADR 4 seam where two
plugins meet. Tooltips carry markup and interpolation. It would need a
payload-path expression language — JSONPath reinvented — where `Span`
properties already do that job with defensive reads and tests. It puts
display decisions outside `uv run pytest`, where 106 tests currently hold
them. And every escape hatch a text format grows becomes an `eval` on config
text.

**`observer.toml` stays what ADR 5 made it: tab wiring.** This is the
distinction that decides the format — which tabs load is an operator's
choice, and how a span renders is the app's own structure. Only the first
belongs in a file an operator edits.

**2. Keyed by scope name, not by property truthiness.** This makes explicit
the guard already written inside the properties, and lets those guards be
dropped. Ordering stops being load-bearing: `patch` names its own rows, so
nothing has to precede anything. Shared fields (`path`, `command`) are listed
by each scope that wants them.

Consequence, taken deliberately: a scope this app does not know by name falls
to the generic fallback even when its payload happens to match a curated
shape. Today's duck-typing would have rendered it. Given hermes' tool set
differs between users and versions, the generic fallback is the correct
destination for an unknown name — and it is what the chain's `else` already
does for anything whose keys do not match.

**3. `render=` names a macro, for the three that resist.**

```python
"mem0_search": Scope(render="mem0_search"),
```

These keep their hand-written Jinja. A spec table that admits three exceptions
is honest; one contorted to absorb `ns.prior` is a worse template with an
extra layer of indirection in front of it.

`render=` stays internal. A third party cannot name a macro in `turn.html`,
and should not be able to — it is the one part of this that is not data.

**4. Specs may be contributed from outside the tree**, by the mechanism
ADR 5 already established rather than a new one:

```toml
[[tabs]]
module = "plugins.prompts"
settings = { scope_specs = ["hermes_observer_acme.specs"] }
```

The Prompts plugin imports each named module and merges its `SCOPES` table
over its own. `settings` is opaque to the shell, the value is an importable
module path, and `plugins.prompts.specs` and `hermes_observer_acme.specs`
load identically — the same three properties ADR 5 gave `module`. No new
config concept, and no entry points, for the reason ADR 5 gave: discovery
would be a second place to look when something does not render.

The contributed module needs `SCOPES` and nothing else. It imports no host
package, exactly as a tab imports none.

Two rules follow from ADR 5's failure model:

- **A spec that fails degrades to the generic fallback for that scope**, with
  the reason reported at startup. It never 500s a turn page. A turn page
  polls every 2 s and holds many spans; one bad spec must not take a page
  down, the way one bad tab does not take the app down.
- **A contributed spec claiming a curated scope name overrides it, and says
  so at startup.** Override is a legitimate want — someone whose `terminal`
  payload differs from hermes' needs to replace that spec, not lose to it.
  This is deliberately *not* ADR 5's fatal-collision rule, because a display
  choice is not a mis-routed URL, and the loser here is visible on screen
  rather than silent. The in-tree table is a default, not a floor.

**Layer stays a CSS class.** The spec declares `layer="detail" | "summary" |
"both"` and that picks `.list-item` / `.list-compact` / neither. One DOM,
both views, no server-side branching — which is what makes per-row
`tr.detail-open` work for free. Nothing may render a row twice.

**Specs carry prose.** Each entry keeps its rationale as a docstring or
comment beside it, and `docs/span-rendering.md` keeps narrating them.

## Consequences

- Adding a scope becomes one table entry, in one file, with no ordered chain
  to slot into. That is the point of the change.
- **Adding a scope becomes the same two steps for a stranger as for this
  repo** — write the spec, name the module — which is what ADR 5 achieved for
  tabs. In-tree scopes have no capability a contributed one lacks, except
  `render=`.
- **`SCOPES` and the spec vocabulary become supported interface**, versioned
  with `PLUGIN_API`, alongside `base.html`'s classes and the ATOF envelope.
  The four axes stop being an implementation detail the moment someone else
  writes against them; renaming `clip="tail"` then breaks a stranger's tab.
  This is the real cost of extensibility here and it is why the vocabulary
  has to be got roughly right before the table is published, not after.
- **The generic fallback stops being the end of the line.** Today an unknown
  tool gets a payload dump and no recourse. It keeps the dump as its default
  and gains a way out that does not involve a patch on this tree.
- The template's span section drops from ~278 lines to a loop plus three
  retained macros. `docs/span-rendering.md` needs rewriting against the spec
  table, and stays the place the reasoning lives.
- **Two mechanisms exist where one did.** A reader now has to know whether a
  scope is declared or hand-written. The `render=` entries make that visible
  in the table rather than by absence, which is the least bad version of it.
- **The escape hatch will be under pressure.** Any scope with an awkward row
  is easier to write as a macro than to express. If `render=` grows past the
  three named here, the spec vocabulary is wrong and should be fixed or the
  decision revisited — not routed around.
- **The vocabulary will be under the opposite pressure.** Every new scope
  will suggest one more axis. Four axes cover ~40 existing rows; a fifth
  needs to earn itself against `render=`.
- **Scope-name keying is a behaviour change at the edges**, as above: an
  unrecognised name that would have duck-typed into a curated branch now
  renders generically. No scope in the log does this today.
- Migration is mechanical but not free — 13 branches, each with tests to keep
  passing. It should be one pass, not a scope at a time, or the codebase
  holds both shapes indefinitely.
- **The reasoning is the thing most at risk.** The comments in `turn.html`
  are the record of a lot of decisions that are not obvious from the markup.
  Any migration that does not carry them across has lost more than it gained.
- **Contributed specs run this app's rendering, not their own code.** A spec
  is data interpreted by the host: it selects payload keys and picks from a
  fixed vocabulary of classes, so it cannot inject markup, and the escaping
  stays the template's. The module is still imported, so it is no less
  trusted than a tab — but a spec's *reach* is narrower than a blueprint's by
  construction, which is a reason to prefer specs over telling people to
  write a tab.
- **A spec module imports the vocabulary it is written in**
  (`plugins.prompts.scope_spec`), where a tab imports nothing. This read as
  a break with ADR 5 and prompted
  [ADR 8](0008-plugins-may-import-published-host-vocabulary.md), which found
  the older rule drawn in the wrong place: `scope_spec` is a published
  surface, on the same footing as `base.html`'s classes, and importing it is
  ordinary. What the alternative would have cost is worth recording anyway —
  a mini-language of plain dicts, a parser to write, and a worse thing to
  read and to get wrong, with no less version coupling for having no import
  statement in it.
- **`ADR 5`'s payload-secret rule has to hold here too.** The generic
  renderer skips keys that look like credentials (`atof_reader.py:77`). A
  spec naming `payload=` bypasses that by being explicit, which is correct
  for a curated field and means the skip list is no longer a guarantee about
  everything on screen.

## Related

- [ADR 5](0005-tabs-are-configured-plugins-loaded-by-module-path.md) — the
  config file, the module-path loading and the per-tab failure model this
  reuses; and the no-fork goal it sets
- [ADR 4](0004-cross-plugin-access-by-link-or-published-accessor.md) — the
  `url_for` and published-accessor couplings that keep two branches
  hand-written
- [docs/span-rendering.md](../span-rendering.md) — what each scope shows today
