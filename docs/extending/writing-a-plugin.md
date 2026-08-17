# Writing a tab

A tab is a Python module. It can live in this repo under `plugins/`, or in a
package of your own installed alongside — both load the same way, so a tab
written elsewhere needs no fork and no patch carried on top of this tree.

## A whole plugin

```python
"""A tab showing the last few lines of a log file."""

import os

from flask import Blueprint, current_app, render_template_string

PLUGIN_API = 1
bp = Blueprint("tail", __name__)
TAB_LABEL = "Tail"
URL_PREFIX = "tail"


def _path(settings):
    return settings.get("path", "/var/log/syslog")


def sources(settings):
    """What this tab reads. Shown in the startup banner."""
    path = _path(settings)
    return [{"label": "log", "path": path, "required": True,
             "problem": None if os.path.exists(path) else "no such file"}]


def init_app(app, settings):
    """Called once at registration."""
    app.config["TAIL_PATH"] = _path(settings)


@bp.route("/")
def index():
    with open(current_app.config["TAIL_PATH"]) as fh:
        lines = fh.readlines()[-50:]
    return render_template_string(
        "{% extends 'base.html' %}{% block content %}"
        "<pre class='blob'>{{ text }}</pre>{% endblock %}",
        text="".join(lines))
```

Add it to `hobserver.toml`:

```toml
[[plugins]]
plugin = "my_tail_tab"          # or "plugins.tail" if it lives in this tree
settings = { path = "~/logs/app.log" }
```

That is the whole mechanism. There is no registry to add yourself to.

## The contract

| attribute | required | meaning |
| --- | --- | --- |
| `PLUGIN_API` | yes | the contract version you wrote against — currently `1` |
| `bp` | yes | a Flask blueprint, registered under your prefix |
| `TAB_LABEL` | yes | what the tab reads in the bar |
| `URL_PREFIX` | yes | your address; may be multi-segment (`memory/mem0`) |
| `init_app(app, settings)` | no | called once at registration |
| `sources(settings)` | no | what you read, for the banner and error states |
| `SCOPES` / `SCOPES_BY_CATEGORY` | no | how your own hermes spans render on a tab that paints spans |
| `SPAN_READERS` | no | how those spans' payloads are read, for the values your rows name (ADR 17) |
| `STYLES` | no | a CSS string the shell injects into every page's `<head>`, for classes the shared stylesheet does not cover (ADR 20) |

Your blueprint must have an `index` endpoint — that is what the tab links to.

**Import only what this app publishes.** The contract is these attributes plus
Flask, so most tabs import nothing at all and depend on no version of this
app. Where you do need something — `base.html`'s classes, or the scope-spec
vocabulary if you contribute `SCOPES` — those are published surfaces
([ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md)). This
app's internals and other plugins are not.

`bp.name` is your code identifier — it appears in `url_for("tail.index")` and
names your template directory. `TAB_LABEL` and `URL_PREFIX` are free to change
without touching either.

## Settings

`settings` is whatever your `[[plugins]]` entry carried, passed through untouched.
The shell expands `~` and `$VARS` in string values and does nothing else — it
never inspects your keys. Supply your own defaults, and validate what you are
given.

It is also available at request time as
`current_app.extensions["tab_settings"][<blueprint name>]`, if you would rather
not stash it yourself in `init_app`.

## Sources

Each entry is a plain dict, so you need no import:

```python
{"label": "log", "path": "/var/log/app.log",
 "required": True, "problem": None}
```

An optional `from` prints in brackets after the path. Reserve it for something
the path does not already say — the Turns tab uses it to report one scope spec
overriding another. Which setting or variable a path came from is not that: the
banner lists the path itself.

`problem` is `None` when the source is fine, else a short phrase saying what is
wrong. `required` decides what happens then:

- **`required: True`** — the tab is taken out of service. The shell serves a
  page in its place naming the problem, the tab bar marks it, and every other
  tab carries on. Use this when your views cannot render at all without the
  source.
- **`required: False`** — you stay in service and explain the situation
  yourself. The Turns tab does this: a missing ATOF log is expected when
  hermes has not run with the exporter on, and the page says so with the path
  it tried.

## Templates and page furniture

Extending `base.html` gives you the tab bar, the copy-to-clipboard button, the
live-poll script and the shared CSS. These are the public surface:

| what | how |
| --- | --- |
| page chrome | `{% extends "base.html" %}`, fill `{% block content %}` and `{% block title %}` |
| self-updating region | wrap content in an element with `data-live-poll="<ms>"`; `"0"` means static |
| item navigation | `{% from "_item_nav.html" import item_nav %}` for "← all X" plus prev/next |
| notices | `<p class="notice warn">` for a problem the reader must see |
| no tab bar | `{% block tabbar %}{% endblock %}` — for a page opened in its own tab to read one thing, never for one reached by navigating |

The empty `tabbar` is the one piece of chrome worth turning off, and only in
the case it exists for: `/turns/span/<uuid>/<key>` is opened from a span
icon into a new tab, so a row of tabs there offers to navigate away from a
place the reader never navigated to. Any page a reader *walks* to keeps the
bar, or they lose their place in the app. The `hobserver` heading is
outside the block, so a page without the bar still has a way home.

Keep your templates in your own package —
`Blueprint("tail", __name__, template_folder="templates")`, with the files
under `templates/tail/` so the names cannot collide with another tab's.
`base.html` still resolves, because the app's templates stay on the Jinja
loader path. The in-tree plugins are built this way too, so nothing about
your layout differs from theirs.

## Styling

`base.html` ships a stylesheet, and the classes a scope spec resolves to —
`.detail-id`, `.mode-tag`, `.list-item`, and the rest — are a published surface
([ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md)).
Reuse them where they fit: by naming them on your own pages, and through the
scope vocabulary on a span-painting tab. A test pins them, so they are not
renamed out from under you.

For styling that is genuinely your own — a class the shared vocabulary does not
cover, or the look of your own pages — expose a `STYLES` string. The shell
concatenates every loaded plugin's `STYLES` and injects it once into every
page's `<head>`, each block fenced with your plugin name. Because it lands on
every page, it also styles the spans you paint on another tab, which have no
template of yours to carry a `<style>`
([ADR 20](../design/adr/0020-a-plugin-ships-its-own-css-through-a-styles-seam.md)).

```python
STYLES = """
.tail-stale { color: #999; font-style: italic; }
"""
```

It is injected as written — trusted, like the rest of your plugin. Keep it to
what the shared vocabulary does not already give you: scattering the common
classes into every plugin trades the shell's one coherent stylesheet for many.
Prefix your class names so they cannot collide with another plugin's.

## Tests

Keep them in your package, `<your_package>/tests/`. The in-tree plugins are
laid out that way — `plugins/mem0/tests/`, `plugins/turns/tests/` — and this
repo's `pyproject.toml` names both `tests` and `plugins` as test roots, so
`uv run pytest` collects a plugin's tests without being told about it. The
root `conftest.py` is on the path from anywhere, so `from conftest import
make_app` gets you a whole app with the tabs registered.

## Failure and collisions

Anything wrong with your tab — import error, missing attribute, wrong
`PLUGIN_API`, unusable required source — takes your tab out of service and
leaves the rest of the app running. The reason is printed at startup and shown
on your tab's page.

One case is fatal to the whole app: two tabs claiming the same `URL_PREFIX` or
the same blueprint name. Either could answer a request and nothing would say
which, so the app refuses to start and names both. Pick a prefix specific
enough not to collide — namespace it (`memory/zep`) if it belongs to a family.

## Reading other tabs' data

Don't. Per [ADR 4](../design/adr/0004-cross-plugin-access-by-link-or-published-accessor.md),
a tab may link to another's page, or call an accessor the other publishes in
`app.extensions`, but never opens another tab's data source. Handle the
accessor being absent — the tab that publishes it may be disabled.

## Checklist

- [ ] `PLUGIN_API`, `bp`, `TAB_LABEL`, `URL_PREFIX`, and an `index` route
- [ ] imports only surfaces this app publishes, and never another plugin
      ([ADR 8](../design/adr/0008-plugins-may-import-published-host-vocabulary.md)) —
      most tabs need no import at all, which is still the lightest thing to be
- [ ] `sources()` reports every file or store you read, with `required` set
      deliberately
- [ ] `SCOPES` (and `SPAN_READERS`, if a row needs a payload read rather
      than looked up) if your tab owns a kind of hermes span — how it shows on a
      turn page travels with the tab that owns it, and goes when it is
      disabled ([ADR 10](../design/adr/0010-a-tab-contributes-its-own-scope-specs.md),
      [writing-a-scope-spec.md](writing-a-scope-spec.md))
- [ ] a prefix unlikely to collide
- [ ] `STYLES` only for a class the shared stylesheet does not already cover —
      reuse `base.html`'s published classes first
      ([ADR 20](../design/adr/0020-a-plugin-ships-its-own-css-through-a-styles-seam.md))
- [ ] your own defaults for every setting, and a sensible error when a setting
      is wrong
