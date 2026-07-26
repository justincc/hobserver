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


@pytest.fixture
def memory_db(tmp_path):
    """Path to a populated event log — the real thing for anything that
    validates or reads a database rather than just being handed one."""
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    return str(db_path)


@pytest.fixture
def client(memory_db):
    app = create_app(memory_db)
    app.config["TESTING"] = True
    return app.test_client()
