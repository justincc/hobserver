"""How mem0's spans show on the Turns tab (ADR 9, ADR 10).

These render on somebody else's page — a turn waterfall — but they are this
plugin's rendering of this plugin's data, so they live here rather than in
the Turns tab's own table. `__init__` exposes them as `SCOPES`, and the
shell hands them to whichever tab paints spans; disable this tab in
`hobserver.toml` and they go with it.

Nothing here opens the mem0 store. A `Link` names a page this tab serves and
`accessor()` calls the lookup this tab published — ADR 4's two shapes, which
is all a spec is allowed. The vocabulary comes from the Turns tab because
that is what paints the rows (ADR 8: importing a published surface is
ordinary).

`docs/extending/writing-a-scope-spec.md` is the guide; this is a worked example of its
cross-plugin case.
"""

from scope_spec import (Diff, Each, Field, Link, Row, Scope,
                                        accessor, attr, const, item, payload)

# What the search got back. Only the top hits: whole facts would swamp the
# summary line, and the rest is one link away. The preview size is bound once
# — the same number decides whether the link says "all N results".
MEM0_PREVIEW = 3

# The link's wording depends on whether the preview already covered
# everything, which is the one thing here that is a callable rather than
# data. `count` arrives as a string, hence the int().
def _mem0_link_text(count):
    try:
        more = int(count) > MEM0_PREVIEW
    except (TypeError, ValueError):
        more = False
    return f"all {count} results in Mem0 →" if more else "full result in Mem0 →"


MEM0_SEARCH = Scope(rows=[
    Row([Field("search_query", font="mono", clip="wrap")]),
    Each("mem0_results", [
        # the score leads its row like a diff mark; then the fact itself
        Row([Field(item("score"), deco="score",
                   transform=lambda s: f"{float(s):.2f}",
                   title=const("mem0 relevance score")),
             Field(item("memory"), clip="wide-wrap")], layer="detail"),
        # the id on its own faint row — the lookup key mem0_update and
        # mem0_delete carry, so the pair resolves without leaving the UI
        Row([Field(item("id"), deco="id", copy="copy memory id")],
            layer="detail"),
    ], limit=MEM0_PREVIEW),
    Link("mem0.search_event",
         params={"session": "session_id", "query": "search_query",
                 "ts": "start_us"},
         text="mem0_result_count", transform=_mem0_link_text,
         title=const("Open this search in the Mem0 tab: every result, the "
                     "context messages mem0 retrieved against, and the raw "
                     "payload."),
         layer="detail"),
])

# What a memory said before this span changed it, recovered from the local
# event log — mem0 itself is never queried and could not answer (ADR 4). The
# provenance row says so, names the search it came from and how long before.
_MEM0_WAS = accessor("mem0_prior_text", payload("memory_id"))

_MEM0_PROV = Link(
    "mem0.event",
    params={"event_id": attr(_MEM0_WAS, "event_id")},
    text=attr(_MEM0_WAS, "event_id"),
    transform=lambda n: f"search #{n}",
    before=[Field(const("previous text from the local log ·"), deco="plain",
                  title=const(
                        "Not retrieved from mem0: this is the memory as it "
                        "appeared in the mem0_search event named here, the "
                        "most recent copy in the local event log. mem0 is "
                        "never queried — once a memory is deleted it cannot "
                        "return it at all, and the pre-update text is not "
                        "exposed either. A change made outside hermes in "
                        "between would therefore not be reflected here."))],
    after=[Field([attr(_MEM0_WAS, "gap_text"), const("earlier")],
                 deco="plain", prefix=", ")],
    layer="detail", cls="prov")

# The id stays off the summary line: it would read as a second uuid beside
# the span's own, and opening the row is enough to reach it.
_MEM0_ID = Row([Field(payload("memory_id"), deco="id",
                      copy="copy memory id")], layer="detail")

# An add stores a fact and has nothing before it.
MEM0_ADD = Scope(rows=[
    Row([Field(payload("content"), clip="wide-wrap")]),
])

# An update replaces one. Its own new text leads — displacing it with the old
# would hide what the span actually wrote — and the − / + pair carries both.
MEM0_UPDATE = Scope(rows=[
    Row([Field(payload("text"), clip="wide-wrap")]),
    _MEM0_ID,
    # only when there is a previous text: without one a lone + row would
    # just repeat the new text already on the row above
    Diff(attr(_MEM0_WAS, "text"), payload("text"), when=_MEM0_WAS),
    _MEM0_PROV,
])

# A delete's payload is only an id, so the recovered text leads instead —
# otherwise the row would say nothing about what was destroyed even before
# being opened. `.list-compact`, because the − row below carries it in full.
MEM0_DELETE = Scope(rows=[
    Row([Field(attr(_MEM0_WAS, "text"), clip="wide")], layer="summary"),
    _MEM0_ID,
    Diff(attr(_MEM0_WAS, "text"), None),
    _MEM0_PROV,
])


# The table this plugin contributes, keyed by the hermes tool's scope name.
# `__init__` re-exports it as SCOPES, which is what the shell picks up.
SCOPES = {
    "mem0_search": MEM0_SEARCH,
    "mem0_add": MEM0_ADD,
    "mem0_update": MEM0_UPDATE,
    "mem0_delete": MEM0_DELETE,
}
