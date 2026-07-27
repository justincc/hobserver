"""Timing plugin view tests — source states, turn index, waterfall detail."""

import os
import re
import time

from markupsafe import escape

from app import create_app
from tests.conftest import make_memory_change_db, make_memory_db
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
        *scope_lines("S3", "tool", 1_650_000, 1_700_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "create", "name": "doc-review",
                                 "category": "productivity"}),
        *scope_lines("S4", "tool", 1_750_000, 1_800_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "write_file", "name": "job-seeker",
                                 "file_path": "references/vacancy-subtext.md"}),
        # the three actions never yet seen in a real log — edit, delete,
        # remove_file — must still render sensibly
        *scope_lines("S5", "tool", 1_810_000, 1_820_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "edit", "name": "editable-skill",
                                 "content": "..."}),
        *scope_lines("S6", "tool", 1_830_000, 1_840_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "delete", "name": "retired-skill",
                                 "absorbed_into": "job-seeker"}),
        *scope_lines("S7", "tool", 1_850_000, 1_860_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "remove_file", "name": "job-seeker",
                                 "file_path": "references/stale.md"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert "github-pr-workflow" in page
    assert "references/hygiene.md" in page
    assert "patch" in page and "job-seeker" in page
    # a "create" carries a category, shown inline in the summary view
    assert "create" in page and "doc-review" in page
    assert "productivity" in page and "skill-cat" in page
    # write_file's file path is separated from the skill name and left-
    # ellipsized (.tail) so the filename end survives truncation
    assert "write_file" in page and "references/vacancy-subtext.md" in page
    assert 'class="path tail"' in page
    # edit shows just the action and skill name
    assert "edit" in page and "editable-skill" in page
    # delete surfaces the skill it was absorbed into
    assert "delete" in page and "retired-skill" in page
    assert "absorbed into" in page
    # remove_file reuses the file-path rendering
    assert "remove_file" in page and "references/stale.md" in page


def test_session_search_discover_query_inline_and_stats_detail_only(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("SS1", "tool", 1_100_000, 1_300_000, name="session_search",
                     session="s1", turn="t1",
                     start_data={"query": "jobs report workflow", "limit": 5},
                     end_data='{"success": true, "mode": "discover",'
                              ' "results": [], "count": 2,'
                              ' "sessions_searched": 3}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the mode is named and the query renders inline in the summary
    assert "mode-tag" in page and ">discover<" in page
    assert "jobs report workflow" in page
    # count and sessions_searched sit on detail-only rows (.list-item)
    assert "count 2" in page and "sessions searched 3" in page
    stats_row = page[page.index("count 2") - 400:page.index("count 2")]
    assert "list-item" in stats_row
    # each label carries a tooltip describing what the number means
    assert "title=\"Session entries actually returned" in page
    assert "not the size of the corpus scanned" in page


def test_session_search_scroll_mode_renders(tmp_path):
    # the mode that was previously invisible: no query, no count
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("SS1", "tool", 1_100_000, 1_300_000, name="session_search",
                     session="s1", turn="t1",
                     start_data={"session_id": "1941bdb78476",
                                 "around_message_id": 14571, "window": 20},
                     end_data='{"success": true, "mode": "scroll",'
                              ' "session_id": "1941bdb78476",'
                              ' "around_message_id": 14571, "window": 20,'
                              ' "messages": [], "messages_before": 7,'
                              ' "messages_after": 4}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert ">scroll<" in page
    assert "session 1941bdb78476 · around msg 14571 · window 20" in page
    # message counts on detail-only rows with explanatory tooltips
    assert "before 7" in page and "after 4" in page
    before_row = page[page.index("before 7") - 300:page.index("before 7")]
    assert "list-item" in before_row
    assert "title=\"Messages in the session before" in page


def test_skill_patch_strings_are_detail_only(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_300_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "patch", "name": "job-search-ops",
                                 "file_path": "workflows/report.md",
                                 "old_string": "version: 1.3.1",
                                 "new_string": "version: 1.4.0"}),
        *scope_lines("S2", "tool", 1_350_000, 1_400_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "patch", "name": "job-seeker",
                                 "old_string": "stale para", "new_string": ""}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the file within the skill stays on the summary line, as for write_file
    assert "workflows/report.md" in page
    # both sides of the patch, each on a detail-only row (.list-item)
    assert "version: 1.3.1" in page and "version: 1.4.0" in page
    old_row = page[page.index("version: 1.3.1") - 300:page.index("version: 1.3.1")]
    assert "list-item" in old_row and "diff-mark del" in old_row
    # an empty new_string is a deletion, said in words rather than left blank
    assert "deletes the matched text" in page


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
    assert "mode-tag" in page            # …under a tag naming the mode
    # the V4A text itself, on a detail-only row
    text_row = page[page.index("*** Begin Patch") - 300:page.index("*** Begin Patch")]
    assert "list-item" in text_row


def test_patch_replace_strings_are_detail_only(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_300_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "replace", "path": "/home/u/notes.md",
                                 "old_string": "## Recommended action",
                                 "new_string": "## Recorded decision"}),
        *scope_lines("P2", "tool", 1_350_000, 1_400_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "replace", "path": "/home/u/b.md",
                                 "old_string": "stale para", "new_string": ""}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the path keeps the summary line; the mode names which shape follows
    assert "/home/u/notes.md" in page and ">replace<" in page
    # both sides of the edit, each on a detail-only row (.list-item)
    assert "## Recommended action" in page and "## Recorded decision" in page
    old_row = page[page.index("## Recommended action") - 300:page.index("## Recommended action")]
    assert "list-item" in old_row and "diff-mark del" in old_row
    # an empty new_string is a deletion, said in words rather than left blank
    assert "deletes the matched text" in page


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
    # both search queries wrap out in full in the detailed layout
    assert '<code class="wrap-detail" title="user timezone preference">' in page
    assert '<code class="wrap-detail" title="flask blueprint url_prefix">' in page


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
    # the full fact wraps out in the detailed layout
    assert '<span class="path wide wrap-detail" title="User prefers tea over coffee.">' in page


def test_mem0_update_and_delete_show_the_memory_id(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, 1_300_000, name="mem0_update",
                     session="s1", turn="t1",
                     start_data={"memory_id": "fdd806c1-0789-4522-aaf3",
                                 "text": "User prefers coffee after all."}),
        *scope_lines("M2", "tool", 1_350_000, 1_400_000, name="mem0_delete",
                     session="s1", turn="t1",
                     start_data={"memory_id": "b3e3ade6-d852-44b2-98f4"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the replacement text leads the summary line, wrapping out in detail
    text = '<span class="path wide wrap-detail" title="User prefers coffee after all.">'
    assert text in page
    assert "list-item" not in page[page.index(text) - 200:page.index(text)]
    # the id is faint and copyable, and detail-only (.list-item) so it never
    # shares the summary line with the span's own uuid
    mem_id = '<span class="mem-id">fdd806c1-0789-4522-aaf3'
    assert mem_id in page and 'data-copy="fdd806c1-0789-4522-aaf3"' in page
    assert "list-item" in page[page.index(mem_id) - 200:page.index(mem_id)]
    # a delete has nothing but the id
    delete_id = '<span class="mem-id">b3e3ade6-d852-44b2-98f4'
    assert delete_id in page
    assert "list-item" in page[page.index(delete_id) - 200:page.index(delete_id)]


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


def test_vision_analyze_image_inline_question_detail_only(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("V1", "tool", 1_100_000, 1_600_000, name="vision_analyze",
                     session="s1", turn="t1",
                     start_data={"image_url": "/tmp/cv-page-1.png",
                                 "question": "List spelling errors only."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the image path shows in the summary line, left-ellipsized
    assert "/tmp/cv-page-1.png" in page
    assert 'class="path tail"' in page
    # the question is present but only on its own detail-mode line
    assert "List spelling errors only." in page
    assert page.count('class="span-detail list-item"') == 1


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
    # … and on its own list-item line for detail mode
    assert page.count('class="span-detail list-item"') == 3


def test_todo_first_item_inline_and_full_list_for_detail_mode(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("D1", "tool", 1_100_000, 1_600_000, name="todo",
                     session="s1", turn="t1",
                     start_data={"todos": [
                         {"id": "a", "content": "Inventory the report",
                          "status": "in_progress"},
                         {"id": "b", "content": "Run discovery",
                          "status": "pending"},
                         {"id": "c", "content": "Write it up",
                          "status": "pending"},
                     ]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # inline mode: first item plus a count of the rest
    assert "Inventory the report" in page
    assert "+2 more" in page
    # detail mode: every item on its own list-item line
    assert page.count('class="span-detail list-item"') == 3
    assert "Run discovery" in page
    assert "Write it up" in page


def test_delegate_task_and_subagent_goals_shown(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("D1", "tool", 1_100_000, 1_200_000, name="delegate_task",
                     session="s1", turn="t1",
                     start_data={"tasks": [
                         {"goal": "Sweep Luma listings",
                          "context": "Window is 20-26 Jul"},
                         {"goal": "Sweep Meetup listings",
                          "context": "Prefers technical events"},
                     ]}),
        mark_line("hermes.subagent.start", 1_300_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Luma listings",
                        "child_session_id": "cs1"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # delegate_task: first goal plus a count inline …
    assert "+1 more" in page
    # … and each task's goal with its context nested under it in detail,
    # the pairs spaced apart via the task-goal class
    assert page.count('class="span-detail list-item task-goal"') == 2
    assert page.count('class="span-detail list-item ctx"') == 2
    assert "Window is 20-26 Jul" in page
    assert "Prefers technical events" in page
    # the subagent start mark row shows its child goal
    row = re.search(
        r'<tr data-span-uuid="mark-hermes\.subagent\.start-1300000">.*?</tr>',
        page, re.S).group(0)
    assert "Sweep Luma listings" in row


def test_subagent_stop_shows_ordinal_status_and_echoed_goal(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.subagent.start", 1_100_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Luma listings",
                        "child_session_id": "c1"}),
        mark_line("hermes.subagent.stop", 1_800_000, session="s1", turn="t1",
                  data={"child_session_id": "c1", "child_status": "timeout",
                        "duration_ms": 600050}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the pair tag rides both the start and the stop row, with an
    # inline-only middot ahead of the goal/status
    assert page.count('class="subagent-ord"') == 2
    assert page.count('<span class="inline-only">·</span>') == 2
    stop_row = re.search(
        r'<tr data-span-uuid="mark-hermes\.subagent\.stop-1800000">.*?</tr>',
        page, re.S).group(0)
    # the stop row shows the status, echoes its start's goal, and keeps
    # the child session id and duration on a detail-only line
    assert "timeout" in stop_row
    assert "Sweep Luma listings" in stop_row
    assert "c1 · 600.05 s" in stop_row


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
    assert "body:not(.show-detail) tr:not(.detail-open) .span-detail { display: inline;" in page
    # … pinned to a single line by ellipsizing the name cell …
    assert "body:not(.show-detail) table.waterfall tr:not(.detail-open) td:nth-child(2) { white-space: nowrap;" in page
    # … and only the span uuid is hidden
    assert "body:not(.show-detail) tr:not(.detail-open) .span-uuid { display: none; }" in page


def test_rows_carry_uuids_for_click_to_expand(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 1_600_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.approval.request", 1_700_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # every span and mark row is click-to-expand, keyed by its uuid …
    assert '<tr data-span-uuid="L1">' in page
    assert '<tr data-span-uuid="mark-hermes.approval.request-1700000">' in page
    # … via the per-row class the page script toggles on click
    assert 'row.classList.toggle("detail-open")' in page


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


def test_turn_detail_shows_top_mem0_results_and_links_to_memory(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1",
                     start_data={"query": "job preferences", "top_k": 10},
                     end_data='{"count": 4, "results":'
                     ' [{"id": "b760576d", "memory": "top fact", "score": 0.8042},'
                     '  {"id": "f9c1f7ee", "memory": "next fact", "score": 0.5339},'
                     '  {"id": "fb54073a", "memory": "third fact", "score": 0.4229},'
                     '  {"id": "c78490d3", "memory": "fourth fact", "score": 0.3115}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/timing/turn/s1/1000000").get_data(as_text=True)
    assert "top fact" in page and "next fact" in page and "third fact" in page
    assert "fourth fact" not in page         # the preview stops at three
    assert "0.80" in page and "0.53" in page and "0.42" in page
    assert "b760576d" in page                # each hit's id, for the lookup
    # the handoff to the memory tab carries what identifies the logged call
    assert "/memory/search-event?" in page
    assert "session=s1" in page
    assert "query=job+preferences" in page
    assert "ts=1100000" in page
    assert "all 4 results" in page


def test_turn_detail_mem0_link_reads_full_result_when_nothing_is_hidden(tmp_path):
    # with three or fewer hits the preview is the whole list, so promising
    # "all 3 results" behind the link would be promising nothing new
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"},
                     end_data='{"count": 2, "results":'
                     ' [{"id": "a", "memory": "one", "score": 0.9},'
                     '  {"id": "b", "memory": "two", "score": 0.8}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/timing/turn/s1/1000000").get_data(as_text=True)
    # the link's own text, not its tooltip — the tooltip still describes the
    # target page as carrying all the results, which it does
    link_text = page[page.index("search-event"):]
    link_text = link_text[link_text.index('">') + 2:link_text.index("</a>")]
    assert link_text.strip() == "full result in Memory &rarr;"


def test_turn_detail_mem0_link_absent_while_the_search_is_open(tmp_path):
    # nothing came back yet, so there is nothing to link to
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"}),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/timing/turn/s1/1000000").get_data(as_text=True)
    assert "/memory/search-event" not in page


def test_turn_detail_web_search_gets_no_mem0_result_rows(tmp_path):
    # web_search shares the query branch but has no mem0 results behind it
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="web_search",
                     session="s1", turn="t1",
                     start_data={"query": "flask blueprints"},
                     end_data='{"count": 1, "results":'
                     ' [{"id": "x", "memory": "not a memory", "score": 0.9}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/timing/turn/s1/1000000").get_data(as_text=True)
    assert "flask blueprints" in page
    assert "not a memory" not in page
    assert "/memory/search-event" not in page


def _change_client(tmp_path, atof_path):
    """A client whose event log carries the search → change pattern, so the
    turn page can recover what a mem0 memory said before a span changed it."""
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    app = create_app(str(db_path), atof_path=atof_path)
    app.config["TESTING"] = True
    return app.test_client()


def _change_stream(name, memory_id, start_data):
    # the fixture log's first search is at epoch 2000 s; the span sits 30 s
    # later, so the search precedes the change exactly as in the real log
    return [
        mark_line("hermes.turn.start", 2_020_000_000, session="s9", turn="t1"),
        *scope_lines("M1", "tool", 2_030_000_000, 2_031_000_000, name=name,
                     session="s9", turn="t1", start_data=start_data),
        mark_line("hermes.turn.end", 2_050_000_000, session="s9", turn="t1"),
    ]


def test_turn_detail_shows_what_an_update_replaced(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/timing/turn/s9/2020000000").get_data(as_text=True)
    assert "the old fact" in page             # recovered from the event log
    assert "the new fact" in page             # the span's own payload
    assert '<span class="diff-mark del">' in page      # both diff sides
    assert '<span class="diff-mark ins">' in page
    assert "previous text from the local log" in page
    assert "/memory/event/1" in page          # the search it came from
    assert "30 s earlier" in page


def test_turn_detail_recovers_a_deleted_memory_with_no_new_side(tmp_path):
    # a delete's payload is only an id, so without the recovered text the row
    # says nothing about what was lost — and there is no "+" side to show
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/timing/turn/s9/2020000000").get_data(as_text=True)
    assert "the doomed fact" in page
    assert "previous text from the local log" in page
    assert '<span class="diff-mark ins">' not in page


def test_turn_detail_previous_text_names_the_local_log_not_mem0(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/timing/turn/s9/2020000000").get_data(as_text=True)
    assert "Not retrieved from mem0" in page
    assert "mem0 is never queried" in page


def test_turn_detail_without_a_matching_memory_shows_no_previous_text(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "unknown-id", {"memory_id": "unknown-id"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/timing/turn/s9/2020000000").get_data(as_text=True)
    assert "previous text from the local log" not in page
    assert "unknown-id" in page               # the id itself still shows


def test_turn_detail_renders_without_the_memory_plugin_lookup(tmp_path):
    # ADR 4: the timing tab does without when the lookup is not published,
    # rather than reaching into the event log itself
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    app = create_app(str(db_path), atof_path=str(atof))
    app.config["TESTING"] = True
    app.extensions.pop("memory_prior_text")
    page = app.test_client().get(
        "/timing/turn/s9/2020000000").get_data(as_text=True)
    assert page.count("mem0_update") >= 1     # the span still renders
    assert "the new fact" in page
    assert "the old fact" not in page
    assert "previous text from the local log" not in page


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


def subagent_stream():
    """A parent turn that spawned two subagents and got a stop for one.
    Both children left their own turn open, as hermes subagents do — only
    the parent's stop mark distinguishes the finished one. All stamps are
    wall-clock-recent so the staleness clock cannot be what excludes it."""
    now = int(time.time() * 1_000_000)
    parent_start = now - 30_000_000
    running_start, stopped_start = now - 25_000_000, now - 20_000_000
    return running_start, stopped_start, [
        *session_scope_lines("p1", start_us=now - 60_000_000),
        mark_line("hermes.turn.start", parent_start, session="p1", turn="pt1",
                  data={"user_message": SHORT_PROMPT, "platform": "webui"}),
        mark_line("hermes.subagent.start", parent_start + 1_000_000,
                  session="p1", turn="pt1",
                  data={"child_goal": "Sweep Luma", "child_session_id": "kid-running"}),
        mark_line("hermes.subagent.start", parent_start + 2_000_000,
                  session="p1", turn="pt1",
                  data={"child_goal": "Sweep Meetup", "child_session_id": "kid-stopped"}),
        mark_line("hermes.turn.start", running_start, session="kid-running", turn="ct1"),
        mark_line("hermes.turn.start", stopped_start, session="kid-stopped", turn="ct2"),
        mark_line("hermes.subagent.stop", now - 10_000_000, session="p1", turn="pt1",
                  data={"child_session_id": "kid-stopped", "child_status": "ok"}),
    ]


def test_inflight_strip_drops_stopped_subagents(tmp_path):
    running_start, stopped_start, lines = subagent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    # the subagent the parent reported stopped is gone from the strip
    assert f'data-inflight-start-us="{stopped_start}"' not in page
    # its still-running sibling, and the parent, stay
    assert f'data-inflight-start-us="{running_start}"' in page
    assert "in flight:" in page


def test_stopped_subagent_turn_still_listed_and_reachable(tmp_path):
    # dropping it from the strip is a liveness call, not a retention one:
    # the turn table and its waterfall page must still have it
    _, stopped_start, lines = subagent_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    assert f"/timing/turn/kid-stopped/{stopped_start}" in \
        client.get("/timing/").get_data(as_text=True)
    assert client.get(f"/timing/turn/kid-stopped/{stopped_start}").status_code == 200


def superseded_stream():
    """Two turns in one session, the first never closed — the exact shape
    that froze follow mode: the older turn looked in flight forever, so its
    page kept claiming to be watching live work."""
    now = int(time.time() * 1_000_000)
    first_start, second_start = now - 120_000_000, now - 30_000_000
    return first_start, second_start, [
        *session_scope_lines("s7", start_us=now - 180_000_000),
        mark_line("hermes.turn.start", first_start, session="s7", turn="t1",
                  data={"user_message": SHORT_PROMPT, "platform": "webui"}),
        *scope_lines("L1", "llm", first_start + 100_000, first_start + 2_000_000,
                     name="anthropic", session="s7", turn="t1"),
        # no hermes.turn.end for t1
        mark_line("hermes.turn.start", second_start, session="s7", turn="t2",
                  data={"user_message": LONG_PROMPT, "platform": "webui"}),
        *scope_lines("L2", "llm", second_start + 100_000, None,
                     name="anthropic", session="s7", turn="t2"),
    ]


def test_superseded_turn_leaves_the_inflight_strip(tmp_path):
    first_start, second_start, lines = superseded_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert f'data-inflight-start-us="{first_start}"' not in page
    assert f'data-inflight-start-us="{second_start}"' in page


def test_superseded_turn_page_does_not_block_follow(tmp_path):
    # the regression: viewing the older turn must not mark it as the current
    # in-flight turn, or followNewTurn() returns early and never advances
    first_start, second_start, lines = superseded_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    page = client.get(f"/timing/turn/s7/{first_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' not in page
    # and it stops polling as though it were live
    assert 'data-live-poll="0"' in page
    # the genuinely live turn still marks itself current and polls fast
    live = client.get(f"/timing/turn/s7/{second_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' in live
    assert 'data-live-poll="2000"' in live


def test_superseded_turn_is_listed_without_claiming_to_be_in_flight(tmp_path):
    first_start, _, lines = superseded_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    index = client.get("/timing/").get_data(as_text=True)
    # still in the table and reachable — this is a liveness call, not retention
    assert f"/timing/turn/s7/{first_start}" in index
    assert client.get(f"/timing/turn/s7/{first_start}").status_code == 200
    # but shown as ended-without-an-end-mark, not as running
    assert "no end mark" in index


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


def test_turn_page_links_to_neighbouring_turns(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    older = client.get("/timing/turn/s1/1000000").get_data(as_text=True)
    # oldest turn: nothing before it, the later turn is "next"
    assert '<span class="disabled" title="no older turn">&laquo; prev</span>' in older
    assert '/timing/turn/s1/10000000"' in older
    newer = client.get("/timing/turn/s1/10000000").get_data(as_text=True)
    assert '/timing/turn/s1/1000000"' in newer
    assert '<span class="disabled" title="no newer turn">next &raquo;</span>' in newer


def test_neighbour_prompt_snippet_reaches_the_step_link_title(tmp_path):
    # the prompt snippet is passed into the shared item_nav macro as a title
    finished_start, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/timing/turn/s9/{inflight_start}").get_data(as_text=True)
    assert f'title="previous (older) turn — {SHORT_PROMPT}"' in page


def test_follow_toggle_rides_the_turn_nav_row(tmp_path):
    # same line as all/prev/next: it is navigation too, just automatic
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get(
        "/timing/turn/s1/1000000").get_data(as_text=True)
    start = page.index('<nav class="event-nav">')
    nav = page[start:page.index("</nav>", start)]
    assert "data-follow-toggle" in nav
    assert "all turns" in nav and "prev" in nav and "next" in nav
    # last in the row, so space-between puts it on the right
    assert nav.index("data-follow-toggle") > nav.index("next &raquo;")


def test_index_keeps_its_standalone_follow_toggle(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/timing/").get_data(as_text=True)
    assert "data-follow-toggle" in page
    # the index has no turn nav to ride, so it stands alone above the region
    assert page.index("data-follow-toggle") < page.index('data-live-poll="3000"')


def test_detail_mode_carries_the_whole_command_and_code(tmp_path):
    """Terminal commands and execute_code snippets are multi-line often
    enough that the one-line inline form loses the point of the span; the
    detail layout has to carry the whole thing, line breaks included."""
    script = "git add -A\ngit commit -m 'wip'\ngit push"
    code = "from pathlib import Path\np = Path('/tmp')\nprint(p.exists())"
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"command": script, "workdir": "/home/u/proj"}),
        *scope_lines("E1", "tool", 1_700_000, 1_900_000, name="execute_code",
                     session="s1", turn="t1", start_data={"code": code}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    # the command wraps out in full, in one code element that keeps newlines
    esc_script, esc_code = escape(script), escape(code)
    assert f'<code class="wrap-detail" title="{esc_script}">{esc_script}</code>' in page
    # execute_code keeps the first-line summary for the inline layout and
    # adds a detail-only element holding every line
    assert '<div class="span-detail list-compact">' in page
    assert f'<code class="wrap-detail" title="{esc_code}">{esc_code}</code>' in page


def test_memory_scope_shows_action_store_and_entry(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "replace", "target": "user",
                                 "old_text": "User does not want auto-commits",
                                 "content": "User requires a staged diff "
                                            "before commits."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="mode-tag">replace</span>' in page
    assert "· user" in page                      # which of the two stores
    assert "User requires a staged diff before commits." in page
    # one op needs no per-op label: the mode tag above already names it and
    # stays visible in detail mode
    assert '<span class="skill-action">replace</span>' not in page
    # both sides of the replaced entry, detail-only, through the shared macro
    assert '<span class="diff-mark del">&minus;</span>' in page
    assert 'title="User does not want auto-commits"' in page


def test_memory_add_shows_only_the_added_side(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "add", "target": "memory",
                                 "content": "Port 5090 is the observer."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="mode-tag">add</span>' in page
    assert '<span class="diff-mark ins">+</span>' in page
    # an add replaces nothing, so there is no − row to show
    assert '<span class="diff-mark del">&minus;</span>' not in page


def test_memory_batch_lists_every_operation(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"target": "user", "operations": [
                         {"action": "replace", "old_text": "Former HCA",
                          "content": "Former HCA Ingest architect at EBI."},
                         {"action": "remove", "old_text": "Atlas weekly"},
                     ]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="mode-tag">batch</span>' in page
    # first entry stands in for the batch inline, the rest counted
    assert '<div class="span-detail list-compact">' in page
    assert "+1 more" in page
    # every op gets its own detail row, named
    assert '<span class="skill-action">replace</span>' in page
    assert '<span class="skill-action">remove</span>' in page
    assert 'title="Atlas weekly"' in page


def test_failed_span_is_badged_and_shows_the_tools_error(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("F1", "tool", 1_100_000, 1_600_000, name="skill_view",
                     session="s1", turn="t1", end_status="error",
                     start_data={"name": "events-hunter"},
                     end_data={"success": False,
                               "error": "[Errno 24] Too many open files"}),
        *scope_lines("OK", "tool", 1_700_000, 1_800_000, name="read_file",
                     session="s1", turn="t1", end_status="ok",
                     start_data={"path": "/home/u/a.md"},
                     end_data={"content": "hi"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="badge-error"' in page
    # the message is visible without expanding the row — a failure should
    # never need a click to notice
    assert '<div class="span-detail err">' in page
    assert "[Errno 24] Too many open files" in page
    assert page.count('<span class="badge-error"') == 1     # not the ok span


def test_memory_rejection_shows_the_budget_and_the_store(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="memory",
                     session="s1", turn="t1", end_status="error",
                     start_data={"action": "add", "target": "user",
                                 "content": "One more thing."},
                     end_data={"success": False, "usage": "1,338/1,375",
                               "error": "Memory at 1,338/1,375 chars. Adding "
                                        "this entry would exceed the limit.",
                               "current_entries": ["Entry one.", "Entry two."]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="badge-error"' in page
    assert "Adding this entry would exceed the limit." in page
    # budget and the store it has to fit in — detail-only, like the
    # session_search counts
    assert re.search(r'class="span-detail list-item">\s*<span class="mode-tag"'
                     r'[^>]*>usage 1,338/1,375<', page)
    assert "store now holds 2" in page
    assert "Entry one." in page and "Entry two." in page


def test_memory_success_reports_usage_without_the_store(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_600_000, name="memory",
                     session="s1", turn="t1", end_status="ok",
                     start_data={"action": "add", "target": "memory",
                                 "content": "Port 5090 is the observer."},
                     end_data={"success": True, "entry_count": 10,
                               "usage": "99% — 2,198/2,200 chars",
                               "message": "Entry added."}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/timing/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="badge-error"' not in page
    assert "usage 99% — 2,198/2,200 chars" in page   # the tool's own wording
    assert "entries 10" in page
    assert "store now holds" not in page
