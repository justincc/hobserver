"""Timing plugin view tests — source states, turn index, waterfall detail."""

from app import create_app
from tests.conftest import make_memory_db
from tests.test_assembler import mark_line, scope_lines, two_turn_stream


def make_client(tmp_path, atof_path):
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    app = create_app(str(db_path), atof_path=atof_path)
    app.config["TESTING"] = True
    return app.test_client()


def write_atof(tmp_path, lines, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


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


def test_empty_file_states_no_events_loudly(tmp_path):
    atof = write_atof(tmp_path, [])
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "no turns yet" in page
    assert "produced no events" in page
    assert "fails open" in page


def test_index_lists_turns_with_waterfall_numbers(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    # complete turn t1: total 5.5s = llm 4s + tool 0.5s + overhead 1s
    assert "5.50 s" in page
    assert "4.00 s" in page
    assert "500 ms" in page
    assert "1.00 s" in page
    # in-flight turn t2 has no total yet
    assert "in flight" in page
    assert "/timing/turn/s1/1000000" in page
    assert "/timing/turn/s1/10000000" in page


def test_index_orders_turns_newest_first(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert page.index('/timing/turn/s1/10000000"') < page.index('/timing/turn/s1/1000000"')


def test_turn_detail_renders_waterfall_bars(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # summary stats
    assert "5.50 s" in page and "overhead" in page
    # spans with identity in text, not color alone
    assert "anthropic" in page and "terminal" in page
    assert "claude-sonnet-4-6" in page
    # bars positioned on the timeline: L1 starts 100ms into a 5.5s turn
    assert "left: 1.82%" in page
    assert "width: 36.36%" in page      # 2s of 5.5s
    # legend present
    assert "cat-llm" in page and "cat-tool" in page
    # usage surfaces in the bar tooltip
    assert "in 100 / out 50 tokens" in page


def test_in_flight_turn_detail_scales_to_last_span(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/10000000").get_data(as_text=True)
    assert "in flight" in page
    assert "anthropic" in page


def test_turn_detail_unknown_turn_404s(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    assert make_client(tmp_path, str(atof)).get("/timing/turn/s1/999").status_code == 404


def test_parse_errors_are_shown_not_dropped(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream() + ["this is not json"])
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "unparseable line" in page
    assert "this is not json" in page
    assert "5.50 s" in page             # good events still render


def test_anomalies_are_shown(tmp_path):
    lines = two_turn_stream() + [
        mark_line("hermes.turn.end", 500_000, session="s2"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "assembly anomal" in page
    assert "turn end without turn start" in page


def test_index_picks_up_appended_turns_between_requests(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    first = client.get("/timing/").get_data(as_text=True)
    assert "/timing/turn/s1/20000000" not in first
    with open(atof, "a", encoding="utf-8") as handle:
        handle.write(mark_line("hermes.turn.start", 20_000_000, session="s1", turn="t3") + "\n")
        for line in scope_lines("L9", "llm", 20_100_000, 21_100_000,
                                session="s1", turn="t3"):
            handle.write(line + "\n")
        handle.write(mark_line("hermes.turn.end", 22_000_000, session="s1", turn="t3") + "\n")
    second = client.get("/timing/").get_data(as_text=True)
    assert "/timing/turn/s1/20000000" in second
