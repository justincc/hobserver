"""Memory plugin tests — the pre-plugin app.py behaviour under /memory/."""


def test_index_lists_all_events(client):
    page = client.get("/memory/").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "second query" in page
    assert '/memory/event/1' in page
    assert '/memory/event/2' in page


def test_index_orders_newest_first(client):
    page = client.get("/memory/").get_data(as_text=True)
    assert page.index("second query") < page.index("please commit the changes")


def test_event_detail_shows_query_result_and_metadata(client):
    page = client.get("/memory/event/1").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "remembered thing one" in page
    for field in ("ts_utc", "event_type", "session_id", "platform",
                  "result_len", "memory_count", "elapsed_ms"):
        assert field in page
    assert "sessionabc" in page
    assert "982.5" in page


def test_fragment_returns_only_newer_events(client):
    page = client.get("/memory/fragment/events?since=3").get_data(as_text=True)
    assert 'data-event-id="5"' in page
    assert 'data-event-id="4"' in page
    assert 'data-event-id="3"' not in page
    # newest first, ready to prepend
    assert page.index('data-event-id="5"') < page.index('data-event-id="4"')


def test_fragment_empty_when_no_new_events(client):
    assert client.get("/memory/fragment/events?since=5").get_data(as_text=True).strip() == ""


def test_index_polls_fragment_endpoint(client):
    page = client.get("/memory/").get_data(as_text=True)
    assert '"/memory/fragment/events"' in page
    assert "let lastId = 5" in page


def test_event_detail_prev_next_links(client):
    # id 1 is the oldest: next goes to 2, no previous
    page = client.get("/memory/event/1").get_data(as_text=True)
    assert '/memory/event/2' in page
    assert 'class="disabled">&larr; previous' in page
    # id 2 is in the middle: links to both neighbours
    page = client.get("/memory/event/2").get_data(as_text=True)
    assert '/memory/event/1' in page
    assert '/memory/event/3' in page
    # id 5 is the newest: previous goes to 4, no next
    page = client.get("/memory/event/5").get_data(as_text=True)
    assert '/memory/event/4' in page
    assert 'class="disabled">next' in page


def test_event_detail_context_messages_from_same_session(client):
    # event 3 shares sessionabc with event 1; event 2 (no session) is excluded
    page = client.get("/memory/event/3").get_data(as_text=True)
    assert "Context Messages" in page
    assert "[#1]" in page
    assert "please commit the changes" in page
    assert "second query" not in page
    # context messages sit below the result
    assert page.index("<h2>Result</h2>") < page.index("<h2>Context Messages</h2>")


def test_context_messages_exclude_tool_call_events(client):
    # event 5's session context: prefetch events 1 and 3, not tool event 4
    page = client.get("/memory/event/5").get_data(as_text=True)
    assert "[#1]" in page
    assert "[#3]" in page
    assert "[#4]" not in page
    assert "tool search terms" not in page


def test_event_detail_context_messages_none_for_first_in_session(client):
    page = client.get("/memory/event/1").get_data(as_text=True)
    assert "Context Messages" in page
    assert "(none)" in page


def test_json_result_is_pretty_printed(client):
    # event 4's mem0_search result is a one-line JSON blob; the detail page
    # shows it indented, one key per line
    page = client.get("/memory/event/4").get_data(as_text=True)
    assert '{\n  &#34;results&#34;: [\n    {\n      &#34;memory&#34;: &#34;a remembered fact&#34;' in page
    assert '&#34;score&#34;: 0.53' in page


def test_non_json_result_is_left_unchanged(client):
    page = client.get("/memory/event/1").get_data(as_text=True)
    assert "## Mem0 Memory\n- remembered thing one" in page


def test_event_detail_missing_id_returns_404(client):
    assert client.get("/memory/event/999").status_code == 404
