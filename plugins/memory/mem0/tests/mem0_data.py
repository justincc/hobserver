"""Mem0's own test data and app builders.

Lives with the plugin, not in the shell's root conftest: the events schema and
its sample rows are mem0's knowledge, and travel with mem0 if it is lifted out
of this tree. Imported by `plugins/memory/mem0/tests/conftest.py` for the fixtures and
by the mem0 tests that build an app directly.
"""

import sqlite3

from testkit import make_app

# A non-existent path rather than nothing: a settings key left empty would fall
# through to this machine's real hermes log, so a test that does not care about
# a source would silently read live data.
NONEXISTENT_ATOF = "/nonexistent/atof.jsonl"
NONEXISTENT_DB = "/nonexistent/events.db"


def mem0_app(db=None):
    """An app serving the Mem0 tab alone, pointed at a test event log."""
    return make_app([
        {"plugin": "plugins.memory.mem0", "settings": {"db": db or NONEXISTENT_DB}},
    ])


def turns_with_mem0_app(atof=None, db=None):
    """Turns and Mem0 together — for the cross-tab tests where a mem0 span is
    rendered on a turn page and links back into the Mem0 tab (ADR 4, ADR 10)."""
    return make_app([
        {"plugin": "plugins.turns", "settings": {"atof_log": atof or NONEXISTENT_ATOF}},
        {"plugin": "plugins.memory.mem0", "settings": {"db": db or NONEXISTENT_DB}},
    ])


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


def make_memory_db(path):
    db = sqlite3.connect(path)
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
        " 'mem0_search', 'sessionabc', 'tool search terms',"
        " '{\"results\": [{\"memory\": \"a remembered fact\", \"score\": 0.53}]}')"
    )
    db.execute(
        "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
        " query, result) VALUES (5, '2026-07-09T13:00:00+00:00', 1783688400.0,"
        " 'prefetch', 'sessionabc', 'fifth query', 'fifth result')"
    )
    db.commit()
    db.close()


def make_memory_change_db(path):
    """An event log built around the search → change pattern.

    A mem0_search surfaces two memories; an update rewrites one and a delete
    removes the other, each naming only the id. A later search re-reports the
    updated memory with its new text, so a lookup that ignored the change
    time would pick the wrong side. Timestamps are round epoch seconds, 30 s
    and 45 s after the first search, mirroring the real gaps.
    """
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    rows = [
        (1, 2000.0, "mem0_search", "what does the user prefer",
         '{"count": 2, "results":'
         ' [{"id": "aaa11111", "memory": "the old fact", "score": 0.81},'
         '  {"id": "bbb22222", "memory": "the doomed fact", "score": 0.44}]}',
         None),
        (2, 2030.0, "mem0_update", "the new fact",
         '{"result": "Memory updated.", "memory_id": "aaa11111"}',
         '{"args": {"memory_id": "aaa11111", "text": "the new fact"}}'),
        (3, 2045.0, "mem0_delete", "bbb22222",
         '{"result": "Memory deleted.", "memory_id": "bbb22222"}',
         '{"args": {"memory_id": "bbb22222"}}'),
        (4, 2100.0, "mem0_add", "a brand new fact",
         '{"result": "Fact queued for storage.", "event_id": "queue-1"}',
         '{"args": {"content": "a brand new fact"}}'),
        (5, 2200.0, "mem0_search", "what does the user prefer now",
         '{"count": 1, "results":'
         ' [{"id": "aaa11111", "memory": "the new fact", "score": 0.9}]}',
         None),
    ]
    for row_id, ts, kind, query, result, extra in rows:
        db.execute(
            "INSERT INTO events (id, ts_utc, ts_epoch, event_type, session_id,"
            " platform, query, result, extra) VALUES (?, ?, ?, ?, 's9',"
            " 'webui', ?, ?, ?)",
            (row_id, f"epoch-{ts}", ts, kind, query, result, extra),
        )
    db.commit()
    db.close()
