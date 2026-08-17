"""Index tests (ADR 11) — the safeguards, incremental extend, projections
and hydration.

The safeguards get the most attention here because they are the whole answer
to the class of bug ADR 2 rejected ETL to avoid. Each is provoked
individually, and each has to survive the case that would defeat the cheaper
check before it: a rewrite that kept the inode, then one that kept the size,
then one that kept both size and head.
"""

import json
import sqlite3

import pytest

from plugins.turns.assembler import assemble
from plugins.turns.atof_index import (HEAD_FINGERPRINT_BYTES,
                                        PAYLOAD_INLINE_MAX_BYTES, AtofIndex,
                                        default_index_path, hydrate_turn)


# --- fixtures -------------------------------------------------------------

def mark(uuid, us, name="m", data=None, metadata=None, parent=None):
    out = {"kind": "mark", "uuid": uuid, "name": name, "timestamp": us}
    if data is not None:
        out["data"] = data
    if metadata is not None:
        out["metadata"] = metadata
    if parent is not None:
        out["parent_uuid"] = parent
    return json.dumps(out)


def scope(uuid, us, end=False, name="read_file", category="tool",
          data=None, metadata=None, profile=None, parent=None):
    out = {"kind": "scope", "scope_category": "end" if end else "start",
           "uuid": uuid, "name": name, "timestamp": us, "category": category}
    if data is not None:
        out["data"] = data
    if metadata is not None:
        out["metadata"] = metadata
    if profile is not None:
        out["category_profile"] = profile
    if parent is not None:
        out["parent_uuid"] = parent
    return json.dumps(out)


# The usage a chat-completions stream reports on its final chunk and
# nowhere else — its span's end payload carries `usage: null`.
CHUNK_USAGE = {"prompt_tokens": 18782, "cache_read_tokens": 1792,
               "completion_tokens": 2248, "total_tokens": 21030}


def write(path, *lines):
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def append(path, *lines):
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


@pytest.fixture
def log(tmp_path):
    return tmp_path / "atof.jsonl"


@pytest.fixture
def index(tmp_path, log):
    return AtofIndex(str(log), str(tmp_path / "index.sqlite3"))


# --- building and extending ----------------------------------------------

def test_builds_from_nothing_then_extends_without_rereading(index, log):
    write(log, mark("a", 1), mark("b", 2))
    state = index.refresh()
    assert state.action == "rebuilt"
    assert [e.uuid for e in index.events()] == ["a", "b"]

    append(log, mark("c", 3))
    state = index.refresh()
    assert state.action == "extended"
    assert state.lines == 1                  # only the appended line was read
    assert [e.uuid for e in index.events()] == ["a", "b", "c"]
    assert [e.line_no for e in index.events()] == [1, 2, 3]


def test_an_unchanged_log_is_a_stat_and_no_work(index, log):
    write(log, mark("a", 1))
    index.refresh()
    state = index.refresh()
    assert state.action == "unchanged"
    assert state.lines == 0
    assert state.indexed_lines == 1


def test_a_partial_trailing_line_is_not_indexed_until_complete(index, log):
    complete = mark("a", 1)
    partial = mark("b", 2)
    log.write_text(complete + "\n" + partial[:12], encoding="utf-8")
    index.refresh()
    assert [e.uuid for e in index.events()] == ["a"]

    log.write_text(complete + "\n" + partial + "\n", encoding="utf-8")
    index.refresh()
    assert [e.uuid for e in index.events()] == ["a", "b"]
    assert [e.line_no for e in index.events()] == [1, 2]


def test_parse_errors_are_kept_not_dropped(index, log):
    write(log, mark("a", 1), "not json at all", mark("b", 3))
    index.refresh()
    assert [e.uuid for e in index.events()] == ["a", "b"]
    errors = index.parse_errors()
    assert [e.line_no for e in errors] == [2]
    assert "not json at all" in errors[0].line_preview


def test_the_index_survives_being_reopened(tmp_path, log):
    """The point of persisting it: a restart must not pay for a rebuild."""
    db = str(tmp_path / "index.sqlite3")
    write(log, mark("a", 1))
    AtofIndex(str(log), db).refresh()

    second = AtofIndex(str(log), db)
    state = second.refresh()
    assert state.action == "unchanged"
    assert [e.uuid for e in second.events()] == ["a"]


# --- the four safeguards --------------------------------------------------

def test_truncation_forces_a_rebuild(index, log):
    write(log, mark("a", 1), mark("b", 2))
    index.refresh()
    write(log, mark("z", 9))                 # shorter than what we indexed
    state = index.refresh()
    assert state.action == "rebuilt"
    assert "shorter" in state.reason
    assert [e.uuid for e in index.events()] == ["z"]


def test_replacing_the_file_forces_a_rebuild(index, log, tmp_path):
    """Rotation: same path, new inode. Same length, so size alone is blind."""
    write(log, mark("a", 1))
    index.refresh()
    replacement = tmp_path / "rotated.jsonl"
    write(replacement, mark("z", 9))
    replacement.replace(log)
    state = index.refresh()
    assert state.action == "rebuilt"
    assert "different file" in state.reason
    assert [e.uuid for e in index.events()] == ["z"]


def test_an_in_place_rewrite_that_kept_the_inode_is_caught_by_the_head(index, log):
    """`>` on an open path keeps the inode, so dev/ino is blind here."""
    write(log, mark("a", 1), mark("b", 2))
    index.refresh()
    before = log.stat()
    with open(log, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(mark("z", 9) + "\n" + mark("y", 8) + "\n")
    assert log.stat().st_ino == before.st_ino
    assert log.stat().st_size == before.st_size          # size is blind too
    state = index.refresh()
    assert state.action == "rebuilt"
    assert "head" in state.reason
    assert [e.uuid for e in index.events()] == ["z", "y"]


def test_a_rewrite_that_kept_size_and_head_is_caught_at_the_seam(index, log):
    """The check the cheaper three cannot make: same file, same inode, same
    size, same first 64 KB — but the bytes under our offsets are not what we
    indexed. The rewrite has to land past the head window, which on the real
    1.19 GB log is everything after the first few hundred lines."""
    lines = [mark(f"e{n:05d}", n) for n in range(HEAD_FINGERPRINT_BYTES // 40)]
    write(log, *lines)
    assert log.stat().st_size > HEAD_FINGERPRINT_BYTES
    index.refresh()
    before = log.stat()

    last = lines[-1]
    rewritten = mark("zzzzzz", len(lines) - 1)
    assert len(rewritten) == len(last)       # keep size and head identical
    with open(log, "r+", encoding="utf-8") as handle:
        handle.seek(log.stat().st_size - len(last) - 1)
        handle.write(rewritten + "\n")
    assert log.stat().st_ino == before.st_ino
    assert log.stat().st_size == before.st_size

    state = index.refresh()
    assert state.action == "rebuilt"
    assert "moved" in state.reason
    assert [e.uuid for e in index.events()][-1] == "zzzzzz"


def test_a_changed_reader_invalidates_the_derived_fields(index, log,
                                                         monkeypatch):
    """The stored spine means whatever the parser meant when it wrote it,
    and that parser keeps changing while hermes' schema does."""
    write(log, mark("a", 1))
    index.refresh()
    monkeypatch.setattr("plugins.turns.atof_index.code_fingerprint",
                        lambda *_: "a-different-reader")
    state = index.refresh()
    assert state.action == "rebuilt"
    assert "reader" in state.reason


def test_the_fingerprint_covers_every_module_a_projection_goes_through():
    """`project()` reads payloads through `spans`, so an edit there has to
    invalidate an index built before it. The list is easy to leave behind
    when code moves between modules — which is exactly how a cache goes
    quietly wrong (ADR 11) — so it is pinned here rather than trusted."""
    import inspect

    from plugins.turns import atof_index

    source = inspect.getsource(atof_index.code_fingerprint)
    for module in ("_atof_reader", "_providers", "_spans", "_assembler"):
        assert module in source, module
    # and the projection really does go through the one that moved
    assert "user_message_from_data" in inspect.getsource(atof_index.project)


def test_a_changed_index_version_invalidates_it(index, log):
    write(log, mark("a", 1))
    index.refresh()
    with sqlite3.connect(index.db_path) as conn:
        conn.execute("UPDATE meta SET value = '999' "
                     "WHERE key = 'index_version'")
    state = index.refresh()
    assert state.action == "rebuilt"
    assert "index version" in state.reason


def test_a_rebuild_replaces_the_stored_shape_not_just_the_rows(index, log):
    """The version dial exists to catch a *column* change, which is the one
    thing `CREATE TABLE IF NOT EXISTS` and `DELETE FROM` between them cannot
    fix: the old table stands and the first insert fails on it. So a rebuild
    drops its tables. Provoked with the real column that found this."""
    write(log, scope("llm1", 100, name="openrouter", category="llm"),
          mark("c1", 150, name="llm.chunk", parent="llm1",
               data={"usage": CHUNK_USAGE}))
    index.refresh()
    with sqlite3.connect(index.db_path) as conn:
        conn.execute("DROP TABLE stream_activity")
        conn.execute("CREATE TABLE stream_activity (uuid TEXT PRIMARY KEY, "
                     "last_us INTEGER NOT NULL, chunks INTEGER NOT NULL)")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'index_version'")

    state = index.refresh()
    assert state.action == "rebuilt"
    assert index.stream_usage()["llm1"]["prompt_tokens"] == 18782


def test_a_vanished_log_leaves_no_stale_rows(index, log):
    write(log, mark("a", 1))
    index.refresh()
    log.unlink()
    index.refresh()
    assert index.events() == []


def test_deleting_the_index_costs_a_rebuild_and_nothing_else(tmp_path, log):
    """It is a cache: the log is the only thing that has to survive."""
    db = tmp_path / "index.sqlite3"
    write(log, mark("a", 1), mark("b", 2))
    first = AtofIndex(str(log), str(db))
    first.refresh()
    expected = [e.uuid for e in first.events()]

    db.unlink()
    second = AtofIndex(str(log), str(db))
    assert second.refresh().action == "rebuilt"
    assert [e.uuid for e in second.events()] == expected


def test_deleting_the_cache_directory_under_a_live_index_is_survivable(
        tmp_path, log):
    """People delete the directory, not the file — the docs tell them it is
    safe — and an app already running had made it once at startup."""
    import shutil

    cache = tmp_path / "cache"
    index = AtofIndex(str(log), str(cache / "index.sqlite3"))
    write(log, mark("a", 1), mark("b", 2))
    index.refresh()

    shutil.rmtree(cache)
    assert index.refresh().action == "rebuilt"
    assert [e.uuid for e in index.events()] == ["a", "b"]


# --- what is stored, and what is left in the log --------------------------

def test_a_small_payload_is_kept_whole(index, log):
    write(log, mark("a", 1, name="hermes.subagent.start",
                    data={"child_session_id": "kid", "child_goal": "do it"}))
    index.refresh()
    event = index.events()[0]
    assert event.payload_elided is False
    assert event.child_session_id == "kid"
    assert event.child_goal == "do it"


def test_a_large_payload_stays_in_the_log(index, log):
    big = "x" * (PAYLOAD_INLINE_MAX_BYTES * 2)
    write(log, scope("s1", 1, data={"path": "/tmp/f", "content": big}))
    index.refresh()
    event = index.events()[0]
    assert event.payload_elided is True
    # the small keys of a large payload are still reachable...
    assert event.data["path"] == "/tmp/f"
    # ...and the large one is not here, but its line is
    assert "content" not in event.data
    assert event.payload_ref.length > PAYLOAD_INLINE_MAX_BYTES


def test_the_prompt_is_projected_out_of_the_turn_mark(index, log):
    """A turn mark averages 570 KB; this short field is all assembly wants
    from it, so it is kept and the payload is not."""
    history = [{"role": "user", "content": "x" * PAYLOAD_INLINE_MAX_BYTES}]
    write(log, mark("t1", 1, name="hermes.turn.start",
                    metadata={"session_id": "s", "turn_id": "t1"},
                    data={"user_message": "please commit",
                          "conversation": history}))
    index.refresh()
    event = index.events()[0]
    assert event.payload_elided is True
    assert event.projected("user_message") == "please commit"

    assembly = assemble(index.events(), index.stream_activity())
    assert assembly.sessions[0].turns[0].user_message == "please commit"


def test_the_reconstructed_prompt_is_projected_out_of_an_llm_request(index, log):
    """The only route to a turn's prompt under the core runtime, and it is
    buried in the largest payload in the log."""
    messages = [{"role": "system", "content": "y" * PAYLOAD_INLINE_MAX_BYTES},
                {"role": "user", "content": "what is the time"}]
    write(log, scope("l1", 1, name="gpt", category="llm",
                     metadata={"session_id": "s"},
                     profile={"model_name": "gpt", "annotated_request":
                              {"messages": messages}}))
    index.refresh()
    event = index.events()[0]
    assert event.payload_elided is True
    assert "annotated_request" not in event.category_profile
    assert event.category_profile["model_name"] == "gpt"   # small key survives
    assert event.projected("request_prompt") == "what is the time"


def test_an_agent_scope_naming_its_session_in_the_payload_is_projected(index, log):
    big = "z" * (PAYLOAD_INLINE_MAX_BYTES * 2)
    write(log, scope("ag", 1, name="hermes.session", category="agent",
                     data={"session_id": "sess-7", "notes": big}))
    index.refresh()
    assert index.events()[0].projected("data_session_id") == "sess-7"


# --- chunks ---------------------------------------------------------------

def test_chunks_become_one_row_per_span(index, log):
    """94% of the log's lines, reaching no template. They say two things —
    that a streaming call is still alive, and what it cost — and both kept."""
    write(log,
          scope("llm1", 100, name="gpt", category="llm",
                metadata={"session_id": "s"}),
          mark("c1", 150, name="llm.chunk", parent="llm1",
               data={"chunk_index": 0}),
          mark("c2", 900, name="llm.chunk", parent="llm1",
               data={"chunk_index": 1}))
    index.refresh()

    assert [e.uuid for e in index.events()] == ["llm1"]     # no chunk rows
    assert index.stream_activity() == {"llm1": 900}

    assembly = assemble(index.events(), index.stream_activity())
    span = assembly.sessions[0].unassigned_spans[0]
    assert span.stream_last_us == 900
    # an open span whose stream is alive is not silent since it started
    assert span.last_activity_us == 900


def test_chunk_activity_accumulates_across_an_extend(index, log):
    write(log, scope("llm1", 100, name="gpt", category="llm"),
          mark("c1", 150, name="llm.chunk", parent="llm1"))
    index.refresh()
    append(log, mark("c2", 800, name="llm.chunk", parent="llm1"))
    index.refresh()
    assert index.stream_activity() == {"llm1": 800}


def test_the_final_chunks_token_counts_are_kept(index, log):
    """The openrouter route reports usage here and nowhere else — its span's
    end payload carries a null `usage`."""
    write(log,
          scope("llm1", 100, name="openrouter", category="llm",
                metadata={"session_id": "s"}),
          scope("llm1", 900, end=True, name="openrouter", category="llm",
                metadata={"session_id": "s"}, data={"usage": None}),
          mark("c1", 150, name="llm.chunk", parent="llm1",
               data={"chunk_index": 0, "usage": None}),
          mark("c2", 800, name="llm.chunk", parent="llm1",
               data={"chunk_index": 1, "usage": CHUNK_USAGE}))
    index.refresh()

    assert index.stream_usage() == {"llm1": {
        "prompt_tokens": 18782, "cache_read_tokens": 1792,
        "output_tokens": 2248, "total_tokens": 21030,
        "input_tokens": 18782 - 1792,
    }}

    assembly = assemble(index.events(), index.stream_activity(),
                        index.stream_usage())
    span = assembly.sessions[0].unassigned_spans[0]
    assert span.usage["prompt_tokens"] == 18782
    assert span.usage_summary == "prompt 18,782 / out 2,248 tokens"


def test_an_extend_that_sees_no_usage_does_not_erase_it(index, log):
    """Usage arrives once, on the last chunk. A later refresh reading only
    the chunks after it must leave the stored counts alone."""
    write(log, scope("llm1", 100, name="openrouter", category="llm"),
          mark("c1", 150, name="llm.chunk", parent="llm1",
               data={"usage": CHUNK_USAGE}))
    index.refresh()
    append(log, mark("c2", 800, name="llm.chunk", parent="llm1",
                     data={"usage": None}))
    index.refresh()
    assert index.stream_usage()["llm1"]["prompt_tokens"] == 18782


def test_spans_whose_stream_said_nothing_are_not_carried(index, log):
    """Only a minority of spans have counts here; the rest cost no row."""
    write(log, scope("llm1", 100, name="gpt", category="llm"),
          mark("c1", 150, name="llm.chunk", parent="llm1",
               data={"chunk_index": 0}))
    index.refresh()
    assert index.stream_usage() == {}
    assert index.stream_activity() == {"llm1": 150}


# --- hydration ------------------------------------------------------------

def test_hydrating_a_turn_reads_its_payloads_back(index, log):
    big = "x" * (PAYLOAD_INLINE_MAX_BYTES * 2)
    write(log,
          mark("m1", 100, name="hermes.turn.start",
               metadata={"session_id": "s", "turn_id": "t1"},
               data={"user_message": "go"}),
          scope("s1", 200, name="read_file", category="tool",
                metadata={"session_id": "s", "turn_id": "t1"},
                data={"path": "/tmp/f"}),
          scope("s1", 300, end=True, name="read_file", category="tool",
                metadata={"session_id": "s", "turn_id": "t1"},
                data={"content": big}),
          mark("m2", 400, name="hermes.turn.end",
               metadata={"session_id": "s", "turn_id": "t1"}))
    index.refresh()
    turn = assemble(index.events(), index.stream_activity()).sessions[0].turns[0]
    span = turn.spans[0]
    assert span.payload_elided is True
    assert "content" not in span.end_data

    hydrate_turn(turn, str(log))
    assert span.payload_elided is False
    assert span.payload_problem is None
    assert span.end_data["content"] == big


def test_a_payload_that_cannot_be_reread_is_reported_not_blank(index, log):
    """The log rotated between assembly and render. ADR 2: say so."""
    big = "x" * (PAYLOAD_INLINE_MAX_BYTES * 2)
    write(log,
          mark("m1", 100, name="hermes.turn.start",
               metadata={"session_id": "s", "turn_id": "t1"},
               data={"user_message": "go"}),
          scope("s1", 200, metadata={"session_id": "s", "turn_id": "t1"},
                data={"path": "/tmp/f", "content": big}),
          scope("s1", 300, end=True,
                metadata={"session_id": "s", "turn_id": "t1"}),
          mark("m2", 400, name="hermes.turn.end",
               metadata={"session_id": "s", "turn_id": "t1"}))
    index.refresh()
    turn = assemble(index.events(), index.stream_activity()).sessions[0].turns[0]

    write(log, mark("gone", 1))              # the log moved under us
    hydrate_turn(turn, str(log))
    span = turn.spans[0]
    assert span.payload_problem is not None
    assert span.payload_elided is True       # still not showing the whole thing
    assert span.start_data["path"] == "/tmp/f"   # what the index kept survives


def test_hydration_is_a_no_op_for_a_payload_the_index_kept(index, log):
    write(log,
          mark("m1", 100, name="hermes.turn.start",
               metadata={"session_id": "s", "turn_id": "t1"},
               data={"user_message": "go"}),
          scope("s1", 200, metadata={"session_id": "s", "turn_id": "t1"},
                data={"path": "/tmp/f"}),
          scope("s1", 300, end=True,
                metadata={"session_id": "s", "turn_id": "t1"}),
          mark("m2", 400, name="hermes.turn.end",
               metadata={"session_id": "s", "turn_id": "t1"}))
    index.refresh()
    turn = assemble(index.events(), index.stream_activity()).sessions[0].turns[0]
    log.unlink()                             # nothing should need to read it
    hydrate_turn(turn, str(log))
    assert turn.spans[0].payload_problem is None
    assert turn.spans[0].start_data["path"] == "/tmp/f"


# --- where it lives -------------------------------------------------------

def test_the_index_is_never_written_beside_the_log(tmp_path, monkeypatch):
    """That directory is hermes'."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    log = tmp_path / "hermes" / "atof.jsonl"
    path = default_index_path(str(log))
    assert not path.startswith(str(tmp_path / "hermes"))
    assert path.startswith(str(tmp_path / "cache"))


def test_two_logs_do_not_share_an_index(tmp_path):
    assert (default_index_path(str(tmp_path / "one.jsonl"))
            != default_index_path(str(tmp_path / "two.jsonl")))


def test_the_same_log_resolves_to_the_same_index(tmp_path):
    assert (default_index_path(str(tmp_path / "a.jsonl"))
            == default_index_path(str(tmp_path / "sub" / ".." / "a.jsonl")))


# --- contributed provider shapes (ADR 13) ---------------------------------

def _acme_shape():
    from providers import COMMON_COUNTS, WHOLE, UsageShape
    return UsageShape(
        name="acme_router", convention=WHOLE,
        counts=(("prompt_tokens", ("acme_prompt_total",)),
                ("cache_read_tokens", ("acme_cached",)),
                ("output_tokens", ("acme_generated",))) + COMMON_COUNTS,
        matches=lambda usage: "acme_prompt_total" in usage)


ACME_END_USAGE = {"acme_prompt_total": 4000, "acme_cached": 3200,
                  "acme_generated": 120}


def test_a_contributed_shape_reaches_the_stored_counts(tmp_path, log):
    """The whole point: a router this tree has never heard of shows its
    tokens without a fork."""
    from providers import USAGE_SHAPES
    write(log,
          scope("llm1", 100, name="acme", category="llm",
                metadata={"session_id": "s"}),
          scope("llm1", 900, end=True, name="acme", category="llm",
                metadata={"session_id": "s"},
                data={"choices": [], "usage": ACME_END_USAGE}))

    plain = AtofIndex(str(log), str(tmp_path / "plain.sqlite3"))
    plain.refresh()
    span = assemble(plain.events()).sessions[0].unassigned_spans[0]
    assert span.token_rows == []            # unrecognised without the shape

    shaped = AtofIndex(str(log), str(tmp_path / "shaped.sqlite3"),
                       (_acme_shape(),) + USAGE_SHAPES)
    shaped.refresh()
    span = assemble(shaped.events()).sessions[0].unassigned_spans[0]
    assert span.usage["prompt_tokens"] == 4000
    assert span.usage["input_tokens"] == 800
    assert span.usage_summary == "prompt 4,000 / out 120 tokens"


def test_changing_the_shape_table_invalidates_the_index(tmp_path, log):
    """A contributed shape decides what the stored counts *mean*, so an
    index built under one table must not be served under another — the
    staleness rule ADR 11 applies to this tree's own code."""
    from providers import USAGE_SHAPES
    write(log, scope("llm1", 100, name="acme", category="llm"),
          scope("llm1", 900, end=True, name="acme", category="llm",
                data={"choices": [], "usage": ACME_END_USAGE}))
    db = str(tmp_path / "index.sqlite3")

    AtofIndex(str(log), db, (_acme_shape(),) + USAGE_SHAPES).refresh()
    state = AtofIndex(str(log), db, USAGE_SHAPES).refresh()
    assert state.action == "rebuilt"
    assert "reader" in state.reason


def test_hydration_reads_a_payload_back_with_the_same_shapes(tmp_path, log):
    """Re-reading an llm end payload re-derives its counts. A hydrated span
    disagreeing with the indexed one would be this app contradicting itself
    between two renders of the same span."""
    from plugins.turns.atof_index import hydrate_turn as _hydrate
    from providers import USAGE_SHAPES
    big = "x" * (PAYLOAD_INLINE_MAX_BYTES * 2)
    shapes = (_acme_shape(),) + USAGE_SHAPES
    write(log,
          mark("m1", 50, name="hermes.turn.start",
               metadata={"session_id": "s", "turn_id": "t1"},
               data={"user_message": "hi"}),
          scope("llm1", 100, name="acme", category="llm",
                metadata={"session_id": "s", "turn_id": "t1"}),
          scope("llm1", 900, end=True, name="acme", category="llm",
                metadata={"session_id": "s", "turn_id": "t1"},
                data={"choices": [], "usage": ACME_END_USAGE, "pad": big}))

    index = AtofIndex(str(log), str(tmp_path / "index.sqlite3"), shapes)
    index.refresh()
    turn = assemble(index.events()).sessions[0].turns[0]
    assert turn.spans[0].payload_elided          # the payload was too big
    _hydrate(turn, str(log), shapes)
    assert turn.spans[0].usage["prompt_tokens"] == 4000
