"""Fixtures for the Mem0 tab's tests, scoped to this directory.

The event-log schema and its sample rows live in `mem0_data.py` beside this
file — mem0's own test data, no longer in the shell's root conftest.
"""

import pytest

from mem0_data import make_memory_change_db, make_memory_db, mem0_app


@pytest.fixture
def memory_db(tmp_path):
    """Path to a populated event log — the real thing for anything that
    validates or reads a database rather than just being handed one."""
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    return str(db_path)


@pytest.fixture
def memory_change_db(tmp_path):
    path = tmp_path / "changes.db"
    make_memory_change_db(path)
    return str(path)


@pytest.fixture
def client(memory_db):
    """A client for the Mem0 tab alone, backed by a populated event log."""
    return mem0_app(db=memory_db).test_client()
