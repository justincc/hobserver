import sqlite3

import pytest

from app import create_app

SCHEMA = """
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    ts_utc       TEXT    NOT NULL,
    ts_epoch     REAL    NOT NULL,
    event_type   TEXT    NOT NULL,
    session_id   TEXT,
    platform     TEXT,
    query        TEXT,
    result       TEXT,
    result_len   INTEGER,
    memory_count INTEGER,
    elapsed_ms   REAL,
    extra        TEXT
);
"""


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " platform, query, result, result_len, memory_count, elapsed_ms, extra)"
        " VALUES (1, '2026-07-09T10:00:00+00:00', 1783677600.0, 'prefetch',"
        " 'sessionabc', 'webui', 'please commit the changes',"
        " '## Mem0 Memory\n- remembered thing one', 38, 10, 982.5, NULL)"
    )
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, query, result)"
        " VALUES (2, '2026-07-09T11:00:00+00:00', 1783681200.0, 'prefetch',"
        " 'second query', 'second result')"
    )
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " query, result) VALUES (3, '2026-07-09T12:00:00+00:00', 1783684800.0,"
        " 'prefetch', 'sessionabc', 'third query', 'third result')"
    )
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " query, result) VALUES (4, '2026-07-09T12:30:00+00:00', 1783686600.0,"
        " 'mem0_search', 'sessionabc', 'tool search terms', '{}')"
    )
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " query, result) VALUES (5, '2026-07-09T13:00:00+00:00', 1783688400.0,"
        " 'prefetch', 'sessionabc', 'fifth query', 'fifth result')"
    )
    db.commit()
    db.close()
    app = create_app(str(db_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_index_lists_all_events(client):
    page = client.get("/").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "second query" in page
    assert '/event/1' in page
    assert '/event/2' in page


def test_index_orders_newest_first(client):
    page = client.get("/").get_data(as_text=True)
    assert page.index("second query") < page.index("please commit the changes")


def test_event_detail_shows_query_result_and_metadata(client):
    page = client.get("/event/1").get_data(as_text=True)
    assert "please commit the changes" in page
    assert "remembered thing one" in page
    for field in ("ts_utc", "event_type", "session_id", "platform",
                  "result_len", "memory_count", "elapsed_ms"):
        assert field in page
    assert "sessionabc" in page
    assert "982.5" in page


def test_fragment_returns_only_newer_events(client):
    page = client.get("/fragment/events?since=3").get_data(as_text=True)
    assert 'data-event-id="5"' in page
    assert 'data-event-id="4"' in page
    assert 'data-event-id="3"' not in page
    # newest first, ready to prepend
    assert page.index('data-event-id="5"') < page.index('data-event-id="4"')


def test_fragment_empty_when_no_new_events(client):
    assert client.get("/fragment/events?since=5").get_data(as_text=True).strip() == ""


def test_index_polls_fragment_endpoint(client):
    page = client.get("/").get_data(as_text=True)
    assert "/fragment/events?since=" in page
    assert "let lastId = 5" in page


def test_event_detail_prev_next_links(client):
    # id 1 is the oldest: next goes to 2, no previous
    page = client.get("/event/1").get_data(as_text=True)
    assert '/event/2' in page
    assert 'class="disabled">&larr; previous' in page
    # id 2 is in the middle: links to both neighbours
    page = client.get("/event/2").get_data(as_text=True)
    assert '/event/1' in page
    assert '/event/3' in page
    # id 5 is the newest: previous goes to 4, no next
    page = client.get("/event/5").get_data(as_text=True)
    assert '/event/4' in page
    assert 'class="disabled">next' in page


def test_event_detail_context_messages_from_same_session(client):
    # event 3 shares sessionabc with event 1; event 2 (no session) is excluded
    page = client.get("/event/3").get_data(as_text=True)
    assert "Context Messages" in page
    assert "[#1]" in page
    assert "please commit the changes" in page
    assert "second query" not in page
    # context messages sit below the result
    assert page.index("<h2>Result</h2>") < page.index("<h2>Context Messages</h2>")


def test_context_messages_exclude_tool_call_events(client):
    # event 5's session context: prefetch events 1 and 3, not tool event 4
    page = client.get("/event/5").get_data(as_text=True)
    assert "[#1]" in page
    assert "[#3]" in page
    assert "[#4]" not in page
    assert "tool search terms" not in page


def test_event_detail_context_messages_none_for_first_in_session(client):
    page = client.get("/event/1").get_data(as_text=True)
    assert "Context Messages" in page
    assert "(none)" in page


def test_event_detail_missing_id_returns_404(client):
    assert client.get("/event/999").status_code == 404
