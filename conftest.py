import pathlib
import sqlite3

import pytest

import tabs as tabs_module
from app import create_app

# This file sits at the repo root, so it is the one fixed point every test can
# anchor on. Tests live at two depths now — tests/ and plugins/<name>/tests/ —
# and walking up from __file__ no longer means the same thing in both.
REPO_ROOT = pathlib.Path(__file__).parent


def make_app(db=None, atof=None, entries=None):
    """An app serving the two in-tree tabs, pointed at test sources.

    Tests describe tabs the way hobserve.toml does — a list of entries — so
    what they exercise is the real config path, not a private shortcut.
    """
    # An explicit non-existent path rather than nothing: a settings key left
    # empty would fall through to this machine's real hermes log, so a test
    # that does not care about a source would silently read live data.
    if entries is None:
        entries = [
            {"module": "plugins.turns",
             "settings": {"atof_log": atof or "/nonexistent/atof.jsonl"}},
            {"module": "plugins.mem0",
             "settings": {"db": db or "/nonexistent/events.db"}},
        ]
    specs = tabs_module.parse_config({"tabs": entries})
    app = create_app(tabs_module.load_tabs(specs))
    app.config["TESTING"] = True
    return app

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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep the ATOF index (ADR 11) out of the developer's real cache dir.

    Its default path is derived from the log path, and tests point at a
    different log each run, so without this a test session leaves a fresh
    SQLite file in ~/.cache for every app it builds.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def memory_change_db(tmp_path):
    path = tmp_path / "changes.db"
    make_memory_change_db(path)
    return str(path)


@pytest.fixture
def memory_db(tmp_path):
    """Path to a populated event log — the real thing for anything that
    validates or reads a database rather than just being handed one."""
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    return str(db_path)


@pytest.fixture
def client(memory_db):
    return make_app(db=memory_db).test_client()
