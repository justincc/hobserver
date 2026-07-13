"""Timing plugin tests — stub states until the ATOF reader lands (ADR 2)."""

from app import create_app
from tests.conftest import make_memory_db


def make_client(tmp_path, atof_path):
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    app = create_app(str(db_path), atof_path=atof_path)
    app.config["TESTING"] = True
    return app.test_client()


def test_unconfigured_source_is_stated_loudly(tmp_path):
    page = make_client(tmp_path, None).get("/timing/").get_data(as_text=True)
    assert "No ATOF source configured" in page
    assert "ATOF_LOG" in page


def test_missing_file_is_stated_loudly(tmp_path):
    missing = tmp_path / "missing.jsonl"
    page = make_client(tmp_path, str(missing)).get("/timing/").get_data(as_text=True)
    assert "ATOF log not found" in page
    assert str(missing) in page
    # the fail-open caveat from ADR 1/2 must be surfaced to the user
    assert "fails open" in page


def test_existing_file_shows_source_and_stub_notice(tmp_path):
    atof = tmp_path / "events.jsonl"
    atof.write_text("", encoding="utf-8")
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert str(atof) in page
    assert "not implemented yet" in page
