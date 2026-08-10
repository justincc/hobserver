"""Turns tab view tests — source states, turn index, waterfall detail."""

import os
import re
import time

from markupsafe import escape

from conftest import REPO_ROOT, make_app
from conftest import make_memory_change_db, make_memory_db
from test_assembler import (mark_line, scope_lines, session_scope_lines,
                                  two_turn_stream)


def make_client(tmp_path, atof_path):
    db_path = tmp_path / "test.db"
    make_memory_db(db_path)
    return make_app(db=str(db_path), atof=atof_path).test_client()


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


def test_missing_file_is_stated_loudly(tmp_path):
    # there is no "unconfigured" state: a path is always resolved, so an
    # absent log is always a named path that does not exist
    missing = tmp_path / "missing.jsonl"
    page = make_client(tmp_path, str(missing)).get("/turns/").get_data(as_text=True)
    assert "ATOF log not found" in page
    assert str(missing) in page
    # the fail-open caveat from ADR 1/2 must be surfaced to the user
    assert "fails open" in page
    # and how to point it somewhere else
    assert "observer.toml" in page and "ATOF_LOG" in page


def test_empty_file_states_no_events_loudly(tmp_path):
    atof = write_atof(tmp_path, [])
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "no turns yet" in page
    assert "produced no events" in page
    assert "fails open" in page


def test_index_lists_turns_with_waterfall_numbers(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    # complete turn t1: total 5.5s = llm 4s + tool 0.5s + overhead 1s
    assert "5.50 s" in page
    assert "4.00 s" in page
    assert "500 ms" in page
    assert "1.00 s" in page
    # in-flight turn t2 has no total yet
    assert "in flight" in page
    assert "/turns/turn/s1/1000000" in page
    assert "/turns/turn/s1/10000000" in page


def test_index_orders_turns_newest_first(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert page.index('/turns/turn/s1/10000000"') < page.index('/turns/turn/s1/1000000"')


def test_turn_detail_renders_waterfall_bars(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    # the tree's top-level buckets surface in the bar tooltip; the leaves
    # under them are detail-only and stay on the page
    assert "prompt 120 / out 50 tokens" in page
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    # unchecked on every load — visibility state is per page load, not persisted
    assert "data-detail-toggle" in page
    assert "data-detail-toggle checked" not in page
    # and it says what it does to the page, not what it is: a lone `details`
    # beside three colour chips read as a fourth legend entry
    assert re.search(r'<label class="switch detail-toggle".*?show all span details\s*</label>',
                     page, re.S)


def test_span_extra_info_stays_inline_when_details_off(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    # every span and mark row is click-to-expand, keyed by its uuid …
    assert '<tr data-span-uuid="L1">' in page
    assert '<tr data-span-uuid="mark-hermes.approval.request-1700000">' in page
    # … via the per-row class the page script toggles on click
    assert 'row.classList.toggle("detail-open")' in page


def test_turn_id_has_copy_button(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    # marks are waterfall rows with a tick, not a separate table
    assert "<h2>Marks</h2>" not in page
    assert "hermes.approval.request" in page
    assert "hermes.session.end" in page
    # approval at 500ms into a 1s turn; session.end clamps to the track end
    assert 'class="tick" style="left: 50.00%;"' in page
    assert 'class="tick" style="left: 100.00%;"' in page


def test_in_flight_turn_detail_scales_to_last_span(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/10000000").get_data(as_text=True)
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
    resp = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000")
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
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "top fact" in page and "next fact" in page and "third fact" in page
    assert "fourth fact" not in page         # the preview stops at three
    assert "0.80" in page and "0.53" in page and "0.42" in page
    assert "b760576d" in page                # each hit's id, for the lookup
    # the handoff to the Mem0 tab carries what identifies the logged call
    assert "/memory/mem0/search-event?" in page
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
        "/turns/turn/s1/1000000").get_data(as_text=True)
    # the link's own text, not its tooltip — the tooltip still describes the
    # target page as carrying all the results, which it does
    link_text = page[page.index("search-event"):]
    link_text = link_text[link_text.index('">') + 2:link_text.index("</a>")]
    # a literal arrow now, not the &rarr; entity the macro used: the spec
    # table writes literals (· − → ) and Jinja leaves them alone
    assert link_text.strip() == "full result in Mem0 →"


def test_turn_detail_mem0_link_absent_while_the_search_is_open(tmp_path):
    # nothing came back yet, so there is nothing to link to
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"}),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "/memory/mem0/search-event" not in page


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
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "flask blueprints" in page
    assert "not a memory" not in page
    assert "/memory/mem0/search-event" not in page


def _change_client(tmp_path, atof_path):
    """A client whose event log carries the search → change pattern, so the
    turn page can recover what a mem0 memory said before a span changed it."""
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    return make_app(db=str(db_path), atof=atof_path).test_client()


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
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "the old fact" in page             # recovered from the event log
    assert "the new fact" in page             # the span's own payload
    assert '<span class="diff-mark del">' in page      # both diff sides
    assert '<span class="diff-mark ins">' in page
    assert "previous text from the local log" in page
    assert "/memory/mem0/event/1" in page          # the search it came from
    assert "30 s earlier" in page


def test_turn_detail_recovers_a_deleted_memory_with_no_new_side(tmp_path):
    # a delete's payload is only an id, so without the recovered text the row
    # says nothing about what was lost — and there is no "+" side to show
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "the doomed fact" in page
    assert "previous text from the local log" in page
    assert '<span class="diff-mark ins">' not in page


def test_delete_summary_line_leads_with_the_deleted_memory(tmp_path):
    # a delete's payload is only an id, so with details off the row would say
    # nothing about what was lost; the recovered text leads instead, the way
    # a mem0_add's own content does
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_delete"):page.index('class="mem-id"')]
    assert "the doomed fact" in summary
    # .list-compact, so detail mode drops it rather than repeating the text
    # the − row below already carries in full
    assert 'class="span-detail list-compact"' in summary


def test_delete_summary_line_absent_when_nothing_was_recovered(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "unknown-id", {"memory_id": "unknown-id"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_delete"):page.index('class="mem-id"')]
    assert "list-compact" not in summary


def test_update_summary_line_still_leads_with_its_own_new_text(tmp_path):
    # an update carries a fact of its own, so the recovered text must not
    # displace it — the old text belongs on the detail-only − row
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    summary = page[page.index("mem0_update"):page.index('class="mem-id"')]
    assert "the new fact" in summary
    assert "the old fact" not in summary


def test_turn_detail_previous_text_names_the_local_log_not_mem0(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "bbb22222", {"memory_id": "bbb22222"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "Not retrieved from mem0" in page
    assert "mem0 is never queried" in page


def test_turn_detail_without_a_matching_memory_shows_no_previous_text(tmp_path):
    atof = write_atof(tmp_path, _change_stream(
        "mem0_delete", "unknown-id", {"memory_id": "unknown-id"}))
    page = _change_client(tmp_path, str(atof)).get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert "previous text from the local log" not in page
    assert "unknown-id" in page               # the id itself still shows


def test_turn_detail_renders_without_the_memory_plugin_lookup(tmp_path):
    # ADR 4: the Turns tab does without when the lookup is not published,
    # rather than reaching into the event log itself
    db_path = tmp_path / "changes.db"
    make_memory_change_db(db_path)
    atof = write_atof(tmp_path, _change_stream(
        "mem0_update", "aaa11111",
        {"memory_id": "aaa11111", "text": "the new fact"}))
    app = make_app(db=str(db_path), atof=str(atof))
    app.config["TESTING"] = True
    app.extensions.pop("mem0_prior_text")
    page = app.test_client().get(
        "/turns/turn/s9/2020000000").get_data(as_text=True)
    assert page.count("mem0_update") >= 1     # the span still renders
    assert "the new fact" in page
    assert "the old fact" not in page
    assert "previous text from the local log" not in page


def test_turn_detail_unknown_turn_404s(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    assert make_client(tmp_path, str(atof)).get("/turns/turn/s1/999").status_code == 404


def test_parse_errors_are_shown_not_dropped(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream() + ["this is not json"])
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "unparseable line" in page
    assert "this is not json" in page
    assert "5.50 s" in page             # good events still render
    # folded closed by default — details opt-in via the summary click
    assert 'class="problems" open' not in page


def test_problems_only_on_turn_list_page(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream() + ["this is not json"])
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert "unparseable line" not in page


def test_anomalies_are_shown(tmp_path):
    lines = two_turn_stream() + [
        mark_line("hermes.turn.end", 500_000, session="s2"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "assembly anomal" in page
    assert "turn end without turn start" in page


def test_index_is_a_live_region(tmp_path):
    # the index always polls, so new turns appear without a manual reload
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert 'data-live-poll="3000"' in page


def test_no_source_states_are_live_too(tmp_path):
    # a missing file page must come alive once the exporter starts writing
    missing = tmp_path / "missing.jsonl"
    page = make_client(tmp_path, str(missing)).get("/turns/").get_data(as_text=True)
    assert 'data-live-poll="3000"' in page


def test_turn_detail_polls_only_while_in_flight(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    in_flight = client.get("/turns/turn/s1/10000000").get_data(as_text=True)
    assert 'data-live-poll="2000"' in in_flight
    finished = client.get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert 'data-live-poll="0"' in finished


def test_inflight_strip_lists_running_turns(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "in flight:" in page
    assert f'data-inflight-start-us="{inflight_start}"' in page
    assert f"/turns/turn/s9/{inflight_start}" in page
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
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
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
    assert f"/turns/turn/kid-stopped/{stopped_start}" in \
        client.get("/turns/").get_data(as_text=True)
    assert client.get(f"/turns/turn/kid-stopped/{stopped_start}").status_code == 200


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
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert f'data-inflight-start-us="{first_start}"' not in page
    assert f'data-inflight-start-us="{second_start}"' in page


def test_superseded_turn_page_does_not_block_follow(tmp_path):
    # the regression: viewing the older turn must not mark it as the current
    # in-flight turn, or followNewTurn() returns early and never advances
    first_start, second_start, lines = superseded_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    page = client.get(f"/turns/turn/s7/{first_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' not in page
    # and it stops polling as though it were live
    assert 'data-live-poll="0"' in page
    # the genuinely live turn still marks itself current and polls fast
    live = client.get(f"/turns/turn/s7/{second_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' in live
    assert 'data-live-poll="2000"' in live


def test_superseded_turn_is_listed_without_claiming_to_be_in_flight(tmp_path):
    first_start, _, lines = superseded_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    index = client.get("/turns/").get_data(as_text=True)
    # still in the table and reachable — this is a liveness call, not retention
    assert f"/turns/turn/s7/{first_start}" in index
    assert client.get(f"/turns/turn/s7/{first_start}").status_code == 200
    # but shown as ended-without-an-end-mark, not as running
    assert "no end mark" in index


def test_inflight_strip_marks_stale_turns(tmp_path):
    # two_turn_stream's in-flight turn has 1970-era stamps: silent far past
    # the cutoff, so it is listed but flagged and excluded from auto-follow
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "in flight:" in page
    assert 'data-stale="1"' in page
    assert "stale — last event" in page


def test_turn_page_marks_its_own_inflight_entry(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{inflight_start}").get_data(as_text=True)
    assert 'data-inflight-current="1"' in page
    assert "(viewing)" in page
    assert f'data-turn-start-us="{inflight_start}"' in page


def test_finished_turn_page_lists_inflight_without_current(tmp_path):
    finished_start, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{finished_start}").get_data(as_text=True)
    assert f'data-inflight-start-us="{inflight_start}"' in page
    assert 'data-inflight-current="1"' not in page


def test_follow_toggle_rendered_on_both_pages(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    client = make_client(tmp_path, str(atof))
    assert "data-follow-toggle" in client.get("/turns/").get_data(as_text=True)
    assert "data-follow-toggle" in client.get(
        f"/turns/turn/s9/{inflight_start}").get_data(as_text=True)


def test_both_switches_are_the_same_switch(tmp_path):
    """Follow mode and the span details do the same kind of thing — a
    persistent on/off for the page — so they are one control, drawn once by
    `.switch`. The track has to sit immediately after the input: it is a
    sibling selector that paints the checkbox's state, and a stray element
    between them leaves a switch that never moves."""
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{inflight_start}").get_data(as_text=True)
    for attr in ("data-follow-toggle", "data-detail-toggle"):
        assert re.search(r'<label class="switch [\w-]+"[^>]*>\s*'
                         r'<input type="checkbox" ' + attr + r'>\s*'
                         r'<span class="track"></span>', page), attr
    # …and the look is stated once, not once per switch
    css = (REPO_ROOT / "templates" / "base.html").read_text()
    assert not re.search(r"\.(follow|detail)-toggle[^{,]*\{[^}]*\b(border-radius|opacity)", css)


def test_index_shows_prompt_snippet_single_line(tmp_path):
    _, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert SHORT_PROMPT in page                      # short prompts fit whole
    assert "investigate these logging traces" in page
    assert "ZZZ-END" not in page                     # long ones are truncated


def test_index_shows_placeholder_when_start_mark_has_no_prompt(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "prompt-cell" in page and "—" in page


def test_turn_page_long_prompt_collapses_but_keeps_full_text(tmp_path):
    _, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{inflight_start}").get_data(as_text=True)
    assert '<details class="prompt">' in page
    assert "investigate these logging traces" in page   # summary snippet
    assert "ZZZ-END" in page                             # full text expandable


def test_turn_page_short_prompt_shown_plain(tmp_path):
    finished_start, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{finished_start}").get_data(as_text=True)
    assert SHORT_PROMPT in page
    assert '<details class="prompt">' not in page


def test_inflight_strip_shows_prompt_snippet(tmp_path):
    _, _, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
    assert "“investigate these logging" in page


def test_index_picks_up_appended_turns_between_requests(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    first = client.get("/turns/").get_data(as_text=True)
    assert "/turns/turn/s1/20000000" not in first
    with open(atof, "a", encoding="utf-8") as handle:
        handle.write(mark_line("hermes.turn.start", 20_000_000, session="s1", turn="t3") + "\n")
        for line in scope_lines("L9", "llm", 20_100_000, 21_100_000,
                                session="s1", turn="t3"):
            handle.write(line + "\n")
        handle.write(mark_line("hermes.turn.end", 22_000_000, session="s1", turn="t3") + "\n")
    second = client.get("/turns/").get_data(as_text=True)
    assert "/turns/turn/s1/20000000" in second


def test_turn_page_links_to_neighbouring_turns(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    older = client.get("/turns/turn/s1/1000000").get_data(as_text=True)
    # oldest turn: nothing before it, the later turn is "next"
    assert '<span class="disabled" title="no older turn">&laquo; prev</span>' in older
    assert '/turns/turn/s1/10000000"' in older
    newer = client.get("/turns/turn/s1/10000000").get_data(as_text=True)
    assert '/turns/turn/s1/1000000"' in newer
    assert '<span class="disabled" title="no newer turn">next &raquo;</span>' in newer


def test_neighbour_prompt_snippet_reaches_the_step_link_title(tmp_path):
    # the prompt snippet is passed into the shared item_nav macro as a title
    finished_start, inflight_start, lines = recent_stream()
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        f"/turns/turn/s9/{inflight_start}").get_data(as_text=True)
    assert f'title="previous (older) turn — {SHORT_PROMPT}"' in page


def test_follow_toggle_rides_the_turn_nav_row(tmp_path):
    # same line as all/prev/next: it is navigation too, just automatic
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    start = page.index('<nav class="event-nav">')
    nav = page[start:page.index("</nav>", start)]
    assert "data-follow-toggle" in nav
    assert "all turns" in nav and "prev" in nav and "next" in nav
    # last in the row, so space-between puts it on the right
    assert nav.index("data-follow-toggle") > nav.index("next &raquo;")


def test_index_keeps_its_standalone_follow_toggle(tmp_path):
    atof = write_atof(tmp_path, two_turn_stream())
    page = make_client(tmp_path, str(atof)).get("/turns/").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
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
    page = make_client(tmp_path, str(atof)).get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="badge-error"' not in page
    assert "usage 99% — 2,198/2,200 chars" in page   # the tool's own wording
    assert "entries 10" in page
    assert "store now holds" not in page


# --- unrecognised scopes ------------------------------------------------
# hermes' tool set is not this app's to know. A scope with no branch of its
# own used to render a name and a duration and nothing else, which is what
# anyone running hermes with other tools would see for most of their spans.


def _unknown_scope(tmp_path, start_data, name="widget_tool"):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("U1", "tool", 1_100_000, 1_600_000, name=name,
                     session="s1", turn="t1", start_data=start_data),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    return make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)


def test_unknown_scope_shows_its_payload(tmp_path):
    page = _unknown_scope(tmp_path, {"action": "wait", "timeout": 300,
                                     "note": "third key"})
    for key in ("action", "timeout", "note"):
        assert f'<span class="gen-key">{key}</span>' in page
    assert "wait" in page and "300" in page and "third key" in page


def test_unknown_scope_summary_line_holds_the_first_three_scalars(tmp_path):
    # the summary line is one line: everything else waits for detail mode
    page = _unknown_scope(tmp_path, {"a": "1", "b": "2", "c": "3", "d": "4"})
    compact = page[page.index('class="span-detail list-compact"'):]
    compact = compact[:compact.index("</div>")]
    assert compact.count('class="gen-key"') == 5      # 3 keys + 2 separators
    assert ">d<" not in compact
    # but d is there in a detail-only row
    assert re.search(r'class="span-detail list-item">\s*<span class="gen-key">d<', page)


def test_unknown_scope_hides_routing_and_secret_keys(tmp_path):
    page = _unknown_scope(tmp_path, {
        "action": "go", "session_id": "s1", "telemetry_schema_version": "1",
        "tool_call_id": "call-9", "headers": {"authorization": "Bearer hunter2"},
        "api_token": "hunter2"})
    assert "go" in page
    for hidden in ("Bearer hunter2", "hunter2", "call-9",
                   "telemetry_schema_version"):
        assert hidden not in page


def test_unknown_scope_names_oversize_values_instead_of_printing_them(tmp_path):
    # the log holds a 7 MB conversation_history, and a live turn page
    # refetches itself every 2 s
    page = _unknown_scope(tmp_path, {"blob": "x" * 5000,
                                     "rows": [{"n": i} for i in range(400)]})
    assert "x" * 200 not in page
    assert "text, 5 KB" in page
    assert re.search(r"list, 400 items \([\d.]+ KB\)", page)


def test_unknown_scope_renders_small_collections(tmp_path):
    page = _unknown_scope(tmp_path, {"tags": ["alpha", "beta"]})
    assert "alpha" in page and "beta" in page


def test_known_scopes_keep_their_own_rendering(tmp_path):
    # the fallback is the else of the branch chain: a scope with a branch
    # must not also collect generic rows
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"command": "ls -la", "workdir": "/tmp"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "ls -la" in page
    assert 'class="gen-key"' not in page


def test_unknown_mark_shows_its_payload(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.session.end", 1_500_000, session="s1", turn="t1",
                  data={"completed": True, "interrupted": False,
                        "platform": "webui"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert '<span class="gen-key">completed</span>' in page
    assert "True" in page


# --- llm scopes ---------------------------------------------------------
# What the model decided, what it said and what it cost — all in the end
# payload, and none of it rendered before.


def _llm_turn(tmp_path, end_data):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 1_600_000, name="openai-codex",
                     session="s1", turn="t1", start_data={"headers": {}},
                     end_data=end_data),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    return make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)


TOKEN_ROW = (r'<div class="span-detail ([^"]*)">\s*'
             r'(?:<span class="gen-key inline-only">·</span>\s*)?'
             r'<span class="mode-tag"[^>]*>([a-z ]+)</span>\s*'
             r'<span class="row-value"[^>]*>([\d,]+)</span>')


def _token_rows(page):
    """The rendered token tree as [(label, css classes)], in page order."""
    return [(label, klass.strip())
            for klass, label, _ in re.findall(TOKEN_ROW, page)]


def _token_figures(page):
    """The tree as `label figure` strings. The key and the figure are two
    spans — faint monospace and the row's reading font — so a plain
    `"in 2,465" in page` no longer sees a row and would pass on markup that
    never rendered."""
    return [f"{label} {value}" for _, label, value in re.findall(TOKEN_ROW, page)]


def _assistant(content="", *tool_names):
    return {"assistant_message": {
        "role": "assistant", "content": content,
        "tool_calls": [{"name": n, "id": f"call{i}", "arguments": "{}"}
                       for i, n in enumerate(tool_names)]}}


FIN_ROW = (r'<div class="span-detail">\s*'
           r'<span class="gen-key key-col list-item"[^>]*>finish_reason</span>\s*'
           r'<span class="row-value"[^>]*>([a-z_]+)</span>\s*</div>')


def test_llm_span_shows_the_finish_reason(tmp_path):
    page = _llm_turn(tmp_path, {**_assistant("done"), "finish_reason": "stop"})
    assert re.search(FIN_ROW, page).group(1) == "stop"


def test_llm_finish_reason_carries_its_key_in_the_key_column(tmp_path):
    """`complete` alone is a bare word in a row of bare words, and the reader
    who goes to the log for it meets `status: "completed"` on the same event —
    a different field — and concludes the page reworded it. The key is what
    says which field this is, and it is the log's own key, not `status`:
    hermes writes `finish_reason` on every route.

    `key-col` is what aligns it with `prompt`, `response` and `tool_calls` —
    the four are the payload's own values and are read down as a group."""
    page = _llm_turn(tmp_path, {**_assistant("done"), "finish_reason": "complete"})
    assert re.search(FIN_ROW, page).group(1) == "complete"


def test_llm_finish_reason_key_is_detail_only(tmp_path):
    """The key carries .list-item, not the row: the row has to survive the
    collapse for the value to. On a summary line the value stands among token
    figures with nothing to confuse it with, and the label would be 14 more
    characters for the cell's ellipsis to eat."""
    page = _llm_turn(tmp_path, {**_assistant("done"), "finish_reason": "complete"})
    row = re.search(FIN_ROW, page)
    assert "list-item" not in row.group(0)[:len('<div class="span-detail">')]
    key = re.search(r'<span class="([^"]*)"[^>]*>finish_reason</span>', page)
    assert "list-item" in key.group(1)


def test_llm_finish_reason_value_is_not_a_metadata_chip(tmp_path):
    """The value is a payload value like the three keyed rows under it, so it
    reads in their font — not the faint monospace `.mode-tag` that call_role
    and retry use. With key and value side by side, the font is what tells
    them apart."""
    page = _llm_turn(tmp_path, {**_assistant("done"), "finish_reason": "complete"})
    assert not re.search(r'<span class="mode-tag"[^>]*>(finish_reason )?complete<',
                         page)


def test_llm_span_names_its_tool_calls_without_their_arguments(tmp_path):
    # the names are worth restating — they were one decision — but the
    # arguments are not: each span below renders its own, through a branch
    # written for that tool
    page = _llm_turn(tmp_path, {"finish_reason": "tool_calls",
                                "assistant_message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"name": "mem0_search", "id": "c1",
                        "arguments": '{"query": "brain development"}'}]}})
    rows = page[page.index("<tbody>"):]
    assert "mem0_search" in rows
    assert "brain development" not in rows     # the argument stays below
    assert re.search(FIN_ROW, rows).group(1) == "tool_calls"  # how it ended


def test_llm_span_lists_the_tools_it_asked_for(tmp_path):
    # names only, in the order asked — the arguments are on the spans below.
    # What this row adds is that the calls were one decision.
    page = _llm_turn(tmp_path, {
        **_assistant("", "mem0_search", "read_file", "read_file"),
        "finish_reason": "tool_calls"})
    calls = re.search(r'<span class="call-list[^"]*">(.*?)</span>\s*</div>',
                      page, re.S)
    assert calls
    assert re.sub(r"<[^>]+>", "", calls.group(1)).split(" · ") == [
        "mem0_search", "read_file", "read_file"]     # repeats kept, order kept


def test_llm_span_names_nothing_when_the_model_only_talked(tmp_path):
    page = _llm_turn(tmp_path, {**_assistant("Just a reply."),
                                "finish_reason": "stop"})
    assert '<span class="call-list' not in page   # no names beside the reason
    assert re.search(FIN_ROW, page).group(1) == "stop"   # the reason still shows


def test_llm_span_counts_a_tool_call_it_cannot_read(tmp_path):
    # the count is the point of the row, so a malformed entry keeps its slot
    # rather than being dropped into a shorter, tidier, wrong list
    page = _llm_turn(tmp_path, {"finish_reason": "tool_calls",
                                "assistant_message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"name": "terminal", "id": "c1", "arguments": "{}"},
                       "not a dict",
                       {"id": "c3", "arguments": "{}"}]}})
    calls = re.search(r'<span class="call-list[^"]*">(.*?)</span>\s*</div>',
                      page, re.S)
    assert re.sub(r"<[^>]+>", "", calls.group(1)).split(" · ") == [
        "terminal", "(unreadable)", "(unnamed)"]


def test_llm_tool_calls_take_their_own_labelled_row(tmp_path):
    """Not riding the finish reason unlabelled. That worked while the reason
    *was* the word `tool_calls`; the core runtime reports `complete`, so on
    958 of the log's tool-calling spans the row read `complete  read_file`
    and nothing said what the names were."""
    page = _llm_turn(tmp_path, {**_assistant("", "terminal", "read_file"),
                                "finish_reason": "complete"})
    row = re.search(r'<div class="span-detail list-item">\s*'
                    r'<span class="gen-key key-col"[^>]*>tool_calls</span>\s*'
                    r'<span class="call-list[^"]*">(.*?)</span>\s*</div>', page, re.S)
    assert row, "no labelled tool_calls row"
    assert "terminal" in row.group(1) and "read_file" in row.group(1)
    # the reason keeps its own row, under its own key, in the same column
    assert re.search(FIN_ROW, page).group(1) == "complete"


def test_llm_tool_call_names_stay_off_the_summary_line(tmp_path):
    # detail-only, and it is the row that carries .list-item — a hidden row
    # hides its children whatever class they have, which is why the names
    # span no longer has to avoid .path to stay off the summary line
    page = _llm_turn(tmp_path, {**_assistant("", "terminal", "read_file"),
                                "finish_reason": "complete"})
    row = re.search(r'<div class="([^"]*)">\s*<span class="gen-key key-col"'
                    r'[^>]*>tool_calls</span>', page)
    assert row, "the tool_calls row is missing"
    assert "list-item" in row.group(1).split()


def test_llm_span_shows_the_start_of_what_the_model_said(tmp_path):
    text = "The answer is 42. " * 40         # 720 chars
    page = _llm_turn(tmp_path, {**_assistant(text), "finish_reason": "stop"})
    assert "The answer is 42." in page
    assert text not in page                  # only the start of it
    assert "&hellip;" in page or "…" in page  # …and an ellipsis saying so


def test_llm_span_shows_short_text_whole_without_an_ellipsis(tmp_path):
    page = _llm_turn(tmp_path, {**_assistant("Short reply."),
                                "finish_reason": "stop"})
    assert "Short reply." in page
    assert "Short reply.…" not in page
    assert "start of" not in page            # no size note anywhere any more


def test_llm_span_reports_tokens_including_cache_reads(tmp_path):
    # a cache read can be most of a prompt, and was only in a tooltip
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 2465, "prompt_tokens": 20897, "output_tokens": 719,
        "cache_read_tokens": 18432, "cache_write_tokens": 0,
        "reasoning_tokens": 124, "total_tokens": 21616, "request_count": 1}})
    figures = _token_figures(page)
    assert "in 2,465" in figures
    assert "prompt 20,897" in figures         # differs from in, so shown
    assert "cache read 18,432" in figures
    assert "reasoning 124" in figures
    assert "requests 1" in figures            # no longer eaten by an ellipsis
    assert "cache write" not in page          # zero says nothing


def test_llm_token_figures_read_as_values_not_as_tags(tmp_path):
    """One rule down the whole llm span: the key is faint monospace, the
    value reads in the row's own font. The figures take the same `.row-value`
    as the finish reason above them, so a reader is not asked to learn two
    conventions between one row and the next."""
    page = _llm_turn(tmp_path, {**_assistant("hi"), "finish_reason": "stop",
                                "usage": {"prompt_tokens": 19349,
                                          "output_tokens": 1089,
                                          "request_count": 1}})
    # every figure is a value; no `label 1,089` chip survives anywhere
    assert not re.search(r'<span class="mode-tag"[^>]*>[a-z ]+ [\d,]+<', page)
    assert {"prompt 19,349", "out 1,089"} <= set(_token_figures(page))
    # …and the key beside it is still a tag, not a second value
    assert re.search(r'<span class="mode-tag"[^>]*>prompt</span>', page)


def test_llm_token_figures_keep_the_row_shade_they_are_on(tmp_path):
    """`.row-value` sets no colour of its own, so it takes whatever shade its
    row is in rather than knowing about any particular row."""
    css = (REPO_ROOT / "templates" / "base.html").read_text()
    # the cache share shares this block rather than restating it, so match
    # the selector list and not `.row-value` alone
    rule = re.search(r"\.span-detail \.row-value,\s*"
                     r"\.span-detail \.tok-share \{([^}]*)\}", css)
    assert rule, "no .row-value rule, or the share no longer reads with it"
    assert "color" not in rule.group(1)
    assert "font-family" not in rule.group(1)  # the row's own, not a chip's
    assert "tabular-nums" in rule.group(1)     # figures compared down a column


def test_every_token_figure_is_one_colour(tmp_path):
    """`.tok-part` used to shade its row `.mode-tag`'s own #8a8a8a, which
    left `reasoning 303` the one pair on the span whose key and value
    matched — the row read as the only one the change to values had missed.
    Parts read like every other figure now; the class stays as a marker, and
    what it means is left to the tooltip that was always carrying it."""
    css = (REPO_ROOT / "templates" / "base.html").read_text()
    assert not re.search(r"\.tok-part[^{,]*\{[^}]*color:", css), \
        "a .tok-part rule still shades its row"
    # …and the marker still reaches the markup, so the rows stay findable
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "prompt_tokens": 19349, "output_tokens": 1089,
        "reasoning_tokens": 303, "request_count": 1}})
    assert ("reasoning", "list-item tok-d2 tok-part") in _token_rows(page)


def test_llm_span_nests_token_counts_under_the_sums_they_make(tmp_path):
    # in + cache read == prompt, prompt + out == total: the indent is that
    # arithmetic, so a reader can see which figures add up and which don't
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 1429, "prompt_tokens": 19349, "output_tokens": 1089,
        "cache_read_tokens": 17920, "cache_write_tokens": 0,
        "reasoning_tokens": 303, "total_tokens": 20438, "request_count": 1}})
    depths = _token_rows(page)
    # cache read before in: how much the provider already had is the question
    # these rows answer, and `in` then reads as the remainder that was new
    assert [label for label, _ in depths] == [
        "prompt", "cache read", "in", "out", "reasoning", "requests"]
    assert ("prompt", "tok-d1") in depths            # a top-level bucket…
    assert ("out", "tok-sep tok-d1") in depths       # …with a `·` before it
    assert ("cache read", "list-item tok-d2") in depths   # …and its parts
    assert ("in", "list-item tok-d2") in depths
    # reasoning is counted within out, not alongside it: deeper, and marked
    assert ("reasoning", "list-item tok-d2 tok-part") in depths
    # …and says so, including that the remainder is not simply the reply:
    # on a tool-calling turn most of it is the arguments, rendered below
    assert "counted within out rather than added to it" in page
    assert "arguments of any tool calls" in page


def test_llm_span_reports_how_much_of_the_prompt_was_cached(tmp_path):
    # on the summary line this is the only place the cache shows at all —
    # cache read is detail-only. 17,920/20,193 is 88.74%, so 89 rounding up
    page = _llm_turn(tmp_path, {**_assistant("hi"), "finish_reason": "stop",
                                "usage": {
        "input_tokens": 2273, "prompt_tokens": 20193, "output_tokens": 84,
        "cache_read_tokens": 17920, "request_count": 1}})
    assert '<span class="tok-share">(89% cached)</span>' in page
    # …and it rides the prompt row, which survives the collapse. Whitespace
    # before it, exactly as between the key and the figure — both collapse to
    # one rendered space. That space is what sets both gaps on the summary
    # line, which is inline and where the detail layout's flex gap does not
    # apply; here it is ignored, whitespace-only text between flex items not
    # being rendered, so `gap` sets both. One mechanism either way, so the
    # two gaps cannot drift apart.
    assert re.search(r'<div class="span-detail tok-sep tok-d1">\s*'
                     r'<span class="gen-key inline-only">·</span>\s*'
                     r'<span class="mode-tag"[^>]*>prompt</span>'
                     r'\s+<span class="row-value"[^>]*>20,193</span>'
                     r'\s+<span class="tok-share">\(89% cached\)</span>', page)


def test_llm_span_never_claims_a_wholly_cached_prompt_it_did_not_have(tmp_path):
    # 12,900/12,901 is 99.99%, which would round to a flat 100% while a token
    # was still fresh — 2.4% of the calls in the log do this
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 1, "prompt_tokens": 12901, "output_tokens": 40,
        "cache_read_tokens": 12900, "request_count": 1}})
    assert '<span class="tok-share">(99% cached)</span>' in page
    assert "100% cached)" not in page


def test_llm_span_reports_a_wholly_cached_prompt_as_all_of_it(tmp_path):
    # the cap is for rounding, not a refusal to ever say 100: nothing fresh
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 0, "prompt_tokens": 12900, "output_tokens": 40,
        "cache_read_tokens": 12900, "request_count": 1}})
    assert '<span class="tok-share">(100% cached)</span>' in page


def test_llm_span_omits_the_cache_share_when_no_cache_read_was_reported(tmp_path):
    # absent is not zero: with no cache_read_tokens at all this app cannot
    # tell nothing-cached from nothing-said, so it claims neither
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 18976, "prompt_tokens": 18976, "output_tokens": 407,
        "request_count": 1}})
    assert '<span class="tok-share">' not in page   # not the stylesheet's rule


def test_llm_span_has_no_total_row(tmp_path):
    # `total` is prompt + out, both already shown — and the one figure the
    # tree could not vouch for, since hermes' codex path takes the provider's
    # reported total over the computed sum
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 1429, "prompt_tokens": 19349, "output_tokens": 1089,
        "cache_read_tokens": 17920, "total_tokens": 20438,
        "request_count": 1}})
    assert "20,438" not in page
    assert [label for label, _ in _token_rows(page)] == [
        "prompt", "cache read", "in", "out", "requests"]


def test_llm_span_keeps_the_two_buckets_on_a_collapsed_row(tmp_path):
    # what went in and what came out are ~30 characters together and worth
    # scanning; their parts wait for the detail view, where each row has room
    # for its own tooltip
    page = _llm_turn(tmp_path, {**_assistant("hi"), "finish_reason": "stop",
                                "usage": {
        "input_tokens": 1429, "prompt_tokens": 19349, "output_tokens": 1089,
        "cache_read_tokens": 17920, "total_tokens": 20438,
        "request_count": 1}})
    # `.list-item` is this codebase's detail-only marker (base.html), so a
    # row without it is one the collapsed waterfall keeps
    shown = [label for label, klass in _token_rows(page)
             if "list-item" not in klass]
    assert shown == ["prompt", "out"]
    # `tokens` heads the tree on a row of its own, carrying no figure — and
    # is detail-only: beside a figure named `prompt` it says nothing new
    assert len(re.findall(r'>tokens</span>', page)) == 1
    assert re.search(r'<div class="span-detail list-item">\s*'
                     r'<span class="mode-tag"[^>]*>tokens</span>\s*</div>', page)


def test_llm_span_gives_cache_writes_a_row_of_their_own(tmp_path):
    # zero on the openai-shaped providers in the log today, but the part of
    # a prompt an anthropic cache stores — the row it earns when non-zero
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 1200, "prompt_tokens": 9200, "output_tokens": 300,
        "cache_read_tokens": 4000, "cache_write_tokens": 4000,
        "total_tokens": 9500, "request_count": 1}})
    figures = _token_figures(page)
    assert "cache write 4,000" in figures
    assert "cache read 4,000" in figures
    assert "prompt 9,200" in figures          # and the three still sum to it


def test_llm_span_shows_the_cached_fresh_split_on_a_cold_prompt(tmp_path):
    # a cold prompt (the first call of a session, say). `cache read` and `in`
    # keep their rows at zero: the detail view is where the split is read,
    # and a missing row leaves a reader guessing whether the figure is zero
    # or unreported. `prompt` and `in` repeating one number is the price.
    page = _llm_turn(tmp_path, {**_assistant("hi"), "usage": {
        "input_tokens": 18824, "prompt_tokens": 18824, "output_tokens": 199,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "reasoning_tokens": 91, "request_count": 1}})
    assert [label for label, _ in _token_rows(page)] == [
        "prompt", "cache read", "in", "out", "reasoning", "requests"]
    assert ("cache read", "list-item tok-d2") in _token_rows(page)
    assert {"cache read 0", "in 18,824"} <= set(_token_figures(page))
    assert '<span class="tok-share">(0% cached)</span>' in page
    # cache write stays out: on the codex route hermes hard-codes that zero
    # rather than measuring it, so the row would be a claim, not a reading
    assert "cache write" not in page


def test_open_llm_span_renders_without_an_end_payload(tmp_path):
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, None, name="openai-codex",
                     session="s1", turn="t1", start_data={"headers": {}}),
    ]
    atof = write_atof(tmp_path, lines)
    page = make_client(tmp_path, str(atof)).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "openai-codex" in page
    assert "badge-inflight" in page


def test_turn_page_opts_into_tailing(tmp_path):
    """The waterfall runs in time order, so new spans land at the bottom and
    a reader already there is watching the newest work."""
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    for start in ("10000000", "1000000"):      # in flight and finished alike
        page = client.get(f"/turns/turn/s1/{start}").get_data(as_text=True)
        assert "data-live-tail" in page


def test_the_turn_list_does_not_tail(tmp_path):
    """It is newest-first, so its new rows arrive at the top — scrolling to
    the bottom would walk away from them."""
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    page = client.get("/turns/").get_data(as_text=True)
    live = page.split("data-live-poll", 1)[1].split(">", 1)[0]
    assert "data-live-tail" not in live


def test_tailing_is_conditioned_on_being_at_the_bottom(tmp_path):
    """Being at the bottom is the opt-in — there is no toggle to find, and
    none to leave on by mistake. Guard the wiring, since nothing else here
    exercises the script."""
    atof = write_atof(tmp_path, two_turn_stream())
    client = make_client(tmp_path, str(atof))
    page = client.get("/turns/turn/s1/10000000").get_data(as_text=True)
    assert 'region.hasAttribute("data-live-tail") && atBottom()' in page
    # measured before the swap, applied after it
    assert page.index("const tailing =") < page.index("region.replaceWith(fresh)")
    assert page.index("region.replaceWith(fresh)") < page.index("if (tailing) window.scrollTo")


# --- the whole value, on its own page (ADR 12) ---------------------------
#
# The turn page shows excerpts. These cover the icon that leads out of one
# and the page it leads to — for llm spans of every call_role, which is the
# point: before this, an auxiliary call's prompt appeared nowhere, since the
# turn header shows the turn's user message and a compaction has none.

REQUEST = {"annotated_request": {
    "instructions": "You are Hermes Agent.",
    "messages": [{"role": "user", "content": "## Ask\n\nsummarise the turns"}]}}


def _llm_client(tmp_path, *, profile=REQUEST, end_data=None, metadata_role=None,
                uuid="L1"):
    """A client over one turn holding one llm span."""
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines(uuid, "llm", 1_100_000, 1_600_000, name="openai-codex",
                     session="s1", turn="t1", profile=profile,
                     start_data={"headers": {}},
                     end_data=end_data or _assistant("## Done\n\nall of it")),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    return make_client(tmp_path, str(write_atof(tmp_path, lines)))


def test_an_llm_span_offers_its_request_and_its_response(tmp_path):
    page = _llm_client(tmp_path).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    links = re.findall(r'class="[^"]*open-text" href="([^"]+)"', page)
    assert "/turns/span/L1/prompt" in links
    assert "/turns/span/L1/response" in links


def test_the_open_link_opens_a_new_tab(tmp_path):
    page = _llm_client(tmp_path).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    link = re.search(r'<a class="[^"]*open-text"[^>]*>', page).group(0)
    assert 'target="_blank"' in link
    assert 'rel="noopener"' in link         # every target=_blank carries it
    assert "new tab" in link                # and says so on hover


def test_the_whole_excerpt_is_the_link_not_just_the_glyph(tmp_path):
    """Clicking anywhere on the text opens it. The key beside it stays
    outside the anchor — it names the row, it is not a way into it."""
    page = _llm_client(tmp_path).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    row = re.search(r'<div class="span-detail list-item">\s*'
                    r'<span class="gen-key key-col"[^>]*>response</span>.*?</div>',
                    page, re.S).group(0)
    link = re.search(r'<a class="[^"]*open-text".*?</a>', row, re.S).group(0)
    assert "## Done" in link or "Done" in link      # the text is inside it
    assert "ic-open" in link                        # …and so is the glyph
    assert "<a " not in link[3:]                    # one anchor, not two
    assert '>response</span>' not in link           # the key is not in it


def test_an_excerpt_with_no_page_behind_it_is_not_a_link(tmp_path):
    """A dead link is worse than no link, so the value falls back to a plain
    span — the same rule the icon has always followed."""
    app = make_app(db="/nonexistent/events.db", atof="/nonexistent/atof.jsonl")
    with app.app_context():
        macros = app.jinja_env.get_template("turns/_macros.html").module
        out = str(macros.open_text(None, "just the text", False))
    assert "just the text" in out
    assert "<a " not in out and "ic-open" not in out


def test_the_open_icon_is_there_when_the_excerpt_was_not_cut_short(tmp_path):
    """A reader cannot see that a value fits, only that it ends."""
    page = _llm_client(tmp_path, end_data=_assistant("brief.")).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "/turns/span/L1/response" in page


def test_a_turn_page_shows_what_each_call_was_asked(tmp_path):
    """The excerpt the request icon hangs off: the last user message of this
    call's own request, which for an auxiliary call is the only place its
    instruction appears at all."""
    page = _llm_client(tmp_path).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "summarise the turns" in page
    assert ">prompt<" in page


def test_a_call_that_said_nothing_offers_no_response_to_open(tmp_path):
    """It only asked for tools; there is no whole response behind the icon,
    so there is no icon."""
    page = _llm_client(tmp_path, end_data=_assistant("", "terminal")).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert "/turns/span/L1/prompt" in page
    assert "/turns/span/L1/response" not in page


def test_the_full_page_renders_the_response_as_markdown(tmp_path):
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert '<div class="md-body">' in page
    assert "<h2>Done</h2>" in page          # rendered, not the literal ##
    assert "<h2>Response</h2>" in page      # …and the page's own heading


def test_the_full_page_renders_the_whole_request(tmp_path):
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/prompt").get_data(as_text=True)
    assert "You are Hermes Agent." in page  # the system prompt, not just the ask
    assert "summarise the turns" in page
    assert "<h2>Ask</h2>" in page           # the content's own heading, rendered


def test_each_message_is_boxed_under_a_label_of_its_own(tmp_path):
    """The labels are this app's and the boxes hold wire content, so they are
    two different kinds of thing on the page rather than two heading levels
    in one document."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/prompt").get_data(as_text=True)
    boxes = re.findall(r'<section id="m\d+" class="msg">.*?</section>', page, re.S)
    assert len(boxes) == 2, "instructions and the user message"
    assert '<div class="msg-label">instructions</div>' in boxes[0]
    assert "You are Hermes Agent." in boxes[0]
    assert '<div class="msg-label">user</div>' in boxes[1]
    assert "summarise the turns" in boxes[1]
    # the label is beside the message, never inside its markdown
    body = re.search(r'<div class="md-body">.*?</div>', boxes[1], re.S).group(0)
    assert "user" not in body
    assert page.count("2 message") == 1     # and the header counts them


def test_the_full_page_says_where_the_value_came_from(tmp_path):
    """Anything reconstructed names its source and is never presented as
    something the origin vouched for (design principle 2)."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/prompt").get_data(as_text=True)
    assert "annotated_request" in page
    assert "The labels are this app&#39;s" in page


def test_the_full_page_names_the_span_it_came_from(tmp_path):
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert "openai-codex" in page
    assert "L1" in page
    assert 'href="/turns/turn/s1/1000000"' in page       # back to the turn


def test_the_full_page_offers_the_raw_text_of_what_it_rendered(tmp_path):
    """Markdown is a reading of the text; a reader who suspects the reading
    needs somewhere to check, and whitespace survives nowhere else."""
    client = _llm_client(tmp_path)
    rendered = client.get("/turns/span/L1/response").get_data(as_text=True)
    assert 'href="/turns/span/L1/response?raw=1"' in rendered
    raw = client.get("/turns/span/L1/response?raw=1").get_data(as_text=True)
    assert "full-blob" in raw
    assert escape("## Done") in raw         # the characters, not a heading
    assert '<div class="md-body">' not in raw


def test_the_full_page_states_a_value_that_is_not_there(tmp_path):
    """An in-flight call has no end payload yet. The page says so rather than
    rendering blank (ADR 2)."""
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, None, name="openai-codex",
                     session="s1", turn="t1", profile=REQUEST,
                     start_data={"headers": {}}),
    ]
    client = make_client(tmp_path, str(write_atof(tmp_path, lines)))
    page = client.get("/turns/span/L1/response").get_data(as_text=True)
    assert "Nothing under this key" in page


def test_an_unknown_span_or_key_is_a_404(tmp_path):
    client = _llm_client(tmp_path)
    assert client.get("/turns/span/L1/nosuch").status_code == 404
    assert client.get("/turns/span/nosuch/response").status_code == 404


def test_a_scope_with_no_full_declared_serves_none(tmp_path):
    """A tool span has no full values, so its uuid addresses nothing — the
    route reads the same declaration the icon does."""
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="terminal",
                     session="s1", turn="t1", start_data={"command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    client = make_client(tmp_path, str(write_atof(tmp_path, lines)))
    assert client.get("/turns/span/T1/prompt").status_code == 404


def test_a_full_page_carries_no_live_poll(tmp_path):
    """A span whose value can still change is one still being written; a page
    that reflowed a prompt under someone reading it would be worse than one a
    moment old."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    # in a tag, not in base.html's script explaining the attribute
    assert not re.search(r"<[a-z][^>]*\sdata-live-poll=", page)


def test_a_span_no_turn_claimed_is_still_readable(tmp_path):
    """An llm call parented to the session rather than to a turn appears on
    no turn page — addressing it by uuid is what makes its prompt readable
    at all."""
    lines = [
        *session_scope_lines("s1", start_us=500_000),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
        # after the turn closed, hung off the session scope
        *scope_lines("L9", "llm", 2_500_000, 2_900_000, name="openai-codex",
                     session="s1", profile=REQUEST, start_data={"headers": {}},
                     end_data=_assistant("late work")),
    ]
    client = make_client(tmp_path, str(write_atof(tmp_path, lines)))
    page = client.get("/turns/span/L9/response")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "late work" in body
    assert "no turn holds this span" in body


def test_html_in_a_prompt_is_shown_not_run(tmp_path):
    """The page renders someone else's log text. Nothing in it is markup."""
    end = _assistant("here is a tag: <script>alert(1)</script>")
    page = _llm_client(tmp_path, end_data=end).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_every_call_role_gets_the_same_facility(tmp_path):
    """The declaration is on the llm *category*, so a compaction is not a
    special case — it is the same two links every model call has."""
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 1_600_000, name="auto",
                     session="s1", turn="t1", profile=REQUEST,
                     start_data={"headers": {}},
                     end_data=_assistant("## Historical Task Snapshot\n\nx")),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    lines[1] = lines[1].replace(
        '"metadata": {', '"metadata": {"call_role": "auxiliary:compression", ')
    client = make_client(tmp_path, str(write_atof(tmp_path, lines)))
    turn = client.get("/turns/turn/s1/1000000").get_data(as_text=True)
    assert "auxiliary:compression" in turn
    assert "/turns/span/L1/prompt" in turn
    page = client.get("/turns/span/L1/response").get_data(as_text=True)
    assert "auxiliary:compression" in page          # the page names the role
    assert "<h2>Historical Task Snapshot</h2>" in page


def test_a_full_page_leaves_the_tab_bar_out(tmp_path):
    """It is opened from a span, in its own tab, to read one thing. A row of
    tabs offering to navigate away is chrome for a place the reader did not
    come from."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert 'nav class="tabs"' not in page
    assert "observer status" not in page
    assert "hermes observer" in page        # …but the way home stays


def test_a_page_reached_by_navigating_keeps_the_tab_bar(tmp_path):
    """The override is for the full page alone; losing the bar anywhere else
    would lose the reader's place in the app."""
    page = _llm_client(tmp_path).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    assert 'nav class="tabs"' in page


def test_a_full_pages_links_are_grouped_left_like_the_turn_pages(tmp_path):
    """Same row, same classes as _item_nav.html: the way back first, the
    incidental link muted a shade beside it, nothing pushed right."""
    client = _llm_client(tmp_path)
    nav = re.search(r'<nav class="event-nav"[^>]*>.*?</nav>',
                    client.get("/turns/span/L1/response").get_data(as_text=True),
                    re.S).group(0)
    assert nav.index("back to the turn") < nav.index(">raw<")
    # both inside the one left-hand span, as the turn page groups them
    left = nav[nav.index("<span>"):nav.index("</nav>")]
    for label in ("back to the turn", ">raw<"):
        assert label in left, label
    assert nav.count("<nav") == 1


def test_a_full_page_does_not_offer_the_turn_list(tmp_path):
    """It was opened in its own tab from one span. The places it owes a
    reader are where that span was and what its value looks like unrendered
    — a jump to every turn strands them, like the tab bar would."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert "all turns" not in page
    assert 'href="/turns/"' not in page
    assert "back to the turn" in page


def test_the_raw_link_leads_back_to_the_rendered_view(tmp_path):
    client = _llm_client(tmp_path)
    raw = client.get("/turns/span/L1/response?raw=1").get_data(as_text=True)
    assert ">rendered<" in raw
    assert 'href="/turns/span/L1/response"' in raw


def test_the_pages_own_words_are_boxed_apart_from_the_value(tmp_path):
    """The value opens on a heading the model wrote. Everything this app says
    — the title, the facts, the provenance — sits in a panel above it, so
    none of it can be read as the value's own first lines."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/prompt").get_data(as_text=True)
    head = re.search(r'<header class="full-head[^"]*">.*?</header>', page, re.S)
    assert head, "the header panel is missing"
    head = head.group(0)
    assert "<h2>Prompt</h2>" in head             # the heading
    assert "openai-codex" in head                # the facts
    assert "annotated_request" in head           # the provenance
    # and nothing of ours loose between the panel and the first message box.
    # The contents list is allowed there: it is a <nav> of links, structured
    # chrome rather than prose, and cannot be read as the value's own words.
    between = page[page.index("</header>"):
                   re.search(r'<section id="m\d+" class="msg">', page).start()]
    between = re.sub(r'<nav class="msg-nav".*?</nav>', "", between, flags=re.S)
    assert re.sub(r"<[^>]+>|\s", "", between) == ""


def test_the_header_panel_is_styled_apart_from_the_rendered_markdown(tmp_path):
    """Class names carry the distinction, so a rename that lost it would
    leave the page looking like the value titled itself."""
    css = (REPO_ROOT / "templates" / "base.html").read_text()
    css = re.sub(r"\{#.*?#\}", "", css, flags=re.S)     # drop Jinja comments
    for selector in (r"\.full-head\b", r"\.full-head h2\b", r"\.md-body\b"):
        assert re.search(rf"{selector}[^{{}}\n]*\{{", css), selector



def test_the_response_row_is_labelled_like_every_other_row(tmp_path):
    """Prose starting mid-row with no key in front of it is prose a reader
    has to identify before they can read it — which is what `prompt` and
    `tokens` already spare them."""
    page = _llm_turn(tmp_path, {**_assistant("Here is the answer."),
                                "finish_reason": "stop"})
    row = re.search(r'<div class="span-detail list-item">\s*'
                    r'<span class="gen-key key-col"[^>]*>response</span>.*?</div>',
                    page, re.S)
    assert row, "the response row has no key"
    assert "Here is the answer." in row.group(0)


def test_a_call_that_said_nothing_has_no_response_row(tmp_path):
    """The label follows the value: a call that only asked for tools said
    nothing, so there is no row to label."""
    page = _llm_turn(tmp_path, {**_assistant("", "terminal"),
                                "finish_reason": "tool_calls"})
    assert ">response<" not in page


def test_the_llm_value_rows_start_at_one_edge(tmp_path):
    """Values a reader compares down the span, so their left edges line up:
    the keys reserve one column rather than each taking its own width."""
    page = _llm_client(tmp_path, end_data={
        **_assistant("## Done\n\nall of it", "read_file"),
        "finish_reason": "complete"}).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    # `[^"]*` and not the bare class string: `finish_reason` carries
    # `.list-item` beside `.key-col`, and an exact match would drop the
    # longest label in the column from the width check below — the one row
    # this test exists to catch
    labels = re.findall(r'class="gen-key key-col[^"]*"[^>]*>([^<]+)</span>', page)
    assert {"finish_reason", "prompt", "response", "tool_calls"} <= set(labels), labels
    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    width = re.search(r"\.gen-key\.key-col[^{}\n]*\{[^}]*min-width:\s*(\d+)ch", css)
    assert width, "key-col reserves no column"
    # the column has to outlast the longest key that sits in it — a label
    # that overflows is the one row whose value does not line up, and
    # nothing else would catch it
    assert int(width.group(1)) > max(len(x) for x in labels), labels


def test_the_llm_value_rows_share_a_right_edge(tmp_path):
    """`.wide` on all three, so the column has two edges and not one."""
    page = _llm_client(tmp_path, end_data=_assistant(
        "## Done\n\nall of it", "read_file")).get(
        "/turns/turn/s1/1000000").get_data(as_text=True)
    for key in ("prompt", "response", "tool_calls"):
        m = re.search(rf'>{key}</span>\s*<[^>]*class="([^"]*)"', page)
        assert m, key
        assert "wide" in m.group(1).split(), (key, m.group(1))


# --- the request page's contents list --------------------------------------

TOOL_REQUEST = {"annotated_request": {
    "instructions": "You are Hermes Agent.",
    "messages": [
        {"role": "user", "content": "go"},
        {"role": "provider_native", "kind": "reasoning", "value": {"s": []}},
        {"role": "tool_call", "name": "read_file", "call_id": "c1",
         "arguments": "{}"},
        {"role": "tool_call", "name": "read_file", "call_id": "c2",
         "arguments": "{}"},
        {"role": "tool_result", "call_id": "c1", "output": "one"},
        {"role": "tool_result", "call_id": "c2", "output": "two"}]}}


def _full_page(tmp_path, profile=TOOL_REQUEST):
    return _llm_client(tmp_path, profile=profile).get(
        "/turns/span/L1/prompt").get_data(as_text=True)


def _nav_items(page):
    nav = re.search(r'<nav class="msg-nav".*?</nav>', page, re.S)
    if not nav:
        return []
    return re.findall(r'<li( class="nav-nested")?><a href="(#m\d+)">([^<]*)</a>',
                      nav.group(0))


def test_the_contents_list_has_one_entry_per_message_in_page_order(tmp_path):
    items = _nav_items(_full_page(tmp_path))
    assert [(bool(n), label) for n, _, label in items] == [
        (False, "instructions"),
        (False, "user"),
        (False, "provider_native · reasoning"),
        (False, "tool_call · read_file"),
        (True, "tool_result"),          # indented, as it is on the page
        (False, "tool_call · read_file"),
        (True, "tool_result"),
    ]


def test_every_contents_link_lands_on_a_message(tmp_path):
    """A nav entry pointing at no anchor is a dead link on a page whose
    whole job is to be read through."""
    page = _full_page(tmp_path)
    targets = [href.lstrip("#") for _, href, _ in _nav_items(page)]
    ids = re.findall(r'<section id="(m\d+)"', page)
    assert targets == ids            # same set, same order, none missing


def test_the_contents_labels_are_the_section_labels_themselves(tmp_path):
    """Two vocabularies for one page is how a nav drifts from what it
    names — the entry has to say what the band it lands on says."""
    page = _full_page(tmp_path)
    labels = re.findall(r'<div class="msg-label">([^<]*)</div>', page)
    assert [label for _, _, label in _nav_items(page)] == labels


def test_a_single_message_gets_no_contents_list(tmp_path):
    """A list of one names the thing the reader is already looking at."""
    page = _full_page(tmp_path, profile={"annotated_request": {
        "messages": [{"role": "user", "content": "just the one"}]}})
    assert _nav_items(page) == []
    assert 'class="msg-nav"' not in page


def test_a_value_that_is_not_sections_gets_no_contents_list(tmp_path):
    """The response page is one document; there is nothing to list."""
    page = _llm_client(tmp_path).get(
        "/turns/span/L1/response").get_data(as_text=True)
    assert 'class="msg-nav"' not in page


def test_the_contents_list_survives_the_raw_view(tmp_path):
    """`raw` changes how each message is rendered, not how many there are."""
    page = _llm_client(tmp_path, profile=TOOL_REQUEST).get(
        "/turns/span/L1/prompt?raw=1").get_data(as_text=True)
    assert len(_nav_items(page)) == 7


def test_the_contents_list_is_styled_as_a_column_beside_the_messages(tmp_path):
    """Class names carry the layout, so a rename that lost them would leave
    the list stacked on top of what it lists."""
    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    for selector in (r"\.full-body\b", r"\.full-sections\b", r"\.msg-nav\b"):
        assert re.search(rf"{selector}[^{{}}\n]*\{{", css), selector
    # nothing marks which message was jumped to: it is at the top of the
    # viewport, which says it already, and a ring would linger afterwards
    assert ":target" not in css
    # sticky, so it stays with the messages it lists rather than scrolling away
    assert re.search(r"\.msg-nav[^{}]*\{[^}]*position:\s*sticky", css)
    # and it yields the two-column layout when there is no room for it
    assert re.search(r"@media[^{]*max-width[^{]*\{\s*\.full-body\s*\{[^}]*display:\s*block", css)


def test_the_contents_list_offers_a_way_back_to_the_top(tmp_path):
    """Reaching the top of a thousand-line request should not mean scrolling
    there. Named for where it goes: the page's own title was tried and reads
    as one more part, since every entry below it is part of the Prompt."""
    page = _full_page(tmp_path)
    nav = re.search(r'<nav class="msg-nav".*?</nav>', page, re.S).group(0)
    top = re.search(r'<a class="nav-top" href="#top">([^<]*)</a>', nav)
    assert top, "no way back to the top"
    assert "top" in top.group(1)
    # above the list, not one of the messages
    assert nav.index('class="nav-top"') < nav.index("<ul>")
    assert "nav-top" not in nav[nav.index("<ul>"):]


def test_the_way_back_to_the_top_lands_at_the_actual_top(tmp_path):
    """`#top` with nothing carrying that id is a link that silently does
    nothing. Landing *nearly* at the top is the subtler bug: an anchor on
    anything the page renders sits below the `hermes observer` heading, so
    the jump stops short of it."""
    page = _full_page(tmp_path)
    assert 'id="top"' in page, "nothing carries the anchor"
    # it is on the site heading, and nothing renders above that
    assert re.search(r'<h1 id="top"><a[^>]*>hermes observer</a></h1>', page)
    anchor = page.index('<h1 id="top"')
    body = page.index("<body>")
    between = page[body + len("<body>"):anchor]
    assert re.sub(r"<[^>]+>|\s|\{#.*?#\}", "", between) == "", between
    # …and the jump reaches scroll position 0 rather than pinning the
    # heading to the viewport edge with the page's top margin scrolled off
    # above it. A "top" you can still scroll up from is not the top.
    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    margin = re.search(r"#top\s*\{[^}]*scroll-margin-top:\s*([\d.]+)rem", css)
    assert margin, "nothing holds the page's own margin open above the heading"
    # generously past the heading's real offset; overshoot clamps at zero
    assert float(margin.group(1)) >= 3


def test_anchor_jumps_animate_unless_motion_is_unwelcome(tmp_path):
    """One click can move a reader thousands of lines; an instant cut leaves
    them unsure whether the page moved or was replaced."""
    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    assert re.search(r"html\s*\{[^}]*scroll-behavior:\s*smooth", css)
    assert re.search(r"prefers-reduced-motion[^{]*\{\s*html\s*\{"
                     r"[^}]*scroll-behavior:\s*auto", css)


def test_message_boxes_fill_the_column_beside_the_contents_list(tmp_path):
    """No reading-measure cap on a box holding JSON and file contents: at
    62rem a third of a wide window sat empty, and width is what saves
    scrolling through a tool result."""
    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    msg = re.search(r"\n\s*\.msg \{([^}]*)\}", css)
    assert msg, ".msg is not styled"
    assert "max-width" not in msg.group(1), msg.group(1)
    # bounded by its column instead, whose min-width:0 stops one long
    # unbroken line pushing the whole page wider
    sections = re.search(r"\.full-sections \{([^}]*)\}", css).group(1)
    assert "min-width: 0" in sections


def test_the_header_keeps_step_with_the_width_of_what_it_heads(tmp_path):
    """A panel narrower than the messages under it reads as a mistake; over
    a value that is one document, both keep the reading measure."""
    client = _llm_client(tmp_path, profile=TOOL_REQUEST)
    sectioned = client.get("/turns/span/L1/prompt").get_data(as_text=True)
    assert re.search(r'<header class="full-head wide">', sectioned)

    one_document = client.get("/turns/span/L1/response").get_data(as_text=True)
    assert re.search(r'<header class="full-head">', one_document)

    css = re.sub(r"\{#.*?#\}", "",
                 (REPO_ROOT / "templates" / "base.html").read_text(), flags=re.S)
    assert re.search(r"\.full-head \{[^}]*max-width:\s*62rem", css)
    assert re.search(r"\.full-head\.wide \{[^}]*max-width:\s*none", css)
