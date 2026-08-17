# Writing a scope spec

How to make the Turns tab display a hermes tool it has never heard of.

A scope spec is a Python module holding a `SCOPES` dict. It can live in this
repo (`plugins/turns/scopes.py` is one) or in a package of your own
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

from plugins.turns.scope_spec import Field, Row, Scope, payload

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
the rest of hobserver.

## Wiring it up

**If you also ship a tab**, expose the table from it and you are done —
`SCOPES` on the tab module is picked up when that tab loads
([ADR 10](../design/adr/0010-a-tab-contributes-its-own-scope-specs.md)):

```python
# my_observer_tab/__init__.py
from my_observer_tab.scopes import SCOPES
```

`scopes.py` beside your `__init__.py`, holding the table: the same shape as
`plugins/mem0/` and `plugins/turns/`, and named for the `SCOPES` it
exports.

Nothing else to configure: enabling your tab brings its spans with it, and
disabling it takes them away again — which is what stops a span linking to a
page nobody serves. `plugins/mem0/` is this shape.

**If you have no tab**, just hermes tools to render, name the module in the
Turns tab's settings in `hobserver.toml`:

```toml
[[tabs]]
plugin = "plugins.turns"
settings = { scope_specs = ["acme_hermes_specs"] }
```

`scope_specs` takes a list, or a bare string for one module. Each entry is an
importable module path, resolved the same way `plugin` is — so an installed
package and a directory on `PYTHONPATH` both work, and nothing needs to be
inside this repo.

Restart the app. The startup banner gains a line naming the module, and the
`deploy` spans on a turn page stop being payload dumps.

## The contract

| attribute | | |
| --- | --- | --- |
| `SCOPES` | required | `{scope name: Scope}` — the hermes tool's own name, as it appears on the span |
| `SCOPES_BY_CATEGORY` | optional | `{category: Scope}`, for spans with no stable name |
| `SPAN_READERS` | optional | `{name: fn(span) -> value}` — payload readings your rows name as sources |

A name beats a category, so a category spec is the default for a whole class
of span and a name spec singles one out. Use a category only when the name
does not identify the scope — an llm span is named for the provider that
answered it (`anthropic`), which is why that one is a category spec.

**A spec module imports `plugins.turns.scope_spec`, and nothing else from
hobserver.** That vocabulary is a published surface — one of the few,
listed in [ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md) —
so importing it is the ordinary thing to do, and the names in it change with
the same care as a URL.

What you should not import is this app's internals (`spans`, `assembler`,
`atof_reader`, a tab's own helpers) or another plugin. Those are not
promised to stay put, and reading another plugin's data has its own route:
[ADR 4](../design/adr/0004-cross-plugin-access-by-link-or-published-accessor.md).

## When a payload needs reading, not looking up

`payload("service")` reads a key. When the value has to be worked out —
decoded from a JSON string, ranked, counted, walked — write a **span reader**
and name it from a `Field` the way you would a built-in
([ADR 17](../design/adr/0017-a-payload-reading-is-contributed-beside-the-spec-that-names-it.md)):

```python
import json

def deploy_targets(span):
    """Hosts this deploy touched, newest first."""
    data = span.end_data                     # a dict, a JSON string, anything
    if isinstance(data, str):
        data = json.loads(data)              # your payload, your parsing
    if not isinstance(data, dict):
        return []                            # never raise on a shape you did
    hosts = data.get("hosts")                # not expect: payloads are opaque
    return [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []

SCOPES = {"deploy": Scope(rows=[
    Row([Field("deploy_targets", clip="wide")], layer="detail")])}
SPAN_READERS = {"deploy_targets": deploy_targets}
```

Four things to know:

- **You get the whole span** — `start_data`, `end_data`, `metadata`,
  `category_profile`, the timings. Read what you need; nothing is curated for
  you first.
- **Read defensively.** A payload is opaque per the ATOF spec, and a shape
  that has held all year is still not a contract. A reader that raises loses
  its own value and nothing else, but a reader that trusts a shape shows a
  wrong one, which no error will tell you about.
- **A reader wins over a built-in of the same name.** That is how you replace
  this app's reading of a tool whose payload differs from hermes'; the
  startup line names it (`overriding Span.command`) so it is never silent.
- **Both halves load or neither does.** A reader that is not callable, or
  named something a `Field` could not use, takes the whole module out with a
  reason in the banner — rows naming readings that never loaded would fail
  more quietly than that.

`plugins/mem0/spans.py` is the in-tree example, beside the `scopes.py` whose
rows name it.

## `render=`, and why it is not for you

`Scope` takes either `rows` or `render`, never both. You will see the second
in `plugins/turns/scopes.py`:

```python
LLM = Scope(render="llm")
```

That names a macro in `plugins/turns/templates/turns/_scope_*.html` which renders the
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

## A value too big for a row

Some values do not belong on a waterfall row at any length — a deploy log, a
model's whole answer. Declare them on the scope as `Full`s and each becomes a
page of its own, reached by an open-in-a-new-tab icon at the end of the
excerpt ([ADR 12](../design/adr/0012-open-a-whole-value-on-its-own-page.md)):

```python
from plugins.turns.scope_spec import Full, const

SCOPES = {"deploy": Scope(
    rows=[Row([Field(payload("service")),
               Field(payload("log"), clip="wrap", full="log")])],
    fulls=[Full(key="log", source=payload("log"), render="markdown",
                title=const("Deploy log"),
                note=const("stdout as the deploy tool reported it"))]),
}
```

- **`fulls` goes on the `Scope`, and a `Field` names one by key.** The route
  serving the page reads the same list, so a key exists in both places or in
  neither. A `Field(full=…)` naming a key you did not declare is reported at
  load, like any other spec fault.
- **The icon is drawn whether or not the value was cut short.** That is
  deliberate and not yours to condition: a reader cannot see that a value
  fits, only that it ends.
- **`render="markdown"`, `"text"` or `"sections"`.** A value that resolves to
  a dict or a list is shown as indented JSON whichever you asked for. Every
  page also offers `?raw=1`, so nothing you declare can hide the characters
  underneath.
- **`"sections"` is for a value that is several labelled parts** — the
  messages of a request, the steps of a deploy. Its source resolves to
  `[{"label": …, "text": …}]`, and each text is rendered under its label,
  drawn as page furniture outside the markdown. Use it whenever the labels
  are *yours* and the text is not: written into the markdown instead, a label
  is one more heading among the content's own, and a reader cannot tell which
  words came from where.
- **`title` and `note` are sources**, so `const("…")` for a literal. Use
  `note` to say where the value came from — required reading if your spec
  reshapes it on the way, since the page must not present a reconstruction as
  something the tool emitted.
- **The URL is `/turns/span/<span uuid>/<key>`.** Your key is a URL
  segment: lower case, digits, `-` and `_`.

## Reading the payload, or a property

`Field(payload("service"))` reads the span's start payload;
`Field(payload_end("status"))` reads the end payload, which is the call's
output. `Field(profile("annotated_request"))` reads the span's category
profile, which is where an llm span keeps its request and its response. All
three read defensively — a payload is opaque per the ATOF spec, may arrive as
a JSON string, and may be missing the key entirely.

`Field("command")` names a value of the span instead — one of your own
`SPAN_READERS` if you declared one by that name, else a `Span` property.
The properties exist for this tree's own scopes, where reading the payload is
not enough: inferring a patch's mode, normalizing two shapes of a memory
batch. **Your spec cannot add a property** — but it does not need to, because
a reader is the same thing from outside (see "When a payload needs reading"
above), and it can stand in front of a property whose reading does not suit
your payload.

`transform` is the other route, and the smaller one: it takes a callable
applied to an already-resolved value, so it is for presentation — shortening,
formatting, `tilde()` on a path — where a reader is for getting the value out
of the payload at all.

## Overriding a spec this app ships

Claim a name already in the table and yours wins. The in-tree table is a
default, not a floor: if your hermes' `terminal` payload differs from the one
this app assumes, replace the spec rather than living with the wrong reading.

The banner names what you took over, so an override is visible at startup
rather than a silent difference in what a page shows:

```
    scope spec acme_hermes_specs  [ok] (from settings, overriding terminal)
```

A `SPAN_READERS` name is announced the same way — `overriding
reader:mem0_results` for another module's reader, `overriding Span.command`
for one of this app's own readings. That second line is the one worth
watching for: nothing else would show it, because the rows go on rendering
with different values in them.

## When it goes wrong

Nothing a spec does can take a turn page down. A page holds many spans and
polls every 2 s, so failure is per scope and always lands on the generic
payload dump — which is what that scope showed before your module existed.

| what happened | what you see |
| --- | --- |
| module will not import | banner names the module and the exception; every other scope renders |
| no `SCOPES` and no `SPAN_READERS` in it, or not a dict | banner says so; module skipped |
| a `SPAN_READERS` entry is not callable, or badly named | banner says so; the whole module is skipped, specs included |
| a spec raises while resolving | that span falls back to its payload; the rest of the page is fine |
| a reader raises while resolving | that one field is empty; the row, the span and the page are fine |
| a spec resolves to no rows | same — treated as "nothing to say about this span" |

Because failure is quiet on the page and loud at startup, **read the banner**
after adding a module. A spec that silently does nothing looks identical to
one you forgot to wire up.

## Testing one

Keep them with your plugin — `plugins/<name>/tests/` here, or a `tests/`
directory in your own package — so the tests travel with the thing they test.

A spec is data, so it resolves without a Flask app or a rendered page:

```python
from plugins.turns.scope_spec import SpecTable, rows_for
from acme_hermes_specs import SCOPES

def test_deploy_leads_with_the_service(make_span):
    table = SpecTable(SCOPES, {})
    span = make_span(name="deploy", start={"service": "api",
                                           "environment": "prod"})
    rows = rows_for(span, table)
    assert rows[0]["cells"][0]["text"] == "api"
```

`plugins/turns/tests/test_scope_spec.py` is a worked set of these, including a
`make_span` helper you can copy.

## Checklist

- [ ] `SCOPES` keyed by the scope name exactly as it appears on the span
- [ ] every value read with `payload()` / `payload_end()` / `profile()`, not
      a property you would have to add to this tree
- [ ] anything too big for a row declared as a `Full` rather than clipped and
      lost, with a `note` saying where it came from
- [ ] payload shapes checked against the tool's own signature in the hermes
      source, not against what your log happens to have shown so far
- [ ] `layer` set deliberately on every row — what belongs on the summary
      line is the exception, not the default
- [ ] no `deco` invented: the vocabulary is closed, and a fifth axis is a
      conversation to have upstream rather than a class to make up
- [ ] the startup banner read once, to confirm the module loaded and to see
      what it overrode
