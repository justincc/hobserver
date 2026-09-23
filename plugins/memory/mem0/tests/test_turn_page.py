"""The Mem0 tab's spans on a Turns turn page (ADR 4, ADR 9, ADR 10).

This tab contributes scope specs and span readers to the Turns tab, so what
they render is this tab's to test. These build an app with both tabs, and
depend on the Turns tab for the page and its ATOF fixtures — the Turns suite
itself imports nothing from here, so it runs with this tab removed.
"""

from mem0_data import make_memory_change_db, make_memory_db, turns_with_mem0_app
from plugins.turns.tests.streams import mark_line, scope_lines
from testkit import make_app


def make_client(tmp_path, atof_path):
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    return turns_with_mem0_app(atof=atof_path, db=str(db_path)).test_client()


def write_atof(tmp_path, lines, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_search_query_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_650_000, 1_700_000, name="mem0_search",
                     session="s1", turn="t1",
                     start_data={"query": "user timezone preference",
                                 "rerank": True, "top_k": 10}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert "user timezone preference" in page
    # the query wraps out in full in the detailed layout
    assert '<code class="wrap-detail" title="user timezone preference">' in page


def test_mem0_add_content_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, 1_600_000, name="mem0_add",
                     session="s1", turn="t1",
                     start_data={"content": "User prefers tea over coffee."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert "User prefers tea over coffee." in page
    # the full fact wraps out in the detailed layout
    assert '<span class="path wide wrap-detail" title="User prefers tea over coffee.">' in page


def test_mem0_update_and_delete_show_the_memory_id(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, 1_300_000, name="mem0_update",
                     session="s1", turn="t1",
                     start_data={"memory_id": "fdd806c1-0789-4522-aaf3",
                                 "text": "User prefers coffee after all."}),
        *scope_lines("M2", "tool", 1_350_000, 1_400_000, name="mem0_delete",
                     session="s1", turn="t1",
                     start_data={"memory_id": "b3e3ade6-d852-44b2-98f4"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    # the replacement text leads the summary line, wrapping out in detail
    text = '<span class="path wide wrap-detail" title="User prefers coffee after all.">'
    assert text in page
    assert "list-item" not in page[page.index(text) - 200:page.index(text)]
    # the id is faint and copyable, and detail-only (.list-item) so it never
    # shares the summary line with the span's own uuid
    mem_id = '<span class="detail-id">fdd806c1-0789-4522-aaf3'
    assert mem_id in page and 'data-copy="fdd806c1-0789-4522-aaf3"' in page
    assert "list-item" in page[page.index(mem_id) - 200:page.index(mem_id)]
    # a delete has nothing but the id
    delete_id = '<span class="detail-id">b3e3ade6-d852-44b2-98f4'
    assert delete_id in page
    assert "list-item" in page[page.index(delete_id) - 200:page.index(delete_id)]


def test_turn_detail_shows_top_mem0_results_and_links_to_memory(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1",
                     start_data={"query": "job preferences", "top_k": 10},
                     end_data='{"count": 4, "results":'
                     ' [{"id": "b760576d", "memory": "top fact", "score": 0.8042},'
                     '  {"id": "f9c1f7ee", "memory": "next fact", "score": 0.5339},'
                     '  {"id": "fb54073a", "memory": "third fact", "score": 0.4229},'
                     '  {"id": "c78490d3", "memory": "fourth fact", "score": 0.3115}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "top fact" in page and "next fact" in page and "third fact" in page
    assert "fourth fact" not in page         # the preview stops at three
    assert "0.80" in page and "0.53" in page and "0.42" in page
    assert "b760576d" in page                # each hit's id, for the lookup
    # the handoff to the Mem0 tab carries what identifies the logged call
    assert "/memory/mem0/search-event?" in page
    assert "session=s1" in page
    assert "query=job+preferences" in page
    assert "ts=1100000" in page
    assert "all 4 results" in page


def test_mem0_spans_lose_both_halves_when_that_tab_is_off(tmp_path):
    """The rows and the reading behind them are one contribution with one
    lifetime (ADR 10, ADR 17). Without the Mem0 tab, nothing in this tree
    knows what a mem0 search result is, and the span falls back to the
    payload dump any unknown tool gets."""
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"},
                     end_data='{"count": 1, "results":'
                     ' [{"id": "a", "memory": "top fact", "score": 0.9}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    app = make_app(entries=[{"plugin": "plugins.turns",
                             "settings": {"atof_log": str(atof)}}])
    page = app.test_client().get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert "mem0_search" in page               # the span is still there
    assert 'class="detail-score"' not in page     # but not mem0's rows
    assert "/memory/mem0/search-event" not in page


def test_turn_detail_mem0_link_reads_full_result_when_nothing_is_hidden(tmp_path):
    # with three or fewer hits the preview is the whole list, so promising
    # "all 3 results" behind the link would be promising nothing new
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"},
                     end_data='{"count": 2, "results":'
                     ' [{"id": "a", "memory": "one", "score": 0.9},'
                     '  {"id": "b", "memory": "two", "score": 0.8}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    # the link's own text, not its tooltip — the tooltip still describes the
    # target page as carrying all the results, which it does
    link_text = page[page.index("search-event"):]
    link_text = link_text[link_text.index('">') + 2:link_text.index("</a>")]
    # a literal glyph, not an entity: the spec table writes literals (· − ↗)
    # and Jinja leaves them alone. Leading ↗ marks it as a link to another
    # page, matching the Turns tab's own cross-page links.
    assert link_text.strip() == "↗ view full result in Mem0"


def test_turn_detail_mem0_link_absent_while_the_search_is_open(tmp_path):
    # nothing came back yet, so there is nothing to link to
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"}),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "/memory/mem0/search-event" not in page


def test_turn_detail_web_search_gets_no_mem0_result_rows(tmp_path):
    # web_search shares the query branch but has no mem0 results behind it
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="web_search",
                     session="s1", turn="t1",
                     start_data={"query": "flask blueprints"},
                     end_data='{"count": 1, "results":'
                     ' [{"id": "x", "memory": "not a memory", "score": 0.9}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "flask blueprints" in page
    assert "not a memory" not in page
    assert "/memory/mem0/search-event" not in page


def _change_client(tmp_path, atof_path):
    """A client whose event log carries the search → change pattern, so the
    turn page can recover what a mem0 memory said before a span changed it."""
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    return turns_with_mem0_app(atof=atof_path, db=str(db_path)).test_client()


def _change_stream(name, memory_id, start_data):
    # the fixture log's first search is at epoch 2000 s; the span sits 30 s
    # later, so the search precedes the change exactly as in the real log
    return [
        mark_line("hermes.turn.start", 2_020_000_000, session="s9", turn="t1"),
        *scope_lines("M1", "tool", 2_030_000_000, 2_031_000_000, name=name,
                     session="s9", turn="t1", start_data=start_data),
        mark_line("hermes.turn.end", 2_050_000_000, session="s9", turn="t1"),
    ]


def test_turn_detail_shows_what_an_update_replaced(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "the old fact" in page             # recovered from the event log
    assert "the new fact" in page             # the span's own payload
    assert '<span class="diff-mark del">' in page      # both diff sides
    assert '<span class="diff-mark ins">' in page
    assert "previous text from the local log" in page
    assert "/memory/mem0/event/1" in page          # the search it came from
    assert "30 s earlier" in page


def test_turn_detail_recovers_a_deleted_memory_with_no_new_side(tmp_path):
    # a delete's payload is only an id, so without the recovered text the row
    # says nothing about what was lost — and there is no "+" side to show
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "the doomed fact" in page
    assert "previous text from the local log" in page
    assert '<span class="diff-mark ins">' not in page


def test_delete_summary_line_leads_with_the_deleted_memory(tmp_path):
    # a delete's payload is only an id, so with details off the row would say
    # nothing about what was lost; the recovered text leads instead, the way
    # a mem0_add's own content does
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_delete"):page.index('class="detail-id"')]
    assert "the doomed fact" in summary
    # .list-compact, so detail mode drops it rather than repeating the text
    # the − row below already carries in full
    assert 'class="span-detail list-compact"' in summary


def test_delete_summary_line_absent_when_nothing_was_recovered(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "unknown-id", {"memory_id": "unknown-id"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_delete"):page.index('class="detail-id"')]
    assert "list-compact" not in summary


def test_update_summary_line_still_leads_with_its_own_new_text(tmp_path):
    # an update carries a fact of its own, so the recovered text must not
    # displace it — the old text belongs on the detail-only − row
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_update"):page.index('class="detail-id"')]
    assert "the new fact" in summary
    assert "the old fact" not in summary


def test_turn_detail_previous_text_names_the_local_log_not_mem0(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "Not retrieved from mem0" in page
    assert "mem0 is never queried" in page


def test_turn_detail_without_a_matching_memory_shows_no_previous_text(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "unknown-id", {"memory_id": "unknown-id"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "previous text from the local log" not in page
    assert "unknown-id" in page               # the id itself still shows


def test_turn_detail_renders_without_the_memory_plugin_lookup(tmp_path):
    # ADR 4: the Turns tab does without when the lookup is not published,
    # rather than reaching into the event log itself
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    app = turns_with_mem0_app(atof=str(atof), db=str(db_path))
    app.config["TESTING"] = True
    app.extensions.pop("mem0_prior_text")
    page = app.test_client().get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert page.count("mem0_update") >= 1     # the span still renders
    assert "the new fact" in page
    assert "the old fact" not in page
    assert "previous text from the local log" not in page
