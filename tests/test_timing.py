"""Timing plugin view tests — source states, turn index, waterfall detail."""

import os
import time

from app import create_app
from tests.conftest import make_memory_db
from tests.test_assembler import (mark_line, scope_lines, session_scope_lines,
                                  two_turn_stream)


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


SHORT_PROMPT = "what is the weather in cambridge?"
LONG_PROMPT = ("investigate these logging traces: "
               + "ERROR timeout in relay worker\n" * 40 + "ZZZ-END")


def recent_stream():
    """A finished turn and an in-flight turn with wall-clock-recent stamps,
    so in-flight entries are genuinely fresh (the shared fixtures use
    1970-era epochs, which read as stale against the staleness cutoff).
    The finished turn has a short prompt, the in-flight one a long one."""
    now = int(time.time() * 1_000_000)
    finished_start, inflight_start = now - 60_000_000, now - 5_000_000
    return finished_start, inflight_start, [
        *session_scope_lines("s9", start_us=now - 120_000_000),
        mark_line("hermes.turn.start", finished_start, session="s9", turn="t8",
                  data={"user_message": SHORT_PROMPT, "platform": "webui"}),
        *scope_lines("L8", "llm", finished_start + 100_000, finished_start + 2_000_000,
                     name="anthropic", session="s9", turn="t8"),
        mark_line("hermes.turn.end", finished_start + 3_000_000, session="s9", turn="t8"),
        mark_line("hermes.turn.start", inflight_start, session="s9", turn="t9",
                  data={"user_message": LONG_PROMPT, "platform": "webui"}),
        *scope_lines("L9", "llm", inflight_start + 100_000, None,
                     name="anthropic", session="s9", turn="t9"),
    ]


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
    # terminal command and workdir shown inline, no disclosure needed
    assert "git status --short" in page
    assert "in /home/u/proj" in page
    # the span uuid is the only id on the page — the raw-log lookup key,
    # muted, right after the span name, with a copy button
    assert '<span class="span-uuid">· T1<button class="copy-btn" data-copy="T1"' in page
    assert "call-1" not in page


def test_workdir_under_home_collapses_to_tilde(tmp_path):
    home_proj = os.path.join(os.path.expanduser("~"), "proj")
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"command": "ls", "workdir": home_proj}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "in ~/proj" in page
    # the full path survives in the title attribute for copy/hover
    assert f'title="{home_proj}"' in page


def test_skill_scope_details_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_300_000, name="skill_view",
                     session="s1", turn="t1",
                     start_data={"name": "github-pr-workflow",
                                 "file_path": "references/hygiene.md"}),
        *scope_lines("S2", "tool", 1_400_000, 1_600_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "patch", "name": "job-seeker"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "github-pr-workflow" in page
    assert "references/hygiene.md" in page
    assert "patch" in page and "job-seeker" in page


def test_file_tool_path_shown_inline_tail_first(tmp_path):
    home_notes = os.path.join(os.path.expanduser("~"), "docs", "notes.md")
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_600_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "replace", "path": home_notes,
                                 "old_string": "a", "new_string": "b"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "~/docs/notes.md" in page
    assert f'title="{home_notes}"' in page
    # left-ellipsized so the end of the path survives truncation
    assert 'class="path tail"' in page


def test_patch_mode_paths_shown_inline(tmp_path):
    patch_text = ("*** Begin Patch\n"
                  "*** Update File: /home/u/proj/a.md\n@@\n-old\n+new\n"
                  "*** Update File: /home/u/proj/b.md\n@@\n-x\n+y\n"
                  "*** End Patch")
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_600_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "patch", "patch": patch_text}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "/home/u/proj/a.md" in page   # first path shown inline
    assert "+1 more" in page             # remaining files as a count
    assert "/home/u/proj/b.md" in page   # all paths in the hover title


def test_search_files_pattern_and_glob_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_600_000, name="search_files",
                     session="s1", turn="t1",
                     start_data={"pattern": "turn_id", "target": "content",
                                 "path": "/home/u/proj", "file_glob": "*.py"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "turn_id" in page
    assert "*.py" in page
    assert "/home/u/proj" in page


def test_search_queries_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="web_search",
                     session="s1", turn="t1",
                     start_data={"query": "flask blueprint url_prefix", "limit": 5}),
        *scope_lines("M1", "tool", 1_650_000, 1_700_000, name="mem0_search",
                     session="s1", turn="t1",
                     start_data={"query": "user timezone preference",
                                 "rerank": True, "top_k": 10}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "flask blueprint url_prefix" in page
    assert "user timezone preference" in page


def test_mem0_add_content_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, 1_600_000, name="mem0_add",
                     session="s1", turn="t1",
                     start_data={"content": "User prefers tea over coffee."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "User prefers tea over coffee." in page


def test_execute_code_first_line_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("E1", "tool", 1_100_000, 1_600_000, name="execute_code",
                     session="s1", turn="t1",
                     start_data={"code": "from pathlib import Path\np = Path('/tmp')"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "from pathlib import Path" in page


def test_web_extract_first_url_and_count_shown_inline(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="web_extract",
                     session="s1", turn="t1",
                     start_data={"char_limit": 30000,
                                 "urls": ["https://a.example/one",
                                          "https://b.example/two",
                                          "https://c.example/three"]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "https://a.example/one" in page
    assert "+2 more" in page
    # every url is available on hover via the title attribute
    assert "https://c.example/three" in page


def test_turn_page_has_details_switch(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # unchecked on every load — visibility state is per page load, not persisted
    assert "data-detail-toggle" in page
    assert "data-detail-toggle checked" not in page


def test_span_extra_info_stays_inline_when_details_off(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # with the switch off, extra info joins the name line instead of hiding …
    assert "body:not(.show-detail) .span-detail { display: inline;" in page
    # … pinned to a single line by ellipsizing the name cell …
    assert "body:not(.show-detail) table.waterfall td:nth-child(2) { white-space: nowrap;" in page
    # … and only the span uuid is hidden
    assert "body:not(.show-detail) .span-uuid { display: none; }" in page


def test_turn_id_has_copy_button(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert 'class="copy-btn" data-copy="t1"' in page


def test_span_and_mark_uuids_have_copy_buttons(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 1_600_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.approval.request", 1_700_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert 'class="copy-btn" data-copy="L1"' in page
    assert 'class="copy-btn" data-copy="mark-hermes.approval.request-1700000"' in page


def test_marks_render_as_ticks_in_the_waterfall(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.approval.request", 1_500_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
        # session.end fires just after the turn ends but carries its turn_id
        mark_line("hermes.session.end", 2_100_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # marks are waterfall rows with a tick, not a separate table
    assert "<h2>Marks</h2>" not in page
    assert "hermes.approval.request" in page
    assert "hermes.session.end" in page
    # approval at 500ms into a 1s turn; session.end clamps to the track end
    assert 'class="tick" style="left: 50.00%;"' in page
    assert 'class="tick" style="left: 100.00%;"' in page


def test_in_flight_turn_detail_scales_to_last_span(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/10000000").get_data(as_text=True)
    assert "in flight" in page
    assert "anthropic" in page


def test_turn_detail_renders_with_string_tool_result_data(tmp_path):
    # regression: real tool end events carry data as a raw JSON string,
    # which 500ed the turn page when the template assumed a dict
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1",
                     end_data='{"results": [{"memory": "a fact"}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    resp = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "mem0_search" in page
    assert "500 ms" in page


def test_turn_detail_unknown_turn_404s(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    assert make_client(tmp_path, str(atof)).get("/timing/turn/s1/999").status_code == 404


def test_parse_errors_are_shown_not_dropped(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream() + ["this is not json"])
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "unparseable line" in page
    assert "this is not json" in page
    assert "5.50 s" in page             # good events still render
    # folded closed by default — details opt-in via the summary click
    assert 'class="problems" open' not in page


def test_problems_only_on_turn_list_page(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream() + ["this is not json"])
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "unparseable line" not in page


def test_anomalies_are_shown(tmp_path):
    lines = two_turn_stream() + [
        mark_line("hermes.turn.end", 500_000, session="s2"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "assembly anomal" in page
    assert "turn end without turn start" in page


def test_index_is_a_live_region(tmp_path):
    # the index always polls, so new turns appear without a manual reload
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert 'data-live-poll="3000"' in page


def test_no_source_states_are_live_too(tmp_path):
    # a missing file page must come alive once the exporter starts writing
    missing = tmp_path / "missing.jsonl"
    page = make_client(tmp_path, str(missing)).get("/timing/").get_data(as_text=True)
    assert 'data-live-poll="3000"' in page


def test_turn_detail_polls_only_while_in_flight(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    in_flight = client.get("/timing/turn/s1/10000000").get_data(as_text=True)
    assert 'data-live-poll="2000"' in in_flight
    finished = client.get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert 'data-live-poll="0"' in finished


def test_inflight_strip_lists_running_turns(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "in flight:" in page
    assert f'data-inflight-start-us="{inflight_start}"' in page
    assert f"/timing/turn/s9/{inflight_start}" in page
    assert 'data-stale="1"' not in page  # a fresh turn is never marked stale


def test_inflight_strip_marks_stale_turns(tmp_path):
    # two_turn_stream's in-flight turn has 1970-era stamps: silent far past
    # the cutoff, so it is listed but flagged and excluded from auto-follow
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "in flight:" in page
    assert 'data-stale="1"' in page
    assert "stale — last event" in page


def test_turn_page_marks_its_own_inflight_entry(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/timing/turn/s9/{inflight_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' in page
    assert "(viewing)" in page
    assert f'data-turn-start-us="{inflight_start}"' in page


def test_finished_turn_page_lists_inflight_without_current(tmp_path):
    finished_start, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/timing/turn/s9/{finished_start}").get_data(as_text=True)
    assert f'data-inflight-start-us="{inflight_start}"' in page
    assert 'data-inflight-current="1"' not in page


def test_follow_toggle_rendered_on_both_pages(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    assert "data-follow-toggle" in client.get("/timing/").get_data(as_text=True)
    assert "data-follow-toggle" in client.get(
        f"/timing/turn/s9/{inflight_start}").get_data(as_text=True)


def test_index_shows_prompt_snippet_single_line(tmp_path):
    _, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert SHORT_PROMPT in page                      # short prompts fit whole
    assert "investigate these logging traces" in page
    assert "ZZZ-END" not in page                     # long ones are truncated


def test_index_shows_placeholder_when_start_mark_has_no_prompt(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "prompt-cell" in page and "—" in page


def test_turn_page_long_prompt_collapses_but_keeps_full_text(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/timing/turn/s9/{inflight_start}").get_data(as_text=True)
    assert '<details class="prompt">' in page
    assert "investigate these logging traces" in page   # summary snippet
    assert "ZZZ-END" in page                             # full text expandable


def test_turn_page_short_prompt_shown_plain(tmp_path):
    finished_start, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/timing/turn/s9/{finished_start}").get_data(as_text=True)
    assert SHORT_PROMPT in page
    assert '<details class="prompt">' not in page


def test_inflight_strip_shows_prompt_snippet(tmp_path):
    _, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "“investigate these logging" in page


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
