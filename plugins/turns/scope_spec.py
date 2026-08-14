"""What each scope shows, declared as data rather than as template branches.

ADR 7. A scope's rendering is a list of row descriptors keyed by scope name;
`templates/turns/turn.html` paints whatever `rows_for` returns and knows
nothing about any particular tool. One scope keeps hand-written Jinja behind
`render=` (ADR 7); the two that reached across the plugin boundary became
declarable once the vocabulary grew a link and an accessor (ADR 9).

Two things this module deliberately does *not* decide:

- **Which layout a row belongs to.** `layer` picks a CSS class
  (`.list-item` detail-only, `.list-compact` summary-only, neither for both)
  and `base.html` does the rest. One DOM, both layouts, never two renderings.
- **What a payload means.** A Field reads either a `Span` property, for the
  scopes where a property does real work, or a payload key directly, so a
  scope can be declared by someone who cannot add properties to this tree.

The vocabulary is four orthogonal axes — font, clip, layer, deco. Resist
adding a fifth: a scope that needs one is usually a scope that wants
`render=`.
"""

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Optional, Sequence, Union

# --- sources ---------------------------------------------------------
#
# A source says where a value comes from. A bare string names a value of the
# span, which is the common case and keeps the table readable; the helpers
# below cover everything else. `payload` is what lets a contributed spec read
# a tool this app has no properties for.
#
# A bare string resolves against the **span readers** first and the `Span`
# object second (ADR 17). A reader is `fn(span) -> value` contributed beside
# a spec, for the payloads that need reading rather than looking up: a
# stranger's tool can have both halves — how its payload is read and how it
# shows — without either living in this tree. In-tree readings stay `Span`
# properties, which is the same thing with a shorter path to it.


def payload(key: str):
    """A key in the span's start payload, read defensively."""
    return ("payload", key)


def payload_end(key: str):
    """A key in the span's end payload — the call's output."""
    return ("payload_end", key)


def profile(key: str):
    """A key in the span's category profile.

    Where an llm span keeps its request and its response — the two biggest
    values in the log, and the ones a `Full` is usually opened to see. They
    are in neither payload, so without this a scope from outside this tree
    could declare a full view of them only by going through a `Span`
    property it cannot add (the fork test, design principle 1).
    """
    return ("profile", key)


def item(key: Optional[str] = None):
    """A key of the current item inside an `Each`; the item itself if None."""
    return ("item", key)


def const(text: str):
    """A literal, for the fixed words a row needs (`→ absorbed into`)."""
    return ("const", text)


def first(source):
    """The first element of a list source."""
    return ("first", source)


def joined(source, sep: str = "\n"):
    """A list source flattened, for a title attribute holding every value."""
    return ("joined", source, sep)


def mapped(source, key: str):
    """One key lifted out of each entry of a list of dicts — the texts of a
    memory batch, say, so the summary line can lead with the first."""
    return ("mapped", source, key)


def attr(source, key: str):
    """One key of whatever `source` resolves to — for reading a field off a
    record an accessor returned."""
    return ("attr", source, key)


def accessor(name: str, key):
    """A value from another plugin's published accessor (ADR 4, ADR 9).

    `name` is the key it published on `app.extensions`; `key` is a source for
    the thing to look up. The accessor is called `fn(key, span_start_seconds)`
    — the value, and the moment on the timeline to read it as of.

    Absent accessor, or one that raises, resolves to nothing and its row
    drops: a plugin that is not registered is a state every caller already
    has to handle, and a turn page must render without it.

    **It runs on the render path**, once per span that names it, on a page
    that polls every 2 s. Keep it to one indexed lookup, as ADR 4 requires;
    anything heavier belongs behind a link instead.
    """
    return ("accessor", name, key)


def _resolve(source, span, ctx):
    """A source to its value, or None. Never raises on a shape it did not
    expect: payloads are opaque, and a spec is data that may have come from
    outside this tree."""
    if source is None:
        return None
    if isinstance(source, str):
        return _span_value(span, source, ctx)
    if isinstance(source, (list, tuple)) and source and isinstance(source[0], str):
        kind = source[0]
        if kind == "payload":
            return _payload_key(span, "start_data", source[1])
        if kind == "payload_end":
            return _payload_key(span, "end_data", source[1])
        if kind == "profile":
            profile_data = getattr(span, "category_profile", None)
            return (profile_data.get(source[1])
                    if isinstance(profile_data, dict) else None)
        if kind == "item":
            current = ctx.get("item")
            if source[1] is None:
                return current
            if isinstance(current, dict):
                return current.get(source[1])
            return getattr(current, source[1], None)
        if kind == "const":
            return source[1]
        if kind == "first":
            value = _resolve(source[1], span, ctx)
            return value[0] if isinstance(value, (list, tuple)) and value else None
        if kind == "joined":
            value = _resolve(source[1], span, ctx)
            if not isinstance(value, (list, tuple)):
                return None
            return source[2].join(str(v) for v in value)
        if kind == "mapped":
            value = _resolve(source[1], span, ctx)
            if not isinstance(value, (list, tuple)):
                return None
            out = []
            for entry in value:
                got = (entry.get(source[2]) if isinstance(entry, dict)
                       else getattr(entry, source[2], None))
                if got not in (None, ""):
                    out.append(got)
            return out
        if kind == "attr":
            return _attr(_resolve(source[1], span, ctx), source[2])
        if kind == "accessor":
            return _accessor(span, ctx, source[1], source[2])
    return None


def _span_value(span, name: str, ctx):
    """One named value of the span: a contributed reader, else a property.

    Readers win, because the in-tree table is a default and not a floor —
    the same rule the spec table follows, and the override is named at
    startup rather than applied silently (ADR 17).

    A reader that raises costs its own value and nothing else. It is
    somebody else's code reading somebody else's payload, which is exactly
    the case `_accessor` below already treats this way: one plugin's bad
    afternoon must not be a payload dump where another plugin's rows were.
    """
    reader = (ctx.get("readers") or {}).get(name)
    if reader is None:
        return getattr(span, name, None)
    try:
        return reader(span)
    except Exception:  # noqa: BLE001 - a reader may come from outside this tree
        return None


def _attr(value, key: str):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _accessor(span, ctx, name: str, key_source):
    """Another plugin's published lookup, called for this span.

    Nothing here knows what the accessor does; it is the owner's own function
    over the owner's own store (ADR 4). A missing one, a missing key or a
    raising one all resolve to nothing — one plugin's absence must not be an
    error on another plugin's page.
    """
    lookup = (ctx.get("accessors") or {}).get(name)
    key = _resolve(key_source, span, ctx)
    if lookup is None or key in (None, ""):
        return None
    start_us = getattr(span, "start_us", None)
    try:
        return lookup(key, start_us / 1_000_000 if start_us else 0)
    except Exception:  # noqa: BLE001 - another plugin's code
        return None


def _payload_key(span, which: str, key: str):
    """A payload key, through the same defensive read the generic renderer
    uses: a payload is opaque per the ATOF spec and may be a JSON string, a
    dict, or something else entirely.

    `which` names the span attribute holding the payload — not `attr`, which
    is now a source helper in this module and would shadow it.
    """
    from plugins.turns.assembler import _as_dict

    data = _as_dict(getattr(span, which, None))
    if data is None:
        return None
    # Absent means absent; "" is a value. A patch that deletes the matched
    # text passes an empty new_string, and a Diff has to be able to show that
    # side — folding it into None here would lose it, which is the trap
    # `Span._new_string` exists to avoid on the property side. A Field still
    # drops an empty value, because there is nothing to put on the row.
    return data.get(key)


# --- the whole value, on its own page --------------------------------
#
# A row shows an excerpt; a `Full` says where the rest of it is. ADR 12.

# How a full value is rendered on its own page. "markdown" for prose a model
# wrote or was sent — the case this exists for — and "text" for anything
# whose line breaks matter more than its punctuation. A value that resolves
# to a dict or a list is JSON either way: rendering a structure as markdown
# would be rendering its punctuation.
#
# "sections" is for a value that is several labelled parts — the messages of
# a request. Its source resolves to `[{"label": …, "text": …}]`, each text
# rendered as markdown under its own label, drawn as page furniture. That
# separation is the point: a label written *into* the markdown is one more
# heading among the model's own, and a reader cannot tell whose words are
# whose. Anything that is not that shape falls back to being rendered whole.
FULL_RENDERERS = frozenset({"text", "markdown", "sections"})

# The endpoint serving that page. Named here rather than in the template so
# a spec's cell can carry the same (endpoint, params) pair a `Link` does,
# and `url_for` stays the template's call (see `Link`).
FULL_ENDPOINT = "turns.span_full"

# What a key may look like. It is a URL segment and it is what a `Field`
# names, so: lower case, no slashes, no surprises.
_FULL_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class Full:
    """A complete value behind an excerpt, opened on a page of its own.

    Declared on the `Scope`, once, and referred to by key from wherever the
    excerpt appears — `Field(full="response")`, or by name from a
    hand-written macro. One declaration, because the row that offers the
    link and the route that serves it have to agree about what is behind it,
    and two copies of that would eventually not.

    key      addresses this value in the URL and from a `Field`. Unique
             within the scope; lower case, digits, `-` and `_`.
    source   where the whole value is — any source helper. It is read from
             the payload in the log, so it may be large; that is the point.
             For `render="sections"` it resolves to `[{"label", "text"}]`.
             A part may also carry `"nests": True` and the one after it
             `"nested": True`, and the two are then drawn as one card — for
             a value whose parts are pairs rather than a flat run. The
             nested part's label can be plain, since the card it sits in
             says what it belongs to.
    render   "markdown", "text" or "sections" (see FULL_RENDERERS).
    title    what this value is, as a heading — `const("Prompt")`. It is
             the page's own heading and the start of the icon's tooltip
             ("Prompt — open in a new tab"), so write it as a name, not as
             a phrase: it has to read as a title above the value and as the
             subject of a sentence beside the icon. A source, like every
             other field here.
    note     a source for one line of provenance under the heading — where
             the value came from, and whether this app reshaped it on the
             way. Required reading for anything reconstructed rather than
             quoted (design principle 2).
    """

    key: str = ""
    source: Any = None
    render: str = "text"
    title: Any = None
    note: Any = None

    def link(self, span, ctx) -> Optional[dict]:
        """The (endpoint, params) pair a template turns into an href.

        Unconditional: it does not resolve the value to decide whether to
        offer it. The value is in the log rather than in memory, and reading
        a megabyte to find out whether to draw a 13-pixel icon — on every
        span of the scope, on a page that polls every 2 s — is the whole
        cost the index exists to avoid (ADR 11). A key that turns out to
        hold nothing says so on the page it opens.
        """
        uuid = getattr(span, "uuid", None)
        if not uuid or not self.key:
            return None
        title = _resolve(self.title, span, ctx)
        return {"endpoint": FULL_ENDPOINT,
                "params": {"span_uuid": uuid, "key": self.key},
                "title": str(title) if title else None}


# --- the four axes ---------------------------------------------------

_DECO_CLS = {"tag": "mode-tag", "key": "gen-key", "id": "mem-id",
             "action": "skill-action", "cat": "skill-cat",
             "score": "mem-score", "omitted": "gen-omitted", "plain": ""}

_CLIP_CLS = {None: "", "tail": "tail", "wrap": "wrap-detail",
             "wide": "wide", "wide-wrap": "wide wrap-detail"}


@dataclass(frozen=True)
class Field:
    """One value on a row. Resolves to nothing when its source is empty, and
    a row where every field does that is not rendered at all.

    source     where the value comes from: a `Span` property name, or
               `payload()` / `payload_end()` / `item()` / `const()` /
               `first()` / `joined()` / `mapped()`. A list of sources joins
               the ones that resolve, with a space.

    font       "prose" → a `<span>`; "mono" → a `<code>`.
    clip       how it survives a narrow line. None ellipsizes on the right;
               "tail" ellipsizes on the left, for paths whose end matters;
               "wrap" wraps out in detail mode; "wide" takes the whole row;
               "wide-wrap" does both.
    deco       what kind of thing this is, which picks a class base.html
               styles: "tag" a mode tag, "key" a payload key, "id" a faint
               lookup id, "action" a verb, "cat" a faint qualifier,
               "score" a relevance figure leading its row, "omitted" a note
               about what is not shown, "plain" no styling.
               None is an ordinary value.

    prefix     literal text inside the element, before the value ("in ").
    label      literal text in its own `<span>` before the element.
    transform  callable applied to the displayed text; `title` keeps the
               original, so a path can read as ~/x and still hover in full.
    title      hover text. Defaults to the untransformed value, or to none
               for a decorated field — a mode tag repeating itself on hover
               says nothing. Pass a source for something else, False for
               none at all. **A bare string is a property name, not a
               literal** — `title="code"` means the `code` property, and a
               literal tooltip is `title=const("…")`.
    more       a list source; renders "+N more" after the element when that
               list holds more than one entry.
    copy       the tooltip for a copy button, which appears when this is set.
    full       the key of one of the scope's `Full`s, which puts an
               open-in-a-new-tab icon after this value. The icon is drawn
               whether or not what is on the row was cut short: a reader
               cannot see that a value fits, only that it ends, and an
               affordance that appears only sometimes has to be hunted for.
    sep_if     a source; when it resolves, a "·" separates this field from
               what precedes it. For a divider that depends on a *particular*
               neighbour being present rather than on any at all.
    """

    source: Any = None
    font: str = "prose"
    clip: Optional[str] = None
    deco: Optional[str] = None
    prefix: str = ""
    label: Optional[str] = None
    transform: Optional[Callable[[str], str]] = None
    title: Any = None
    more: Any = None
    copy: Optional[str] = None
    full: Optional[str] = None
    sep_if: Any = None

    def cell(self, span, ctx) -> Optional[dict]:
        sources = self.source if isinstance(self.source, list) else [self.source]
        parts = [_resolve(s, span, ctx) for s in sources]
        parts = [str(p) for p in parts if p not in (None, "", [], {})]
        if not parts:
            return None
        raw = " ".join(parts)
        text = self.transform(raw) if self.transform else raw

        if self.title is None:
            title = None if self.deco else raw
        elif self.title is False:
            title = None
        else:
            title = _resolve(self.title, span, ctx)

        more = _resolve(self.more, span, ctx)
        more_count = len(more) - 1 if isinstance(more, (list, tuple)) else 0

        # A key naming a Full the scope does not declare resolves to nothing
        # rather than to a broken link; `check_table` reports it at load,
        # which is where a spec's mistakes belong.
        full = (ctx.get("fulls") or {}).get(self.full) if self.full else None

        classes = []
        if self.deco:
            classes.append(_DECO_CLS.get(self.deco, ""))
        elif self.font != "mono":
            classes.append("path")
        classes.append(_CLIP_CLS.get(self.clip, ""))
        cls = " ".join(c for c in classes if c)

        # Key names avoid dict's own attributes: Jinja resolves `c.copy` to
        # `dict.copy` (a truthy bound method) before it looks for the key, so
        # a key called `copy` or `items` renders as a method repr.
        return {
            "el": "code" if self.font == "mono" else "span",
            "cls": cls,
            "text": self.prefix + text,
            "title": title,
            "label": self.label,
            "lrm": self.clip == "tail",
            "more": more_count if more_count > 0 else 0,
            "copy_text": raw if self.copy else None,
            "copy_title": self.copy,
            "full": full.link(span, ctx) if full is not None else None,
            "sep": bool(self.sep_if is not None
                        and _resolve(self.sep_if, span, ctx)),
        }


@dataclass(frozen=True)
class Alt:
    """The first of several fields that resolves, used where a value has
    alternative sources that render differently — a patch names either its
    `path` or the files inside its V4A text, never both.

    fields     tried in order; the first one that resolves wins, and the
               rest are not rendered.
    """

    fields: Sequence[Field]

    def cell(self, span, ctx) -> Optional[dict]:
        for f in self.fields:
            cell = f.cell(span, ctx)
            if cell is not None:
                return cell
        return None


# --- rows ------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """A line of fields. Drops out entirely when nothing on it resolves, so
    a spec never emits an empty row for an absent key.

    cells      the `Field`s (or `Alt`s) on the line, in order, separated by
               a space.
    layer      which layout this row belongs to: "summary" shows only on the
               collapsed line, "detail" only in the detail view, None both.
               This is the whole of the split — base.html does the rest.
    cls        one extra CSS class, where a row needs its own spacing
               ("task-goal", "ctx").
    when_many  inside an `Each`, hold this row back unless the list has two
               or more entries — for a label a lone entry does not need.
    """

    cells: Sequence[Union[Field, Alt]] = ()
    layer: Optional[str] = None
    cls: Optional[str] = None
    when_many: bool = False

    def rows(self, span, ctx):
        # `when_many` holds back a row that only earns its place in a list:
        # inside an Each, a lone op is already named by the tag above it.
        if self.when_many and ctx.get("count", 0) < 2:
            return []
        cells = [c for c in (f.cell(span, ctx) for f in self.cells)
                 if c is not None]
        if not cells:
            return []
        return [{"kind": "fields", "cells": cells,
                 "cls": _row_cls(self.layer, self.cls)}]


@dataclass(frozen=True)
class Diff:
    """A replaced pair, through the same − / + macro every patch-shaped
    scope uses. Detail-only, since a replacement runs long.

    old        source for the − side; omit for an add.
    new        source for the + side; omit for a remove. An empty string is
               a real side (a patch deleting its matched text) and renders,
               where an absent key does not.
    when       render only if this source resolves. For a pair that exists
               *because* of something else — a memory's previous text makes
               the comparison worth drawing, and without it a lone + row
               just repeats the value already on the row above.
    """

    old: Any = None
    new: Any = None
    when: Any = None

    def rows(self, span, ctx):
        if self.when is not None and not _resolve(self.when, span, ctx):
            return []
        old = _resolve(self.old, span, ctx)
        new = _resolve(self.new, span, ctx)
        if old is None and new is None:
            return []
        return [{"kind": "diff", "old": old, "new": new}]


@dataclass(frozen=True)
class Items:
    """A list shown as first-plus-count on the summary line and one row per
    entry in detail — urls, todo items. Both layouts in one declaration,
    which is why it is a row kind rather than two `Row`s.

    source     a list of strings.
    """

    source: Any = None

    def rows(self, span, ctx):
        values = _resolve(self.source, span, ctx)
        if not isinstance(values, (list, tuple)) or not values:
            return []
        # `entries`, not `items`: see the note on key names in Field.cell
        return [{"kind": "items", "entries": [str(v) for v in values]}]


@dataclass(frozen=True)
class Link:
    """A link to a page another tab serves (ADR 4, ADR 9).

    endpoint   a Flask endpoint name ("mem0.search_event"), never a raw
               href: the tab that owns the page owns its URL, and Flask
               fails loudly on a name that does not exist.
    params     {query parameter: source} — read off the span like any field.
    text       a source for the link text — `const("…")` for a literal —
               and takes `transform` where the wording depends on what is
               being linked to. The row drops when it resolves to nothing,
               so a link to a page that does not exist yet never renders.
    transform  callable applied to the resolved text.
    title      a source for the hover text; `const("…")` for a literal.
    layer      as on `Row`.
    cls        one extra class on the row.
    before     fields rendered before the link on the same row, for a
               provenance line that reads as a sentence.
    after      fields rendered after it.

    Resolves to an endpoint and its parameters, not to a URL — `url_for` is
    the template's to call, so a spec stays data and this module stays
    free of an app context.
    """

    endpoint: str = ""
    params: dict = dc_field(default_factory=dict)
    text: Any = None
    transform: Optional[Callable[[str], str]] = None
    title: Any = None
    layer: Optional[str] = None
    cls: Optional[str] = None
    before: Sequence[Any] = ()
    after: Sequence[Any] = ()

    def rows(self, span, ctx):
        if not self.endpoint:
            return []
        params = {}
        for name, source in self.params.items():
            value = _resolve(source, span, ctx)
            if value is not None:
                params[name] = value
        # A bare string is a source here as everywhere else; `const()` is how
        # a literal is written. Falling back to "treat the string as a
        # literal when it does not resolve" was tried and was wrong: it
        # turned an absent value into the source's own name on the page.
        text = _resolve(self.text, span, ctx)
        if text in (None, ""):
            return []
        if self.transform:
            text = self.transform(text)
        title = _resolve(self.title, span, ctx)
        sides = {}
        for side in ("before", "after"):
            sides[side] = [c for c in (f.cell(span, ctx)
                                       for f in getattr(self, side))
                           if c is not None]
        return [{"kind": "link", "endpoint": self.endpoint, "params": params,
                 "text": str(text), "title": title,
                 "before": sides["before"], "after": sides["after"],
                 "cls": _row_cls(self.layer, self.cls)}]


@dataclass(frozen=True)
class Each:
    """Rows repeated per entry of a list — one op of a memory batch, one
    stat of a session_search mode.

    source     a list; each entry in turn becomes what `item()` reads.
    rows_spec  the rows to emit for every entry. Emitted per entry in order,
               so a label and the diff beneath it stay together rather than
               every label preceding every diff.
    limit      show only the first N, where the rest belong behind a link.

    Entry count reaches `Row(when_many=True)`, which is how a per-entry label
    is held back when there is only one.
    """

    source: Any = None
    rows_spec: Sequence[Any] = ()
    limit: Optional[int] = None

    def rows(self, span, ctx):
        values = _resolve(self.source, span, ctx)
        if not isinstance(values, (list, tuple)) or not values:
            return []
        if self.limit is not None:
            values = values[:self.limit]
        out = []
        for value in values:
            inner = dict(ctx, item=value, count=len(values))
            for spec in self.rows_spec:
                out.extend(spec.rows(span, inner))
        return out


def _row_cls(layer: Optional[str], extra: Optional[str]) -> str:
    """The row's classes after `span-detail`. Layer is the whole of the
    summary/detail split — see the module docstring."""
    parts = []
    if layer == "detail":
        parts.append("list-item")
    elif layer == "summary":
        parts.append("list-compact")
    if extra:
        parts.append(extra)
    return (" " + " ".join(parts)) if parts else ""


# --- scopes ----------------------------------------------------------


# The macros templates/turns/_scope_*.html define for scopes that are not
# declarative, and that turn.html dispatches between. Kept
# here so a spec naming one that does not exist is reported at load instead
# of silently rendering as a payload dump — the template's `{% elif %}` chain
# would simply not match it. Adding a name here means adding the macro there.
RENDER_MACROS = frozenset({"llm"})


@dataclass(frozen=True)
class Scope:
    """One scope's rendering.

    Set **either** `rows` or `render`, never both:

    - `rows` — the ordinary case. A list of row descriptors (`Row`, `Diff`,
      `Items`, `Each`) resolved against the span and painted by a template
      that knows nothing about this particular tool.
    - `render` — the escape hatch. Names a macro in
      `templates/turns/_scope_*.html` which renders the scope by hand,
      for the
      few whose layout the vocabulary above should not grow to express. Only
      the names in `RENDER_MACROS` exist; a spec naming anything else is
      rejected at load with a message saying so.

    `render` is effectively internal: adding a new one means writing a macro
    in this app's template, which a contributed spec module cannot do. It is
    not a way to render a scope with your own code — a spec selects payload
    keys, and the markup stays the template's. If a scope of yours seems to
    need it, that is the signal the vocabulary is missing something, which is
    a conversation to have upstream (ADR 7).

    `fulls` is independent of that choice: rows and `render=` scopes alike
    declare the complete values they can open on a page of their own (ADR
    12), and the route that serves one reads this same list. A `render=`
    scope's macro names a key by hand where a `Field` would carry `full=`.
    """

    rows: Sequence[Any] = ()
    render: Optional[str] = None
    fulls: Sequence[Full] = ()

    def full_named(self, key: str) -> Optional[Full]:
        return next((f for f in self.fulls if f.key == key), None)

    def resolve(self, span, accessors=None, readers=None) -> list:
        ctx = {"accessors": accessors or {}, "readers": readers or {},
               "fulls": {f.key: f for f in self.fulls}}
        out = []
        for spec in self.rows:
            out.extend(spec.rows(span, ctx))
        return out


@dataclass(frozen=True)
class SpecTable:
    """Specs by scope name, and by category for the scopes that have no
    stable name — an llm span is named for the provider that answered
    (`anthropic`), so it is the category that identifies it.

    Name wins over category, so a spec can single out one tool inside a
    category that already has one.

    `readers` is the other half of a contribution (ADR 17): the payload
    readings a spec names as bare-string sources. It travels with the specs
    because it arrives with them, from the same module, and dies with them
    when that tab is disabled.
    """

    by_name: dict = dc_field(default_factory=dict)
    by_category: dict = dc_field(default_factory=dict)
    readers: dict = dc_field(default_factory=dict)

    def lookup(self, span) -> Optional[Scope]:
        spec = self.by_name.get(getattr(span, "name", None))
        if spec is None:
            spec = self.by_category.get(getattr(span, "category", None))
        return spec

    def merged_with(self, by_name: dict, by_category: dict,
                    readers: Optional[dict] = None) -> "SpecTable":
        """This table with another module's specs laid over it. Later wins:
        the in-tree table is a default, not a floor (ADR 7)."""
        return SpecTable({**self.by_name, **by_name},
                         {**self.by_category, **by_category},
                         {**self.readers, **(readers or {})})

    def overrides_of(self, by_name: dict, by_category: dict,
                     readers: Optional[dict] = None) -> list:
        """Which names another module would take over, for the startup line.
        An override is legitimate and announced, never silent."""
        return sorted([n for n in by_name if n in self.by_name]
                      + [f"category:{c}" for c in by_category
                         if c in self.by_category]
                      + [f"reader:{r}" for r in (readers or {})
                         if r in self.readers])


def rows_for(span, table: SpecTable, accessors=None) -> Optional[list]:
    """The rows for this span, or None to fall back to the generic payload
    renderer — which is what an unknown scope, a spec that resolves to
    nothing, or a spec that raises all get.

    A broken spec must never take a turn page down: the page polls every 2 s
    and holds many spans, so one bad entry degrades to the payload dump the
    reader would have had anyway (ADR 5's per-tab failure model, one level
    down).
    """
    spec = table.lookup(span)
    if spec is None or spec.render:
        return None
    try:
        rows = spec.resolve(span, accessors, table.readers)
    except Exception:  # noqa: BLE001 - a spec may come from outside this tree
        return None
    return rows or None


def render_macro(span, table: SpecTable) -> Optional[str]:
    """The macro name for a scope that keeps hand-written Jinja, else None."""
    spec = table.lookup(span)
    return spec.render if spec is not None else None


def full_for(span, table: SpecTable, key: str) -> Optional[Full]:
    """The `Full` this span's scope declares under `key`, or None.

    The route serving the full page and the row offering the link both come
    through here, so a key that has been renamed or withdrawn stops working
    in both places at once rather than leaving a live link to a 404.
    """
    spec = table.lookup(span)
    return spec.full_named(key) if spec is not None else None


def full_link(span, table: SpecTable, key: str, accessors=None) -> Optional[dict]:
    """The link to one of this span's full values, for a hand-written macro.

    A declarative row gets this through `Field(full=…)`; a `render=` scope
    has no Field to carry it, so its macro asks for the link by the same key
    the scope declared.
    """
    full = full_for(span, table, key)
    if full is None:
        return None
    return full.link(span, {"accessors": accessors or {},
                            "readers": table.readers})


def resolve_source(source, span, accessors=None, readers=None):
    """One source resolved outside any row — a `Full`'s title and note.

    Those two are sources like everything else, so a scope can take its
    heading from the payload it is showing; they are just not attached to a
    `Field`. A source that raises costs its line rather than the page, as
    everywhere else a spec is read.
    """
    try:
        return _resolve(source, span, {"accessors": accessors or {},
                                       "readers": readers or {}})
    except Exception:  # noqa: BLE001 - a spec may come from outside this tree
        return None


def resolve_full(span, full: Full, accessors=None, readers=None):
    """The value behind a `Full`, whatever type its source resolves to.

    Left as it came: a string stays a string, a structure stays a structure,
    and what to do about that is the renderer's business, not the
    vocabulary's.
    """
    return _resolve(full.source, span, {"accessors": accessors or {},
                                        "readers": readers or {}})


def check_table(by_name: dict, by_category: dict) -> list:
    """What is wrong with a contributed spec table, as a list of messages.

    Run at load rather than at render, because both mistakes it catches fail
    the same silent way otherwise: `rows_for` gives up and the scope renders
    as a payload dump, which is exactly what it looked like before the module
    was installed. A spec that does nothing must not be indistinguishable
    from one that was never wired up.
    """
    problems = []
    for label, table in (("SCOPES", by_name),
                         ("SCOPES_BY_CATEGORY", by_category)):
        for key, spec in table.items():
            where = f"{label}[{key!r}]"
            if not isinstance(spec, Scope):
                problems.append(f"{where} is {type(spec).__name__}, not a Scope")
                continue
            if spec.render and spec.render not in RENDER_MACROS:
                known = ", ".join(sorted(RENDER_MACROS))
                problems.append(
                    f"{where} names render={spec.render!r}, which is not a "
                    f"macro this app defines (have: {known}). Declare the "
                    f"scope with rows instead.")
            if spec.render and spec.rows:
                problems.append(f"{where} sets both rows and render")
            if not spec.render and not spec.rows:
                problems.append(f"{where} sets neither rows nor render")
            problems.extend(_full_problems(where, spec))
    return problems


def check_readers(readers: dict) -> list:
    """What is wrong with a contributed span-reader table (ADR 17).

    The same reasoning as `check_table`: every fault here otherwise shows up
    as a row that quietly does not render, which is indistinguishable from
    the module not being installed. A reader is a name a spec can put where
    a `Span` property would go, so the name has to be one — anything else
    could never be reached by a source and is a typo with no other symptom.
    """
    problems = []
    if not isinstance(readers, dict):
        return [f"SPAN_READERS is {type(readers).__name__}, not a dict"]
    for name, reader in readers.items():
        where = f"SPAN_READERS[{name!r}]"
        if not isinstance(name, str) or not name.isidentifier():
            problems.append(f"{where} is not a name a spec could use as a "
                            "source")
        elif not callable(reader):
            problems.append(f"{where} is {type(reader).__name__}, not "
                            "callable — a reader is fn(span) -> value")
    return problems


def _full_problems(where: str, spec: "Scope") -> list:
    """What is wrong with a scope's `Full`s and the fields naming them.

    Checked at load for the same reason the rest is: every one of these
    mistakes otherwise shows up as an icon that opens a page saying the key
    is unknown — a link the reader has to click to discover is broken, on a
    page they opened precisely because they could not see the value.
    """
    problems = []
    seen = set()
    for full in spec.fulls:
        if not isinstance(full, Full):
            problems.append(f"{where} has a {type(full).__name__} in fulls, "
                            f"not a Full")
            continue
        label = f"{where} full {full.key!r}"
        if not full.key:
            problems.append(f"{where} has a Full with no key")
        elif set(full.key) - _FULL_KEY_CHARS:
            problems.append(f"{label} is not a URL segment: use lower case, "
                            f"digits, '-' and '_'")
        elif full.key in seen:
            problems.append(f"{label} is declared twice")
        seen.add(full.key)
        if full.render not in FULL_RENDERERS:
            known = ", ".join(sorted(FULL_RENDERERS))
            problems.append(f"{label} names render={full.render!r} "
                            f"(have: {known})")
        if full.source is None:
            problems.append(f"{label} has no source, so there is nothing to "
                            f"open")
    for field_spec in _fields_of(spec.rows):
        if field_spec.full and field_spec.full not in seen:
            problems.append(
                f"{where} has a Field(full={field_spec.full!r}) but the scope "
                f"declares no Full with that key"
                + (f" (have: {', '.join(sorted(seen))})" if seen else ""))
    return problems


def _fields_of(rows) -> list:
    """Every Field in a row list, including inside Alt and Each.

    A spec is data, and data from outside this tree at that, so this walks
    what it finds rather than what it expects: anything without the
    attributes it is looking for is simply not a Field.
    """
    out = []
    for row in rows or ():
        for cell in getattr(row, "cells", ()) or ():
            if isinstance(cell, Field):
                out.append(cell)
            elif isinstance(cell, Alt):
                out.extend(f for f in cell.fields if isinstance(f, Field))
        for side in ("before", "after"):
            out.extend(f for f in (getattr(row, side, ()) or ())
                       if isinstance(f, Field))
        out.extend(_fields_of(getattr(row, "rows_spec", ()) or ()))
    return out
