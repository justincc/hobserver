"""Scope specs — the vocabulary, the lookup, override and degradation (ADR 7).

What each scope *shows* is covered by test_prompts.py, which asserts against
the rendered page. These tests cover the machinery underneath it: that a spec
can read a payload without a Span property, that a contributed table overrides
this tree's, and that a bad spec degrades to the generic renderer rather than
taking a page down.
"""

import pathlib

import pytest

from conftest import REPO_ROOT
from plugins.prompts.assembler import Span
from plugins.prompts.scope_spec import (RENDER_MACROS, Alt, Diff, Each, Field,
                                        Items, Link, Row, Scope, SpecTable,
                                        accessor, attr, check_table, const,
                                        first, item,
                                        joined, mapped, payload, payload_end,
                                        render_macro, rows_for)
from plugins.prompts.scopes import SCOPES, SCOPES_BY_CATEGORY


def make_span(name="acme_widget", category="tool", start=None, end=None):
    return Span(uuid="u1", name=name, category=category, session_id="s1",
                parent_uuid=None, start_us=0, end_us=1_000, metadata={},
                category_profile={}, start_data=start, end_data=end,
                model_name=None, tool_call_id=None, api_request_id=None,
                turn_id="t1", line_no=1)


def table(by_name=None, by_category=None):
    return SpecTable(by_name or {}, by_category or {})


def only_cell(rows):
    """The single cell of a single-row result, for the field tests."""
    assert len(rows) == 1, rows
    assert len(rows[0]["cells"]) == 1, rows[0]
    return rows[0]["cells"][0]


# --- sources ---------------------------------------------------------


def test_a_field_reads_a_payload_key_with_no_property_behind_it():
    """The point of `payload=`: a scope declarable by someone who cannot add
    a Span property to this tree."""
    span = make_span(start={"widget": "left-handed"})
    spec = Scope(rows=[Row([Field(payload("widget"))])])
    assert only_cell(spec.resolve(span))["text"] == "left-handed"


def test_a_field_reads_a_span_property_by_bare_name():
    span = make_span(name="terminal", start={"command": "ls -la"})
    spec = Scope(rows=[Row([Field("command")])])
    assert only_cell(spec.resolve(span))["text"] == "ls -la"


def test_a_payload_source_reads_a_json_string_payload():
    """Payloads arrive as JSON strings as well as dicts; a spec must not care
    which, because the ATOF spec says the shape is opaque."""
    span = make_span(start='{"widget": "right"}')
    spec = Scope(rows=[Row([Field(payload("widget"))])])
    assert only_cell(spec.resolve(span))["text"] == "right"


def test_payload_end_reads_the_output_side():
    span = make_span(start={}, end={"result": "done"})
    spec = Scope(rows=[Row([Field(payload_end("result"))])])
    assert only_cell(spec.resolve(span))["text"] == "done"


def test_a_missing_source_drops_its_row_entirely():
    span = make_span(start={})
    spec = Scope(rows=[Row([Field(payload("absent"))])])
    assert spec.resolve(span) == []


def test_an_unreadable_payload_yields_no_rows_rather_than_raising():
    for bad in (None, "not json", 42, ["a"]):
        span = make_span(start=bad)
        spec = Scope(rows=[Row([Field(payload("widget"))])])
        assert spec.resolve(span) == []


def test_first_joined_and_mapped_read_list_shapes():
    span = make_span(start={"ops": [{"text": "one"}, {"text": "two"}]})
    spec = Scope(rows=[Row([Field(first(mapped(payload("ops"), "text")),
                                  title=joined(mapped(payload("ops"), "text")),
                                  more=payload("ops"))])])
    cell = only_cell(spec.resolve(span))
    assert cell["text"] == "one"
    assert cell["title"] == "one\ntwo"
    assert cell["more"] == 1


def test_const_supplies_a_literal():
    span = make_span(start={"x": 1})
    spec = Scope(rows=[Row([Field(const("fixed words"))])])
    assert only_cell(spec.resolve(span))["text"] == "fixed words"


# --- the four axes ---------------------------------------------------


@pytest.mark.parametrize("font,clip,deco,el,cls", [
    ("mono", "wrap", None, "code", "wrap-detail"),
    ("mono", None, None, "code", ""),
    ("prose", None, None, "span", "path"),
    ("prose", "tail", None, "span", "path tail"),
    ("prose", "wide", None, "span", "path wide"),
    ("prose", "wide-wrap", None, "span", "path wide wrap-detail"),
    ("prose", None, "tag", "span", "mode-tag"),
    ("prose", None, "id", "span", "mem-id"),
    ("prose", None, "cat", "span", "skill-cat"),
    ("prose", None, "action", "span", "skill-action"),
    ("prose", None, "plain", "span", ""),
])
def test_the_axes_resolve_to_the_classes_base_html_styles(font, clip, deco,
                                                          el, cls):
    """The four axes are the whole vocabulary, and these are the class names
    base.html actually styles. A fifth axis needs to earn itself."""
    span = make_span(start={"v": "x"})
    spec = Scope(rows=[Row([Field(payload("v"), font=font, clip=clip,
                                  deco=deco)])])
    cell = only_cell(spec.resolve(span))
    assert (cell["el"], cell["cls"]) == (el, cls)


def test_tail_clipping_carries_the_bidi_marks():
    """A left-ellipsized path is wrapped in &lrm; so the filename end reads
    correctly however the path is scripted."""
    span = make_span(start={"path": "/a/b.py"})
    spec = Scope(rows=[Row([Field(payload("path"), clip="tail")])])
    assert only_cell(spec.resolve(span))["lrm"] is True


def test_a_title_defaults_to_the_untransformed_value():
    """The title holds the path in full while the text is collapsed to ~."""
    span = make_span(start={"path": "/home/somebody/x"})
    spec = Scope(rows=[Row([Field(payload("path"),
                                  transform=lambda p: "~" + p[14:])])])
    cell = only_cell(spec.resolve(span))
    assert cell["text"] == "~/x" and cell["title"] == "/home/somebody/x"


def test_a_decorated_field_gets_no_title_by_default():
    """A mode tag repeating itself on hover says nothing."""
    span = make_span(start={"mode": "replace"})
    spec = Scope(rows=[Row([Field(payload("mode"), deco="tag")])])
    assert only_cell(spec.resolve(span))["title"] is None


def test_layer_is_the_whole_of_the_summary_detail_split():
    span = make_span(start={"v": "x"})
    for layer, cls in [("detail", " list-item"), ("summary", " list-compact"),
                       (None, "")]:
        spec = Scope(rows=[Row([Field(payload("v"))], layer=layer)])
        assert spec.resolve(span)[0]["cls"] == cls


def test_a_row_can_carry_an_extra_class_beside_its_layer():
    span = make_span(start={"v": "x"})
    spec = Scope(rows=[Row([Field(payload("v"))], layer="detail",
                           cls="task-goal")])
    assert spec.resolve(span)[0]["cls"] == " list-item task-goal"


# --- composite rows --------------------------------------------------


def test_alt_takes_the_first_field_that_resolves():
    spec = Scope(rows=[Row([Alt([Field(payload("path")),
                                 Field(payload("fallback"))])])])
    assert only_cell(spec.resolve(
        make_span(start={"path": "p"})))["text"] == "p"
    assert only_cell(spec.resolve(
        make_span(start={"fallback": "f"})))["text"] == "f"


def test_each_keeps_a_label_with_its_own_diff():
    """One pass per entry, so an op's label stays with the pair beneath it
    rather than every label preceding every diff."""
    span = make_span(start={"ops": [{"action": "add", "new": "one"},
                                    {"action": "remove", "old": "two"}]})
    spec = Scope(rows=[Each(payload("ops"), [
        Row([Field(item("action"), deco="action")], layer="detail"),
        Diff(item("old"), item("new")),
    ])])
    kinds = [(r["kind"], r.get("cells", [{}])[0].get("text") or r.get("new")
              or r.get("old")) for r in spec.resolve(span)]
    assert kinds == [("fields", "add"), ("diff", "one"),
                     ("fields", "remove"), ("diff", "two")]


def test_when_many_holds_a_row_back_for_a_lone_entry():
    """A single op is already named by the mode tag above it."""
    spec = Scope(rows=[Each(payload("ops"), [
        Row([Field(item("action"), deco="action")], layer="detail",
            when_many=True),
    ])])
    one = make_span(start={"ops": [{"action": "add"}]})
    two = make_span(start={"ops": [{"action": "add"}, {"action": "remove"}]})
    assert spec.resolve(one) == []
    assert len(spec.resolve(two)) == 2


def test_items_renders_a_list_as_one_row_kind():
    span = make_span(start={"urls": ["a", "b"]})
    spec = Scope(rows=[Items(payload("urls"))])
    rows = spec.resolve(span)
    assert rows == [{"kind": "items", "entries": ["a", "b"]}]


def test_a_diff_with_neither_side_drops_out():
    span = make_span(start={})
    assert Scope(rows=[Diff(payload("o"), payload("n"))]).resolve(span) == []


def test_an_empty_new_string_still_renders_its_side():
    """A patch that deletes the matched text passes "" — a real side, and
    distinct from the key being absent. Both routes to the value have to keep
    that difference: `payload=` for a contributed spec, and the property for
    this tree's own."""
    span = make_span(start={"old": "gone", "new": ""})
    assert Scope(rows=[Diff(payload("old"), payload("new"))]).resolve(span) \
        == [{"kind": "diff", "old": "gone", "new": ""}]

    patch = make_span(name="patch", start={"old_string": "gone",
                                           "new_string": ""})
    assert Scope(rows=[Diff("patch_old_string", "patch_new_string")]) \
        .resolve(patch) == [{"kind": "diff", "old": "gone", "new": ""}]


def test_an_absent_key_is_not_an_empty_one():
    """The distinction the line above depends on: no key at all means the
    side does not exist, and the row shows one side only."""
    span = make_span(start={"old": "gone"})
    assert Scope(rows=[Diff(payload("old"), payload("new"))]).resolve(span) \
        == [{"kind": "diff", "old": "gone", "new": None}]


def test_a_field_still_drops_an_empty_value():
    """There is nothing to put on a row for "", even though a Diff can use
    it — the emptiness is the content there, not here."""
    span = make_span(start={"v": ""})
    assert Scope(rows=[Row([Field(payload("v"))])]).resolve(span) == []


# --- links and published data (ADR 9) --------------------------------


def test_a_link_resolves_to_an_endpoint_and_params_not_a_url():
    """`url_for` stays in the template, so a spec is data and this module
    needs no app context."""
    span = make_span(start={"q": "tea"})
    rows = Scope(rows=[Link("mem0.search_event",
                            params={"query": payload("q"), "ts": "start_us"},
                            text=const("open →"))]).resolve(span)
    assert rows == [{"kind": "link", "endpoint": "mem0.search_event",
                     "params": {"query": "tea", "ts": 0},
                     "text": "open →", "title": None,
                     "before": [], "after": [], "cls": ""}]


def test_a_link_drops_when_its_text_does_not_resolve():
    """An open search has no result count yet, and a link to a page that
    cannot be described is worse than no link."""
    spec = Scope(rows=[Link("mem0.search_event", text="mem0_result_count")])
    assert spec.resolve(make_span(start={})) == []


def test_link_text_treats_a_bare_string_as_a_source_not_a_literal():
    """The trap this closes: falling back to the literal turned an absent
    value into the source's own name on the page."""
    spec = Scope(rows=[Link("x", text="no_such_property")])
    assert spec.resolve(make_span()) == []


def test_an_accessor_reads_another_plugins_published_lookup():
    seen = {}

    def lookup(key, before):
        seen["args"] = (key, before)
        return {"text": "what it said before", "event_id": 7}

    span = make_span(start={"memory_id": "m-1"})
    spec = Scope(rows=[Row([Field(attr(accessor("prior", payload("memory_id")),
                                       "text"))])])
    rows = spec.resolve(span, {"prior": lookup})
    assert only_cell(rows)["text"] == "what it said before"
    assert seen["args"] == ("m-1", 0)      # key, and the span start in seconds


def test_an_absent_accessor_drops_its_row_rather_than_failing():
    """A plugin that is not registered is a state every caller handles."""
    span = make_span(start={"memory_id": "m-1"})
    spec = Scope(rows=[Row([Field(accessor("nobody", payload("memory_id")))])])
    assert spec.resolve(span, {}) == []
    assert spec.resolve(span, None) == []


def test_an_accessor_that_raises_drops_its_row():
    """Another plugin's code must not 500 a turn page."""
    def boom(key, before):
        raise RuntimeError("db gone")

    span = make_span(start={"memory_id": "m-1"})
    spec = Scope(rows=[Row([Field(accessor("prior", payload("memory_id")))])])
    assert spec.resolve(span, {"prior": boom}) == []


def test_an_accessor_is_not_called_without_a_key():
    called = []
    spec = Scope(rows=[Row([Field(accessor("prior", payload("absent")))])])
    spec.resolve(make_span(start={}), {"prior": lambda *a: called.append(a)})
    assert called == []


def test_each_limit_takes_the_first_n():
    span = make_span(start={"xs": ["a", "b", "c", "d"]})
    spec = Scope(rows=[Each(payload("xs"),
                            [Row([Field(item())])], limit=2)])
    assert [only_cell([r])["text"] for r in spec.resolve(span)] == ["a", "b"]


def test_a_gated_diff_needs_its_reason_to_exist():
    """A mem0_update with no recovered previous text showed a lone + row
    repeating the text already on the row above. The pair exists because
    there is something to compare against."""
    spec = Scope(rows=[Diff(attr(accessor("prior", payload("id")), "text"),
                            payload("text"),
                            when=accessor("prior", payload("id")))])
    span = make_span(start={"id": "m-1", "text": "the new text"})
    assert spec.resolve(span, {}) == []                       # no prior
    assert spec.resolve(span, {"prior": lambda k, b: {"text": "the old"}}) \
        == [{"kind": "diff", "old": "the old", "new": "the new text"}]


# --- lookup, override, degradation -----------------------------------


def test_lookup_is_by_scope_name():
    spec = Scope(rows=[Row([Field(payload("v"))])])
    t = table({"acme_widget": spec})
    assert rows_for(make_span(start={"v": "x"}), t) is not None
    assert rows_for(make_span(name="other", start={"v": "x"}), t) is None


def test_lookup_falls_back_to_category_for_scopes_with_no_stable_name():
    """An llm span is named for the provider that answered it."""
    t = table({}, {"llm": Scope(render="llm")})
    assert render_macro(make_span(name="anthropic", category="llm"), t) == "llm"
    assert render_macro(make_span(name="openai", category="llm"), t) == "llm"


def test_a_name_wins_over_its_category():
    by_name = {"anthropic": Scope(render="special")}
    t = table(by_name, {"llm": Scope(render="llm")})
    assert render_macro(make_span(name="anthropic", category="llm"), t) == "special"
    assert render_macro(make_span(name="openai", category="llm"), t) == "llm"


def test_a_contributed_spec_overrides_a_curated_one():
    """The in-tree table is a default, not a floor (ADR 7)."""
    mine = Scope(rows=[Row([Field(payload("theirs"))])])
    merged = SpecTable({"terminal": SCOPES["terminal"]}, {}).merged_with(
        {"terminal": mine}, {})
    span = make_span(name="terminal", start={"command": "ls",
                                             "theirs": "override"})
    assert only_cell(rows_for(span, merged))["text"] == "override"


def test_an_override_is_named_so_the_banner_can_report_it():
    base = SpecTable({"terminal": SCOPES["terminal"]}, {"llm": Scope()})
    assert base.overrides_of({"terminal": Scope()}, {}) == ["terminal"]
    assert base.overrides_of({}, {"llm": Scope()}) == ["category:llm"]
    assert base.overrides_of({"brand_new": Scope()}, {}) == []


def test_merging_leaves_the_original_table_untouched():
    base = SpecTable({"terminal": SCOPES["terminal"]}, {})
    base.merged_with({"terminal": Scope()}, {})
    assert base.by_name["terminal"] is SCOPES["terminal"]


def test_a_spec_that_raises_degrades_to_the_generic_renderer():
    """One bad spec must not take down a page that polls every 2 s and holds
    many spans (ADR 5's failure model, one level down)."""
    class Exploding:
        def rows(self, span, ctx):
            raise RuntimeError("boom")

    t = table({"acme_widget": Scope(rows=[Exploding()])})
    assert rows_for(make_span(start={"v": "x"}), t) is None


def test_a_spec_resolving_to_nothing_falls_back_to_the_payload():
    """An empty result means the spec had nothing to say about this span, so
    the payload dump is better than a bare row."""
    t = table({"acme_widget": Scope(rows=[Row([Field(payload("absent"))])])})
    assert rows_for(make_span(start={"v": "x"}), t) is None


def test_a_render_scope_yields_no_rows_and_names_its_macro():
    t = table({"mem0_search": Scope(render="mem0_search")})
    span = make_span(name="mem0_search", start={"query": "q"})
    assert rows_for(span, t) is None
    assert render_macro(span, t) == "mem0_search"


# --- a tab contributes its own specs (ADR 10) ------------------------


def build_app(mem0=True, prompts_settings=None):
    """An app with the real tabs, so the contribution path is exercised end
    to end rather than mocked."""
    import app as app_module
    import tabs as tabs_module

    specs = [tabs_module.TabSpec(module="plugins.prompts",
                                 settings=prompts_settings or {})]
    if mem0:
        specs.append(tabs_module.TabSpec(module="plugins.mem0"))
    return app_module.create_app(tabs_module.load_tabs(specs))


def test_the_mem0_tab_contributes_its_own_span_rendering():
    """The specs live in plugins/mem0/scopes.py and arrive because that tab
    loaded, not because the Prompts tab knows about mem0."""
    table = build_app(mem0=True).config["SCOPE_SPECS"]
    for name in ("mem0_search", "mem0_add", "mem0_update", "mem0_delete"):
        assert name in table.by_name, name


def test_disabling_the_tab_takes_its_specs_with_it():
    """The reason spec lifetime is tied to the tab: a link into a page nobody
    serves must not be left behind."""
    table = build_app(mem0=False).config["SCOPE_SPECS"]
    assert not [n for n in table.by_name if n.startswith("mem0")]
    assert "terminal" in table.by_name          # the rest is unaffected


def test_the_prompts_tab_depends_on_no_other_plugin():
    """It reads `app.extensions`; it never imports another plugin or names
    one in code. Prose may still discuss mem0 — the docstrings explain why
    the coupling went — so this looks at imports and at string literals that
    are not documentation, which is where a hardcoded lookup would hide.
    """
    import ast

    source = (REPO_ROOT / "plugins" / "prompts" / "__init__.py").read_text()
    tree = ast.parse(source)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "mem0" not in (node.module or ""), ast.dump(node)
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "mem0" not in alias.name, alias.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                assert "mem0" not in node.value, f"line {node.lineno}: {node.value!r}"


def test_contributed_specs_are_collected_before_any_tab_registers():
    """A painting tab resolves its table in init_app, during registration —
    so collection has to happen first, whatever order the config lists."""
    for mem0_first in (True, False):
        import app as app_module
        import tabs as tabs_module
        specs = [tabs_module.TabSpec(module="plugins.mem0"),
                 tabs_module.TabSpec(module="plugins.prompts")]
        if not mem0_first:
            specs.reverse()
        app = app_module.create_app(tabs_module.load_tabs(specs))
        assert "mem0_search" in app.config["SCOPE_SPECS"].by_name, mem0_first


def test_the_shell_carries_contributed_tables_without_reading_them():
    """tabs.py must not learn what a scope spec is — ADR 5's rule that the
    shell imports no plugin."""
    source = (REPO_ROOT / "tabs.py").read_text()
    assert "scope_spec" not in source and "import plugins" not in source


# --- the in-tree table -----------------------------------------------


def test_every_shipped_spec_is_either_declarative_or_names_a_macro():
    for name, spec in {**SCOPES, **SCOPES_BY_CATEGORY}.items():
        assert bool(spec.rows) != bool(spec.render), name


def test_the_escape_hatch_is_down_to_llm_alone():
    """ADR 7 shipped three; ADR 9 found two of them were the same missing
    vocabulary — a link and a published accessor — and declared them. If this
    grows again, ask the same question rather than routing around it."""
    macros = {s.render for s in {**SCOPES, **SCOPES_BY_CATEGORY}.values()
              if s.render}
    assert macros == {"llm"} == set(RENDER_MACROS)


def test_every_render_macro_named_is_defined_and_dispatched():
    """RENDER_MACROS is only useful if it tracks the templates. A name here
    with no macro renders as a payload dump and says nothing about why; a
    macro with no dispatch arm never runs.

    The macros live in `templates/prompts/_scope_*.html`, one file per
    subject; the dispatch that picks between them is on the turn page. Found
    by glob rather than by filename, so splitting or merging those files is
    not something this test has an opinion about.
    """
    # Found through the blueprint rather than by path: the Prompts tab carries
    # its own templates, and where a plugin keeps them is its business.
    import plugins.prompts as prompts_tab

    prompts = (pathlib.Path(prompts_tab.__file__).parent
               / prompts_tab.bp.template_folder / "prompts")
    macros = "\n".join(f.read_text() for f in prompts.glob("_scope_*.html"))
    page = (prompts / "turn.html").read_text()
    assert macros, "no _scope_*.html files found"
    for macro in RENDER_MACROS:
        assert f"macro {macro}_rows(" in macros, f"{macro}: no macro"
        assert f'spec_render == "{macro}"' in page, f"{macro}: no dispatch"
        assert f"{macro}_rows" in page, f"{macro}: macro not imported"


# --- a contributed table is checked at load, not at render -----------


def test_an_unknown_render_macro_is_refused_with_the_reason():
    """The silent version of this is a payload dump indistinguishable from a
    module that was never wired up."""
    faults = check_table({"deploy": Scope(render="my_own_macro")}, {})
    assert len(faults) == 1
    assert "my_own_macro" in faults[0] and "not a macro this app defines" in faults[0]
    assert "llm" in faults[0]  # names the ones that do exist


def test_a_known_render_macro_is_accepted_from_a_contributed_table():
    """Reusing one is legitimate — someone with a second llm provider scope
    wants exactly the rendering this app already has."""
    assert check_table({"vertex": Scope(render="llm")}, {}) == []


def test_setting_both_rows_and_render_is_refused():
    both = Scope(rows=[Row([Field(payload("v"))])], render="llm")
    assert "both rows and render" in check_table({"x": both}, {})[0]


def test_setting_neither_is_refused():
    assert "neither rows nor render" in check_table({"x": Scope()}, {})[0]


def test_a_value_that_is_not_a_scope_is_refused():
    assert "not a Scope" in check_table({"x": {"rows": []}}, {})[0]


def test_the_check_covers_the_category_table_too():
    faults = check_table({}, {"llm": Scope(render="nope")})
    assert faults and "SCOPES_BY_CATEGORY['llm']" in faults[0]


def test_this_trees_own_table_passes_its_own_check():
    assert check_table(SCOPES, SCOPES_BY_CATEGORY) == []


def test_a_bad_render_name_reaches_the_banner(tmp_path, monkeypatch):
    from plugins.prompts import spec_table

    name = write_spec_module(tmp_path, monkeypatch, "badrender_specs", '''
from plugins.prompts.scope_spec import Scope
SCOPES = {"deploy": Scope(render="my_own_macro")}
''')
    built, notes = spec_table({"scope_specs": [name]})
    assert "my_own_macro" in notes[0]["problem"]
    assert "deploy" not in built.by_name   # skipped, not half-loaded


# --- contributed spec modules, loaded from settings ------------------


def write_spec_module(tmp_path, monkeypatch, name, body):
    """A spec module on sys.path, as an installed one would be."""
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def test_a_contributed_module_extends_the_table_without_a_fork(tmp_path,
                                                               monkeypatch):
    """The whole point (ADR 7 / design-principles §1): a tool this tree has
    never heard of, displayed without editing this tree."""
    from plugins.prompts import spec_table

    name = write_spec_module(tmp_path, monkeypatch, "acme_specs", '''
from plugins.prompts.scope_spec import Field, Row, Scope, payload
SCOPES = {"acme_widget": Scope(rows=[Row([Field(payload("widget"))])])}
''')
    built, notes = spec_table({"scope_specs": [name]})
    span = make_span(start={"widget": "left-handed"})
    assert only_cell(rows_for(span, built))["text"] == "left-handed"
    assert notes[0]["problem"] is None


def test_a_bare_string_setting_is_accepted_as_one_module(tmp_path,
                                                         monkeypatch):
    from plugins.prompts import spec_modules

    assert spec_modules({"scope_specs": "one.module"}) == ["one.module"]
    assert spec_modules({"scope_specs": ["a", "b"]}) == ["a", "b"]
    assert spec_modules({}) == []


def test_a_contributed_override_is_reported_not_silent(tmp_path, monkeypatch):
    from plugins.prompts import spec_table

    name = write_spec_module(tmp_path, monkeypatch, "override_specs", '''
from plugins.prompts.scope_spec import Field, Row, Scope, payload
SCOPES = {"terminal": Scope(rows=[Row([Field(payload("cmdline"))])])}
''')
    built, notes = spec_table({"scope_specs": [name]})
    span = make_span(name="terminal", start={"command": "ls",
                                             "cmdline": "theirs"})
    assert only_cell(rows_for(span, built))["text"] == "theirs"
    assert "overriding terminal" in notes[0]["from"]


def test_a_module_that_cannot_be_imported_is_reported_and_skipped():
    """The tab still serves, on this tree's own specs — a broken contribution
    is not a reason to lose the log (ADR 5's failure model)."""
    from plugins.prompts import spec_table

    built, notes = spec_table({"scope_specs": ["no_such_module_anywhere"]})
    assert notes[0]["problem"].startswith("ModuleNotFoundError")
    assert built.by_name["terminal"] is SCOPES["terminal"]


def test_a_module_with_no_specs_is_reported(tmp_path, monkeypatch):
    from plugins.prompts import spec_table

    name = write_spec_module(tmp_path, monkeypatch, "empty_specs", "X = 1\n")
    _, notes = spec_table({"scope_specs": [name]})
    assert "no SCOPES" in notes[0]["problem"]


def test_a_module_whose_table_is_the_wrong_shape_is_reported(tmp_path,
                                                             monkeypatch):
    from plugins.prompts import spec_table

    name = write_spec_module(tmp_path, monkeypatch, "bad_specs",
                             "SCOPES = ['not', 'a', 'dict']\n")
    _, notes = spec_table({"scope_specs": [name]})
    assert "must be a dict" in notes[0]["problem"]


def test_contributed_modules_appear_in_the_startup_sources(tmp_path,
                                                           monkeypatch):
    """Reported through the same hook as the log itself, so an override or a
    failed import is visible in the banner beside what it renders."""
    from plugins.prompts import sources

    entries = sources({"atof_log": str(tmp_path / "nope.jsonl"),
                       "scope_specs": ["no_such_module_anywhere"]})
    labels = [e["label"] for e in entries]
    assert labels == ["ATOF log", "scope spec"]
    assert entries[1]["problem"].startswith("ModuleNotFoundError")


# --- the docstrings are the reference, so they have to stay true ------


def test_every_field_parameter_is_documented_on_the_class():
    """The docstrings are what an editor shows while you type, and
    span-rendering.md points at them rather than repeating the values. An
    undocumented parameter is one nobody will find."""
    import dataclasses
    doc = Field.__doc__
    for f in dataclasses.fields(Field):
        assert f"\n    {f.name} " in doc, f.name


def test_every_row_parameter_is_documented_on_the_class():
    import dataclasses
    doc = Row.__doc__
    for f in dataclasses.fields(Row):
        assert f"\n    {f.name} " in doc, f.name


def test_the_field_docstring_names_every_value_the_code_accepts():
    """A value the code supports but the docstring omits is undiscoverable;
    one the docstring claims but the code drops silently misleads."""
    from plugins.prompts.scope_spec import _CLIP_CLS, _DECO_CLS
    doc = Field.__doc__
    for deco in _DECO_CLS:
        assert f'"{deco}"' in doc, f"deco {deco}"
    for clip in _CLIP_CLS:
        if clip is not None:
            assert f'"{clip}"' in doc, f"clip {clip}"
    for font in ("prose", "mono"):
        assert f'"{font}"' in doc, font


def test_the_row_docstring_names_every_layer():
    doc = Row.__doc__
    for layer in ("summary", "detail"):
        assert f'"{layer}"' in doc, layer


def test_every_class_the_vocabulary_resolves_to_exists_in_base_html():
    """base.html is a published surface (ADR 8). A spec naming a class that
    has been renamed renders unstyled and says nothing about why, so the
    rename has to fail here instead.

    Looks for a real rule, not the name anywhere in the file: base.html's own
    header comment lists these classes, and a substring check is satisfied by
    the documentation while the styling is gone.
    """
    import re

    from plugins.prompts.scope_spec import _CLIP_CLS, _DECO_CLS
    css = (REPO_ROOT / "templates" / "base.html").read_text()
    css = re.sub(r"\{#.*?#\}", "", css, flags=re.S)     # drop Jinja comments
    named = set(list(_DECO_CLS.values()) + list(_CLIP_CLS.values()))
    named |= {"list-item", "list-compact", "span-detail", "path"}
    for entry in named:
        for cls in entry.split():          # "wide wrap-detail" is two
            # the class in a selector that opens a rule on the same line
            assert re.search(rf"\.{re.escape(cls)}\b[^{{}}\n]*\{{", css), cls


def test_cell_keys_avoid_dicts_own_attributes():
    """Jinja resolves `c.copy` to `dict.copy` before it looks for the key, so
    a key named after a dict method renders as a bound method."""
    span = make_span(start={"v": "x"})
    cell = only_cell(Scope(rows=[Row([Field(payload("v"))])]).resolve(span))
    for key in cell:
        assert not hasattr({}, key), key
    rows = Scope(rows=[Items(payload("urls"))]).resolve(
        make_span(start={"urls": ["a"]}))
    for key in rows[0]:
        assert not hasattr({}, key), key
