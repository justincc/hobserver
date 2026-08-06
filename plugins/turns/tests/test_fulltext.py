"""Rendering one whole value for the page that shows it (ADR 12).

The value is someone else's log text, so two things matter more than the
prettiness of the output: that markup inside it is never run, and that a
renderer which is not there degrades the page rather than breaking it.
"""

import plugins.turns.fulltext as fulltext
from plugins.turns.fulltext import render


def test_markdown_becomes_html():
    out = render("## Goal\n\n- one\n- two\n", "markdown")
    assert out.kind == "markdown"
    assert "<h2>Goal</h2>" in out.html
    assert out.html.count("<li>") == 2


def test_the_text_is_kept_beside_the_html():
    """The rendering is a view of the value, never a replacement for it: the
    raw view and the char count both come from the text."""
    out = render("# Title\n", "markdown")
    assert out.text == "# Title\n"
    assert out.chars == len("# Title\n")


def test_html_inside_the_value_is_shown_not_run():
    """A prompt is log text, not markup. The renderer is configured with raw
    HTML disabled, so a script tag in someone's prompt renders as the
    characters it is."""
    out = render("before\n\n<script>alert(1)</script>\n\nafter", "markdown")
    assert "<script>" not in out.html
    assert "&lt;script&gt;" in out.html


def test_an_inline_html_attribute_cannot_escape_either():
    out = render('<img src=x onerror="alert(1)">', "markdown")
    assert "<img" not in out.html
    assert "&lt;img" in out.html


def test_a_javascript_link_is_not_left_clickable():
    """markdown-it validates link targets: the text stays, the link does
    not."""
    out = render("[click](javascript:alert(1))", "markdown")
    assert 'href="javascript:' not in out.html
    assert "<a " not in out.html


def test_an_ordinary_link_still_works():
    out = render("[docs](https://example.invalid/x)", "markdown")
    assert '<a href="https://example.invalid/x">docs</a>' in out.html


def test_text_render_leaves_the_value_alone():
    out = render("## not a heading here\n", "text")
    assert out.kind == "text" and out.html is None
    assert out.text == "## not a heading here\n"


def test_a_structure_is_json_whatever_the_scope_asked_for():
    """Markdown of a dict would be markdown of its punctuation. This is a
    fallback, not a failure, so it carries no problem — the page names the
    form it is showing either way."""
    out = render({"b": 1, "a": [2, 3]}, "markdown")
    assert out.kind == "json"
    assert out.problem is None
    assert '"b": 1' in out.text and "\n" in out.text     # indented
    assert out.html is None


def test_a_structure_that_json_cannot_hold_still_renders():
    out = render({"when": object()}, "text")
    assert out.kind == "json" and out.text


def test_a_value_that_is_not_there_says_so():
    out = render(None, "markdown")
    assert out.text is None
    assert "not in the payload" in out.problem


def test_a_missing_renderer_degrades_to_text_and_says_why(monkeypatch):
    """A dependency that is absent or broken must cost the rendering, not the
    page (design principle 1, degrade per component)."""
    monkeypatch.setattr(fulltext, "_MD", None)
    monkeypatch.setattr(fulltext, "_MD_PROBLEM", "ImportError: no markdown_it")
    out = render("## Goal", "markdown")
    assert out.kind == "text"
    assert out.text == "## Goal"
    assert "no markdown_it" in out.problem


def test_a_renderer_that_raises_degrades_the_same_way(monkeypatch):
    class Exploding:
        def render(self, text):
            raise RuntimeError("boom")

    monkeypatch.setattr(fulltext, "_MD", Exploding())
    out = render("## Goal", "markdown")
    assert out.kind == "text" and out.text == "## Goal"
    assert "boom" in out.problem


def test_a_quarter_megabyte_request_renders():
    """The real ones are this size — a whole conversation, repeated on every
    call. Nothing here chunks or truncates, so the only question is whether
    it completes."""
    text = "## `user`\n\n" + ("some prose about a jobs report. " * 8000)
    out = render(text, "markdown")
    assert out.kind == "markdown" and out.chars > 250_000


# --- sections: a value that is several labelled parts --------------------


def sections(*pairs):
    return [{"label": label, "text": text} for label, text in pairs]


def test_each_section_is_rendered_under_its_own_label():
    out = render(sections(("user", "## Ask\n\nhello"),
                          ("assistant", "**done**")), "sections")
    assert out.kind == "sections"
    assert [s.label for s in out.sections] == ["user", "assistant"]
    assert "<h2>Ask</h2>" in out.sections[0].html
    assert "<strong>done</strong>" in out.sections[1].html


def test_a_label_never_reaches_the_html_of_its_own_section():
    """The reason for the shape: a label written into the markdown is one
    more heading among the model's own."""
    out = render(sections(("user", "just prose")), "sections")
    assert "user" not in out.sections[0].html


def test_the_section_text_is_kept_verbatim_beside_the_rendering():
    out = render(sections(("user", "  spaced\n\nout  ")), "sections")
    assert out.sections[0].text == "  spaced\n\nout  "


def test_sections_count_every_character_they_hold():
    out = render(sections(("a", "12345"), ("b", "678")), "sections")
    assert out.chars == 8
    assert out.text is None          # there is no single text to show


def test_an_unlabelled_section_is_named_rather_than_left_blank():
    out = render([{"label": None, "text": "x"}], "sections")
    assert out.sections[0].label == "(unlabelled)"


def test_html_inside_a_section_is_shown_not_run():
    out = render(sections(("user", "<script>alert(1)</script>")), "sections")
    assert "<script>" not in out.sections[0].html
    assert "&lt;script&gt;" in out.sections[0].html


def test_a_value_that_is_not_sections_falls_back_to_being_rendered_whole():
    """A scope declaring sections over something else gets the plainest true
    thing, not an error."""
    assert render("## just a string", "sections").kind == "markdown"
    assert render({"a": 1}, "sections").kind == "json"
    assert render([{"label": "a"}], "sections").kind == "json"  # no text key
    # an empty list is a value, and not the same thing as an absent one — so
    # it shows as the `[]` it is rather than as "nothing under this key"
    empty = render([], "sections")
    assert empty.kind == "json" and empty.text == "[]"


def test_a_missing_renderer_leaves_the_sections_and_says_why(monkeypatch):
    monkeypatch.setattr(fulltext, "_MD", None)
    monkeypatch.setattr(fulltext, "_MD_PROBLEM", "ImportError: no markdown_it")
    out = render(sections(("user", "## Ask")), "sections")
    assert out.kind == "sections"
    assert out.sections[0].html is None
    assert out.sections[0].text == "## Ask"      # …the page shows this instead
    assert "no markdown_it" in out.problem
