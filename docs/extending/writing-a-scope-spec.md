# Writing a scope spec

How to make the Prompts tab display a hermes tool it has never heard of.

A scope spec is a Python module holding a `SCOPES` dict. It can live in this
repo (`plugins/prompts/scopes.py` is one) or in a package of your own
installed alongside — both load the same way, so displaying your own tools
needs no fork and no patch carried on top of this tree.

You need one when a span shows up on a turn page as a payload dump: that is
the generic fallback, and it means no spec claimed that scope name.

- The **vocabulary** — the four axes, the field sources, the row kinds — is in
  [span-rendering.md](../design/span-rendering.md#writing-a-spec). This file is about
  getting a module of your own loaded and knowing what happens when it breaks.
- [writing-a-plugin.md](writing-a-plugin.md) is the equivalent for a whole
  tab. Write a spec when you want an existing tab to render your tool; write
  a tab when you have a different log or store to show.

## A whole spec module

Say hermes has been extended with a `deploy` tool whose payload is
`{"service": ..., "environment": ..., "revision": ..., "notes": ...}`.

```python
"""Scope specs for the acme hermes tools."""

from plugins.prompts.scope_spec import Field, Row, Scope, payload

DEPLOY = Scope(rows=[
    # The summary line: what was deployed, and where.
    Row([Field(payload("service"), font="mono"),
         Field(payload("environment"), deco="tag")]),
    # The revision is a lookup key, so it is faint, copyable and detail-only.
    Row([Field(payload("revision"), deco="id", copy="copy revision")],
        layer="detail"),
    # Free text: wraps out in detail rather than being ellipsized.
    Row([Field(payload("notes"), clip="wide-wrap")], layer="detail"),
])

SCOPES = {"deploy": DEPLOY}
```

That is the whole module. No blueprint, no registration call, no import from
the rest of hermes-observer.

## Wiring it up

**If you also ship a tab**, expose the table from it and you are done —
`SCOPES` on the tab module is picked up when that tab loads
([ADR 10](../design/adr/0010-a-tab-contributes-its-own-scope-specs.md)):

```python
# my_observer_tab/__init__.py
from my_observer_tab.scopes import SCOPES
```

`scopes.py` beside your `__init__.py`, holding the table: the same shape as
`plugins/mem0/` and `plugins/prompts/`, and named for the `SCOPES` it
exports.

Nothing else to configure: enabling your tab brings its spans with it, and
disabling it takes them away again — which is what stops a span linking to a
page nobody serves. `plugins/mem0/` is this shape.

**If you have no tab**, just hermes tools to render, name the module in the
Prompts tab's settings in `observer.toml`:

```toml
[[tabs]]
module = "plugins.prompts"
settings = { scope_specs = ["acme_hermes_specs"] }
```

`scope_specs` takes a list, or a bare string for one module. Each entry is an
importable module path, resolved the same way `module` is — so an installed
package and a directory on `PYTHONPATH` both work, and nothing needs to be
inside this repo.

Restart the app. The startup banner gains a line naming the module, and the
`deploy` spans on a turn page stop being payload dumps.

## The contract

| attribute | | |
| --- | --- | --- |
| `SCOPES` | required | `{scope name: Scope}` — the hermes tool's own name, as it appears on the span |
| `SCOPES_BY_CATEGORY` | optional | `{category: Scope}`, for spans with no stable name |

A name beats a category, so a category spec is the default for a whole class
of span and a name spec singles one out. Use a category only when the name
does not identify the scope — an llm span is named for the provider that
answered it (`anthropic`), which is why that one is a category spec.

**A spec module imports `plugins.prompts.scope_spec`, and nothing else from
hermes-observer.** That vocabulary is a published surface — one of the few,
listed in [ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md) —
so importing it is the ordinary thing to do, and the names in it change with
the same care as a URL.

What you should not import is this app's internals (`assembler`,
`atof_reader`, a tab's own helpers) or another plugin. Those are not
promised to stay put, and reading another plugin's data has its own route:
[ADR 4](../design/adr/0004-cross-plugin-access-by-link-or-published-accessor.md).

## `render=`, and why it is not for you

`Scope` takes either `rows` or `render`, never both. You will see the second
in `plugins/prompts/scopes.py`:

```python
LLM = Scope(render="llm")
```

That names a macro in `plugins/prompts/templates/prompts/_scope_*.html` which renders the
scope by hand. Exactly one scope uses it — an llm call, whose token tree runs
a separator state machine that is not a shape the vocabulary should learn.
`scope_spec.RENDER_MACROS` is the list of names that exist, and it has one
entry.

**A contributed module cannot use it**, because adding a macro means editing
this app's template — and since ADR 9 there is far less reason to want to:
the two scopes that used to need a macro were reaching into another tab, and
that is now declarable (see above). Naming a macro that does not exist is
refused at load, with the reason in the banner, rather than leaving you with
a payload dump and no clue why:

```
scope spec acme_specs  [MISSING (ValueError: SCOPES['deploy'] names
render='my_own_macro', which is not a macro this app defines …)]
```

If a scope of yours seems to need `render=`, the vocabulary is missing
something — worth raising upstream, since a fourth exception is a signal
about the design rather than a gap for you to work around.

## Showing your own tab's data on a span

A span row can link into a page your tab serves, and show a value your tab
looks up — the two shapes
[ADR 4](../design/adr/0004-cross-plugin-access-by-link-or-published-accessor.md) allows,
available to a spec since
[ADR 9](../design/adr/0009-scope-specs-may-link-and-read-published-data.md). The mem0
scopes in `plugins/mem0/scopes.py` are written this way and are worth reading
as a worked example — they sit in the plugin that owns them, which is
[the first route above](#wiring-it-up), and nothing about them is privileged
for being in-tree.

**A link** names an endpoint and its parameters, never a URL. Your tab owns
its own addresses, and a link whose endpoint nothing serves drops its row
rather than breaking the page:

```python
Link("zep.search_event",
     params={"session": "session_id", "query": payload("query")},
     text=const("open in Zep →"),
     layer="detail")
```

The row drops when `text` resolves to nothing, so an in-flight call never
shows a link to a result that does not exist yet.

**An accessor** reads a lookup your tab published on `app.extensions` in its
`init_app`. It is called `fn(key, span_start_seconds)` — what to look up, and
the moment to read it as of — and `attr()` takes a field off the record:

```python
Field(attr(accessor("zep_prior_text", payload("memory_id")), "text"),
      clip="wide-wrap")
```

Three rules worth knowing before you rely on it:

- **It runs on the render path**, once per span that names it, on a page that
  polls every 2 s. One indexed lookup, as ADR 4 requires. Anything heavier
  belongs behind a link.
- **Absent is fine.** If your tab is disabled the accessor is missing, the
  row drops, and the rest of the page renders. Same if it raises.
- **Key on a payload field, not a `Span` property.** Properties are something
  only this tree can add, so keying on one puts you back where you started.

## Reading the payload, or a property

`Field(payload("service"))` reads the span's start payload;
`Field(payload_end("status"))` reads the end payload, which is the call's
output. Both read defensively — a payload is opaque per the ATOF spec, may
arrive as a JSON string, and may be missing the key entirely.

`Field("command")` names a `Span` property instead. Those exist for this
tree's own scopes, where reading the payload is not enough — inferring a
patch's mode, normalizing two shapes of a memory batch. **Your spec does not
need one**, and cannot add one without patching this tree, which is why
`payload=` exists.

If you find yourself wanting a property, the usual answer is that the value
needs computing before it can be shown; do it in your own module and pass a
callable to `transform`, which receives the resolved value.

## Overriding a spec this app ships

Claim a name already in the table and yours wins. The in-tree table is a
default, not a floor: if your hermes' `terminal` payload differs from the one
this app assumes, replace the spec rather than living with the wrong reading.

The banner names what you took over, so an override is visible at startup
rather than a silent difference in what a page shows:

```
    scope spec acme_hermes_specs  [ok] (from settings, overriding terminal)
```

## When it goes wrong

Nothing a spec does can take a turn page down. A page holds many spans and
polls every 2 s, so failure is per scope and always lands on the generic
payload dump — which is what that scope showed before your module existed.

| what happened | what you see |
| --- | --- |
| module will not import | banner names the module and the exception; every other scope renders |
| no `SCOPES` in it, or not a dict | banner says so; module skipped |
| a spec raises while resolving | that span falls back to its payload; the rest of the page is fine |
| a spec resolves to no rows | same — treated as "nothing to say about this span" |

Because failure is quiet on the page and loud at startup, **read the banner**
after adding a module. A spec that silently does nothing looks identical to
one you forgot to wire up.

## Testing one

A spec is data, so it resolves without a Flask app or a rendered page:

```python
from plugins.prompts.scope_spec import SpecTable, rows_for
from acme_hermes_specs import SCOPES

def test_deploy_leads_with_the_service(make_span):
    table = SpecTable(SCOPES, {})
    span = make_span(name="deploy", start={"service": "api",
                                           "environment": "prod"})
    rows = rows_for(span, table)
    assert rows[0]["cells"][0]["text"] == "api"
```

`tests/test_scope_spec.py` in this repo is a worked set of these, including a
`make_span` helper you can copy.

## Checklist

- [ ] `SCOPES` keyed by the scope name exactly as it appears on the span
- [ ] every value read with `payload()` / `payload_end()`, not a property you
      would have to add to this tree
- [ ] payload shapes checked against the tool's own signature in the hermes
      source, not against what your log happens to have shown so far
- [ ] `layer` set deliberately on every row — what belongs on the summary
      line is the exception, not the default
- [ ] no `deco` invented: the vocabulary is closed, and a fifth axis is a
      conversation to have upstream rather than a class to make up
- [ ] the startup banner read once, to confirm the module loaded and to see
      what it overrode
