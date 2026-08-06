"""Memory plugin tests — the Mem0 tab, served under /memory/mem0/."""

import sqlite3

from conftest import make_app
from plugins import mem0


def test_index_lists_all_events(client):
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "second query" in page
    assert '/memory/mem0/event/1' in page
    assert '/memory/mem0/event/2' in page


def test_index_orders_newest_first(client):
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert page.index("second query") < page.index("please commit the changes")


def test_event_detail_shows_query_result_and_metadata(client):
    page = client.get("/memory/mem0/event/1").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "remembered thing one" in page
    for field in ("ts_utc", "event_type", "session_id", "platform",
                  "result_len", "memory_count", "elapsed_ms"):
        assert field in page
    assert "sessionabc" in page
    assert "982.5" in page


def test_fragment_returns_only_newer_events(client):
    page = client.get("/memory/mem0/fragment/events?since=3").get_data(as_text=True)
    assert 'data-event-id="5"' in page
    assert 'data-event-id="4"' in page
    assert 'data-event-id="3"' not in page
    # newest first, ready to prepend
    assert page.index('data-event-id="5"') < page.index('data-event-id="4"')


def test_fragment_empty_when_no_new_events(client):
    assert client.get("/memory/mem0/fragment/events?since=5").get_data(as_text=True).strip() == ""


def test_index_polls_fragment_endpoint(client):
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert '"/memory/mem0/fragment/events"' in page
    assert "let lastId = 5" in page


def test_event_detail_prev_next_links(client):
    # id 1 is the oldest: next goes to 2, no previous
    page = client.get("/memory/mem0/event/1").get_data(as_text=True)
    assert '/memory/mem0/event/2' in page
    assert '<span class="disabled" title="no older event">&laquo; prev</span>' in page
    # id 2 is in the middle: links to both neighbours
    page = client.get("/memory/mem0/event/2").get_data(as_text=True)
    assert '/memory/mem0/event/1' in page
    assert '/memory/mem0/event/3' in page
    # id 5 is the newest: previous goes to 4, no next
    page = client.get("/memory/mem0/event/5").get_data(as_text=True)
    assert '/memory/mem0/event/4' in page
    assert '<span class="disabled" title="no newer event">next &raquo;</span>' in page


def test_event_detail_nav_uses_shared_item_nav_layout(client):
    # Same shape as the turns turn page: back-to-list and the muted steppers
    # grouped together on one row (see templates/_item_nav.html).
    page = client.get("/memory/mem0/event/2").get_data(as_text=True)
    nav = page[page.index('<nav class="event-nav">'):]
    nav = nav[: nav.index("</nav>")]
    assert "all events" in nav
    assert '<span class="nav-steps">' in nav
    assert nav.index("all events") < nav.index("nav-steps")


def test_event_detail_context_messages_from_same_session(client):
    # event 3 shares sessionabc with event 1; event 2 (no session) is excluded
    page = client.get("/memory/mem0/event/3").get_data(as_text=True)
    assert "Context Messages" in page
    assert "[#1]" in page
    assert "please commit the changes" in page
    assert "second query" not in page
    # context messages sit below the result
    assert page.index("<h2>Result</h2>") < page.index("<h2>Context Messages</h2>")


def test_context_messages_exclude_tool_call_events(client):
    # event 5's session context: prefetch events 1 and 3, not tool event 4
    page = client.get("/memory/mem0/event/5").get_data(as_text=True)
    assert "[#1]" in page
    assert "[#3]" in page
    assert "[#4]" not in page
    assert "tool search terms" not in page


def test_event_detail_context_messages_none_for_first_in_session(client):
    page = client.get("/memory/mem0/event/1").get_data(as_text=True)
    assert "Context Messages" in page
    assert "(none)" in page


def test_json_result_is_pretty_printed(client):
    # event 4's mem0_search result is a one-line JSON blob; the detail page
    # shows it indented, one key per line
    page = client.get("/memory/mem0/event/4").get_data(as_text=True)
    assert '{\n  &#34;results&#34;: [\n    {\n      &#34;memory&#34;: &#34;a remembered fact&#34;' in page
    assert '&#34;score&#34;: 0.53' in page


def test_non_json_result_is_left_unchanged(client):
    page = client.get("/memory/mem0/event/1").get_data(as_text=True)
    assert "## Mem0 Memory\n- remembered thing one" in page


def test_event_detail_missing_id_returns_404(client):
    assert client.get("/memory/mem0/event/999").status_code == 404


def _change_client(memory_change_db):
    app = make_app(db=memory_change_db)
    app.config["TESTING"] = True
    return app.test_client()


def test_query_heading_names_what_the_column_holds(memory_change_db):
    # the column is only literally a query on the retrieval events; calling
    # it "Query" made a stored fact and a bare memory id read as searches
    c = _change_client(memory_change_db)
    assert "<h2>Query</h2>" in c.get("/memory/mem0/event/1").get_data(as_text=True)
    assert "<h2>New text</h2>" in c.get("/memory/mem0/event/2").get_data(as_text=True)
    assert "<h2>Deleted memory id</h2>" in c.get(
        "/memory/mem0/event/3").get_data(as_text=True)
    assert "<h2>Added text</h2>" in c.get("/memory/mem0/event/4").get_data(as_text=True)


def test_deleted_memory_text_recovered_from_the_log(memory_change_db):
    # a delete's whole payload is an id — mem0 cannot return the memory once
    # it is gone, but the search that surfaced the id still has its text
    page = _change_client(memory_change_db).get(
        "/memory/mem0/event/3").get_data(as_text=True)
    assert "the doomed fact" in page
    assert "Previous text" in page
    assert "/memory/mem0/event/1" in page          # links to the search it came from
    assert "45 s earlier" in page


def test_updated_memory_shows_the_text_it_replaced(memory_change_db):
    page = _change_client(memory_change_db).get(
        "/memory/mem0/event/2").get_data(as_text=True)
    assert "the old fact" in page             # recovered
    assert "the new fact" in page             # the update's own payload
    assert "30 s earlier" in page


def test_previous_text_says_it_is_local_not_mem0(memory_change_db):
    # the reader must never take this for something mem0 returned
    page = _change_client(memory_change_db).get(
        "/memory/mem0/event/2").get_data(as_text=True)
    assert "the local log" in page            # visible, not only in the tooltip
    assert "Not retrieved from mem0" in page  # the tooltip spells it out
    assert "mem0 is never queried" in page


def test_previous_text_ignores_searches_after_the_change(memory_change_db):
    # event 5 re-reports aaa11111 with its *new* text; the update at event 2
    # must still show what it replaced, not what it produced
    app = make_app(db=memory_change_db)
    with app.test_request_context():
        prior = mem0.prior_memory_text("aaa11111", 2030.0)
    assert prior["text"] == "the old fact"
    assert prior["event_id"] == 1


def test_previous_text_absent_when_no_search_surfaced_the_memory(memory_change_db):
    app = make_app(db=memory_change_db)
    with app.test_request_context():
        assert mem0.prior_memory_text("never-seen", 2030.0) is None
        # the only search naming it is later than the change
        assert mem0.prior_memory_text("aaa11111", 1999.0) is None


def test_events_without_a_change_get_no_previous_text(memory_change_db):
    # a search or an add replaces nothing, so the section must not appear
    c = _change_client(memory_change_db)
    assert "Previous text" not in c.get("/memory/mem0/event/1").get_data(as_text=True)
    assert "Previous text" not in c.get("/memory/mem0/event/4").get_data(as_text=True)


def test_gap_text_reads_in_the_right_unit():
    assert mem0._gap_text(29.8) == "30 s"
    assert mem0._gap_text(174.1) == "3 min"
    assert mem0._gap_text(9000) == "2.5 h"
    # a clock skew between the two logs must not print a negative gap
    assert mem0._gap_text(-5) == "0 s"


def test_prior_text_lookup_is_published_for_other_plugins(memory_change_db):
    # ADR 4: the Turns tab calls this rather than opening the event log
    app = make_app(db=memory_change_db)
    assert app.extensions["mem0_prior_text"] is mem0.prior_memory_text


def test_search_event_redirects_to_the_matching_event(client):
    # the Turns tab's mem0_search spans carry no event id, so they are
    # matched back to the log on (session_id, query) — event 4 in the fixture
    resp = client.get("/memory/mem0/search-event?session=sessionabc"
                      "&query=tool+search+terms")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/memory/mem0/event/4")


def test_search_event_ignores_non_search_events(client):
    # event 3 is a prefetch with its own query in the same session; only
    # mem0_search calls are what a mem0_search span can mean
    assert client.get("/memory/mem0/search-event?session=sessionabc"
                      "&query=third+query").status_code == 404


def test_search_event_unmatched_query_404s(client):
    # the two logs are written independently, so one can cover a call the
    # other does not — that must say so rather than land somewhere wrong
    resp = client.get("/memory/mem0/search-event?session=sessionabc&query=nope")
    assert resp.status_code == 404
    assert "written" in resp.get_data(as_text=True)


def test_search_event_requires_session_and_query(client):
    assert client.get("/memory/mem0/search-event").status_code == 400
    assert client.get("/memory/mem0/search-event?session=s").status_code == 400


def test_search_event_breaks_a_repeated_query_tie_by_time(tmp_path):
    # the one ambiguity (session_id, query) admits: the same search run twice
    # in a session. The span's start, passed as epoch microseconds, picks the
    # nearer of the two logged calls.
    from conftest import make_memory_db

    db_path = tmp_path / "dupes.db"
    make_memory_db(db_path)
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " query, result) VALUES (6, '2026-07-09T14:00:00+00:00', 1783692000.0,"
        " 'mem0_search', 'sessionabc', 'tool search terms', '{}')"
    )
    db.commit()
    db.close()
    app = make_app(db=str(db_path))
    app.config["TESTING"] = True
    dupe_client = app.test_client()

    # event 4 is logged at 1783686600, event 6 at 1783692000
    near_first = dupe_client.get("/memory/mem0/search-event?session=sessionabc"
                                "&query=tool+search+terms&ts=1783686599000000")
    assert near_first.headers["Location"].endswith("/memory/mem0/event/4")
    near_second = dupe_client.get("/memory/mem0/search-event?session=sessionabc"
                                 "&query=tool+search+terms&ts=1783691999000000")
    assert near_second.headers["Location"].endswith("/memory/mem0/event/6")
    # without a ts there is nothing to choose on, so it takes the earlier
    no_ts = dupe_client.get("/memory/mem0/search-event?session=sessionabc"
                            "&query=tool+search+terms")
    assert no_ts.headers["Location"].endswith("/memory/mem0/event/4")


def test_check_db_accepts_a_real_event_log(memory_db):
    assert mem0.check_db(memory_db) is None


def test_check_db_rejects_a_missing_path(tmp_path):
    assert mem0.check_db(str(tmp_path / "nope.db")) == "no such file"


def test_check_db_rejects_a_directory(tmp_path):
    # `app.py .` — the path exists, so an existence check passed it through
    # and sqlite only failed later, per-request, with "disk I/O error".
    assert mem0.check_db(str(tmp_path)) == "not a regular file"


def test_check_db_rejects_a_file_that_is_not_sqlite(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("this is not a database")
    assert "not a mem0 event log" in mem0.check_db(str(plain))


def test_check_db_rejects_a_database_without_the_events_table(tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.close()
    problem = mem0.check_db(str(other))
    assert "not a mem0 event log" in problem and "events" in problem
