"""Span reading tests: one span's payloads, and what they mean.

Named `test_span_readings` rather than `test_spans` because pytest collects
these modules by bare filename, so `plugins/memory/mem0/tests/test_spans.py` — mem0's
half of the same idea — already has that name.

The other half of what `test_assembler.py` used to hold. These assert facts a
span can answer about itself — a tool's arguments, a call's token counts, the
entry a memory write matched — where the assembler tests assert what several
events add up to.

Spans come from the real parser and assembler (`streams.assemble_lines`)
rather than being built by hand, so a reading is always tested against a log
line hermes could have written.
"""

import json

from plugins.turns.spans import resolve_memory_entries
from streams import (SESSION_SCOPE_UUID, assemble_lines, mark_line,
                     scope_lines, session_scope_lines, two_turn_stream)


def test_a_span_never_needs_the_assembler_to_read_itself():
    """The split is only worth having while the dependency runs one way:
    `assembler` imports `spans`, never the reverse. A span that reached for
    its turn would put reading and assembly back in one knot, with an import
    cycle as the first symptom."""
    import inspect

    from plugins.turns import spans

    source = inspect.getsource(spans)
    assert "import assembler" not in source
    assert "from plugins.turns.assembler" not in source


# --- token counts reported on the stream, not on the end event -----------
# The openrouter route reports usage only on the last chunk of a stream and
# leaves the span's own end payload with a null `usage`. Every count is in
# the log; without the fallback none of them reaches the page.

def _streamed_llm(end_usage, chunk_usage_data, *, chunks=2):
    """One llm span whose stream carries counts its end event does not."""
    lines = [
        *session_scope_lines("s1", start_us=0),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 3_100_000, name="openrouter",
                     session="s1", turn="t1",
                     profile={"model_name": "moonshotai/kimi-k3"},
                     end_data={"usage": end_usage}),
    ]
    # only the last chunk of a stream reports anything
    for i in range(chunks - 1):
        lines.append(mark_line("llm.chunk", 1_200_000 + i, parent="L1",
                               data={"chunk_index": i, "usage": None}))
    lines.append(mark_line("llm.chunk", 3_000_000, parent="L1",
                           data={"chunk_index": chunks - 1,
                                 "usage": chunk_usage_data}))
    return lines


CHUNK_USAGE = {"prompt_tokens": 18782, "cache_read_tokens": 1792,
               "completion_tokens": 2248, "total_tokens": 21030}


def test_a_span_falls_back_to_the_counts_its_stream_reported():
    assembly = assemble_lines(_streamed_llm(None, CHUNK_USAGE))
    (span,) = assembly.sessions[0].turns[0].spans
    assert span.usage == {"prompt_tokens": 18782, "cache_read_tokens": 1792,
                          "output_tokens": 2248, "total_tokens": 21030,
                          "input_tokens": 18782 - 1792}


def test_streamed_counts_reach_the_token_tree_and_the_tooltip():
    assembly = assemble_lines(_streamed_llm(None, CHUNK_USAGE))
    (span,) = assembly.sessions[0].turns[0].spans
    assert [(r["label"], r["value"]) for r in span.token_rows] == [
        ("prompt", "18,782"), ("cache read", "1,792"), ("in", "16,990"),
        ("out", "2,248"),
    ]
    assert span.token_rows[0]["share"] == "10% cached"
    assert span.usage_summary == "prompt 18,782 / out 2,248 tokens"


def test_the_end_payload_wins_over_the_stream_when_both_report():
    """Same call counted twice is still one call: the end event is the
    provider's final word and the chunk is a running one."""
    assembly = assemble_lines(_streamed_llm(
        {"prompt_tokens": 999, "output_tokens": 9}, CHUNK_USAGE))
    (span,) = assembly.sessions[0].turns[0].spans
    assert span.usage == {"prompt_tokens": 999, "output_tokens": 9}


def test_a_stream_that_reported_no_counts_leaves_usage_absent():
    assembly = assemble_lines(_streamed_llm(None, None))
    (span,) = assembly.sessions[0].turns[0].spans
    assert span.usage is None
    assert span.token_rows == []


def test_a_span_with_nothing_to_count_has_no_tooltip_summary():
    """It used to interpolate `?` for a missing figure, which read as a
    count the provider had withheld rather than one this app never sought."""
    assembly = assemble_lines(_streamed_llm({"total_tokens": 30251}, None))
    (span,) = assembly.sessions[0].turns[0].spans
    assert span.usage == {"total_tokens": 30251}   # nothing is lost
    assert span.token_rows == []                   # but no row can be vouched for
    assert span.usage_summary is None


def test_command_and_workdir_type_guard_odd_start_data():
    # start payloads are opaque: a JSON string still yields the fields,
    # anything non-dict yields None rather than an error
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_200_000, name="terminal",
                     session="s1", turn="t1",
                     start_data='{"command": "ls", "workdir": "/tmp"}'),
        *scope_lines("T2", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1", start_data="not json"),
        *scope_lines("T3", "tool", 1_500_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"command": 42, "workdir": ""}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    t1, t2, t3 = assemble_lines(lines).sessions[0].turns[0].spans
    assert t1.command == "ls" and t1.workdir == "/tmp"
    assert t2.command is None and t2.workdir is None
    assert t3.command is None and t3.workdir is None


def test_skill_scope_details_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_200_000, name="skill_view",
                     session="s1", turn="t1",
                     start_data={"name": "adaptive-information-gathering",
                                 "file_path": "references/fallbacks.md"}),
        *scope_lines("S2", "tool", 1_300_000, 1_400_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "patch", "name": "job-seeker",
                                 "old_string": "a", "new_string": "b"}),
        *scope_lines("S3", "tool", 1_450_000, 1_480_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "create", "name": "doc-review",
                                 "category": "productivity", "content": "..."}),
        *scope_lines("S4", "tool", 1_490_000, 1_495_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "delete", "name": "old-skill",
                                 "absorbed_into": "job-seeker"}),
        *scope_lines("T1", "tool", 1_500_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"name": "not-a-skill", "command": "ls",
                                 "action": "not-a-skill-action",
                                 "category": "not-a-skill-category",
                                 "absorbed_into": "not-a-skill"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    view, manage, create, delete, other = \
        assemble_lines(lines).sessions[0].turns[0].spans
    assert view.skill_name == "adaptive-information-gathering"
    assert view.skill_file_path == "references/fallbacks.md"
    assert view.skill_action is None       # skill_view has no action
    assert view.skill_category is None
    assert view.skill_absorbed_into is None
    assert manage.skill_name == "job-seeker"
    assert manage.skill_action == "patch"
    assert manage.skill_file_path is None
    assert manage.skill_category is None   # only "create" carries a category
    assert manage.skill_absorbed_into is None
    assert manage.skill_old_string == "a"
    assert manage.skill_new_string == "b"
    assert create.skill_old_string is None  # only "patch" carries the strings
    assert create.skill_new_string is None
    assert create.skill_action == "create"
    assert create.skill_category == "productivity"
    assert delete.skill_action == "delete"
    assert delete.skill_absorbed_into == "job-seeker"  # only "delete" has it
    # the generic name/action/category keys mean nothing outside a skill scope
    assert other.skill_name is None
    assert other.skill_action is None
    assert other.skill_file_path is None
    assert other.skill_category is None
    assert other.skill_absorbed_into is None
    assert other.skill_old_string is None
    assert other.skill_new_string is None


def test_skill_patch_keeps_an_empty_new_string():
    # "" is a real patch — it deletes the matched text — so it must not fold
    # into None the way an absent key does
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_200_000, name="skill_manage",
                     session="s1", turn="t1",
                     start_data={"action": "patch", "name": "job-seeker",
                                 "old_string": "drop me", "new_string": ""}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    span = assemble_lines(lines).sessions[0].turns[0].spans[0]
    assert span.skill_old_string == "drop me"
    assert span.skill_new_string == ""


def test_vision_analyze_details_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("V1", "tool", 1_100_000, 1_200_000, name="vision_analyze",
                     session="s1", turn="t1",
                     start_data={"image_url": "/tmp/cv.png",
                                 "question": "any typos?"}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"image_url": "/x", "question": "q",
                                 "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    vision, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert vision.vision_image_url == "/tmp/cv.png"
    assert vision.vision_question == "any typos?"
    # the generic image_url/question keys mean nothing outside the scope
    assert other.vision_image_url is None
    assert other.vision_question is None


def test_file_tool_path_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_200_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "replace", "path": "/home/u/notes.md",
                                 "old_string": "a", "new_string": "b"}),
        *scope_lines("L1", "llm", 1_300_000, 1_400_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    patch, llm = assemble_lines(lines).sessions[0].turns[0].spans
    assert patch.path == "/home/u/notes.md"
    assert llm.path is None


def test_patch_mode_paths_from_v4a_headers():
    patch_text = ("*** Begin Patch\n"
                  "*** Update File: /home/u/a.md\n@@\n-x\n+y\n"
                  "*** Update File: /home/u/a.md\n@@\n-p\n+q\n"
                  "*** Add File: /home/u/b.md\n+hello\n"
                  "*** Move File: /home/u/c.md -> /home/u/d.md\n"
                  "*** End Patch")
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_200_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "patch", "patch": patch_text}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"patch": "not-a-file-edit", "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    patch, other = assemble_lines(lines).sessions[0].turns[0].spans
    # deduped, in patch order; a move keeps its "old -> new" whole
    assert patch.patch_paths == ["/home/u/a.md", "/home/u/b.md",
                                 "/home/u/c.md -> /home/u/d.md"]
    assert patch.path is None
    # the generic "patch" key means nothing outside a patch scope
    assert other.patch_paths == []
    assert other.patch_mode is None and other.patch_text is None


def test_patch_mode_and_replaced_text():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("P1", "tool", 1_100_000, 1_200_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"mode": "replace", "path": "/home/u/a.md",
                                 "old_string": "was", "new_string": "now"}),
        # the tool defaults to replace, so a payload that names no mode and
        # carries no V4A text is one (see $HERMES_SOURCE/tools/file_tools.py patch_tool)
        *scope_lines("P2", "tool", 1_250_000, 1_280_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"path": "/home/u/b.md",
                                 "old_string": "gone", "new_string": ""}),
        # …and one carrying a V4A text is a patch even unnamed
        *scope_lines("P3", "tool", 1_300_000, 1_400_000, name="patch",
                     session="s1", turn="t1",
                     start_data={"patch": "*** Begin Patch\n"
                                          "*** Add File: /home/u/c.md\n+hi\n"
                                          "*** End Patch"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    replace, defaulted, v4a = assemble_lines(lines).sessions[0].turns[0].spans
    assert replace.patch_mode == "replace"
    assert replace.patch_old_string == "was"
    assert replace.patch_new_string == "now"
    assert replace.patch_text is None
    assert defaulted.patch_mode == "replace"
    # "" is a real patch — it deletes the matched text — so it must not fold
    # into None the way an absent key does
    assert defaulted.patch_new_string == ""
    assert v4a.patch_mode == "patch"
    assert v4a.patch_paths == ["/home/u/c.md"]
    # patch mode carries neither side; the patch text is the whole story
    assert v4a.patch_old_string is None and v4a.patch_new_string is None


def test_search_files_query_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("S1", "tool", 1_100_000, 1_200_000, name="search_files",
                     session="s1", turn="t1",
                     start_data={"pattern": "TODO", "target": "content",
                                 "path": "/home/u/proj", "file_glob": "*.py"}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"pattern": "not-a-search", "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    search, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert search.search_pattern == "TODO"
    assert search.file_glob == "*.py"
    assert search.path == "/home/u/proj"
    # the generic "pattern" key means nothing outside a search_files scope
    assert other.search_pattern is None and other.file_glob is None


def test_search_query_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_200_000, name="web_search",
                     session="s1", turn="t1",
                     start_data={"query": "flask blueprint url_prefix", "limit": 5}),
        *scope_lines("M1", "tool", 1_250_000, 1_280_000, name="mem0_search",
                     session="s1", turn="t1",
                     start_data={"query": "What is the user's name?",
                                 "rerank": True, "top_k": 10}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"query": "not-a-search", "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    web, mem, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert web.search_query == "flask blueprint url_prefix"
    assert mem.search_query == "What is the user's name?"
    # the generic "query" key means nothing outside the search scopes;
    # session_search's query is handled by the mode-aware properties, not here
    assert other.search_query is None


def _one_span(name, start_data, end_data=None):
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("SS1", "tool", 1_100_000, 1_200_000, name=name,
                     session="s1", turn="t1",
                     start_data=start_data, end_data=end_data),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    return assemble_lines(lines).sessions[0].turns[0].spans[0]


def test_session_search_discover_mode():
    # the ATOF log carries the tool result as a JSON *string*; _as_dict decodes
    span = _one_span(
        "session_search",
        {"query": "jobs report", "limit": 5, "sort": "newest"},
        '{"success": true, "mode": "discover", "results": [],'
        ' "count": 2, "sessions_searched": 3}')
    assert span.session_search_mode == "discover"
    assert span.session_search_summary == "jobs report"
    labels = [(s["label"], s["value"]) for s in span.session_search_stats]
    assert labels == [("count", 2), ("sessions searched", 3)]
    assert all(s["tooltip"] for s in span.session_search_stats)


def test_session_search_scroll_mode():
    span = _one_span(
        "session_search",
        {"session_id": "1941bdb78476", "around_message_id": 14571, "window": 20},
        '{"success": true, "mode": "scroll", "session_id": "1941bdb78476",'
        ' "around_message_id": 14571, "window": 20, "messages": [],'
        ' "messages_before": 7, "messages_after": 4}')
    assert span.session_search_mode == "scroll"
    assert span.session_search_summary == \
        "session 1941bdb78476 · around msg 14571 · window 20"
    assert [(s["label"], s["value"]) for s in span.session_search_stats] == \
        [("before", 7), ("after", 4)]


def test_session_search_read_mode():
    span = _one_span(
        "session_search",
        {"session_id": "1941bdb78476"},
        '{"success": true, "mode": "read", "session_id": "1941bdb78476",'
        ' "message_count": 812, "truncated": true, "messages": []}')
    assert span.session_search_mode == "read"
    assert span.session_search_summary == "session 1941bdb78476"
    labels = [(s["label"], s["value"]) for s in span.session_search_stats]
    assert labels == [("messages", 812), ("truncated", "")]


def test_session_search_browse_mode():
    span = _one_span(
        "session_search",
        {},
        '{"success": true, "mode": "browse", "results": [], "count": 4}')
    assert span.session_search_mode == "browse"
    assert span.session_search_summary == "recent sessions"
    assert [(s["label"], s["value"]) for s in span.session_search_stats] == \
        [("sessions", 4)]


def test_session_search_mode_inferred_from_start_while_open():
    # no end payload yet (span in flight) — mode comes from start-payload keys,
    # matching the tool's own dispatch precedence
    scroll = _one_span("session_search",
                       {"session_id": "s9", "around_message_id": 3, "window": 5})
    assert scroll.session_search_mode == "scroll"
    read = _one_span("session_search", {"session_id": "s9"})
    assert read.session_search_mode == "read"
    discover = _one_span("session_search", {"query": "hi"})
    assert discover.session_search_mode == "discover"
    browse = _one_span("session_search", {})
    assert browse.session_search_mode == "browse"
    # open spans have no end payload, so no result stats yet
    assert scroll.session_search_stats == []


def test_session_search_properties_inert_on_other_scopes():
    # another scope's end payload carrying a "count" key must not be mistaken
    # for a session_search result
    web = _one_span("web_search", {"query": "flask"}, '{"count": 9}')
    assert web.session_search_mode is None
    assert web.session_search_summary is None
    assert web.session_search_stats == []


def test_execute_code_first_line_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("E1", "tool", 1_100_000, 1_200_000, name="execute_code",
                     session="s1", turn="t1",
                     start_data={"code": "import json\nprint(json.dumps({}))"}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"code": "not-code", "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    exe, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert exe.code == "import json\nprint(json.dumps({}))"
    assert exe.code_first_line == "import json"
    # the generic "code" key means nothing outside an execute_code scope
    assert other.code is None and other.code_first_line is None


def test_web_extract_urls_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_200_000, name="web_extract",
                     session="s1", turn="t1",
                     start_data={"char_limit": 30000,
                                 "urls": ["https://a.example/one",
                                          "https://b.example/two"]}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"urls": ["https://c.example"], "command": "ls"}),
        *scope_lines("W2", "tool", 1_500_000, 1_600_000, name="web_extract",
                     session="s1", turn="t1",
                     start_data={"urls": "not-a-list"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    extract, other, odd = assemble_lines(lines).sessions[0].turns[0].spans
    assert extract.web_extract_urls == ["https://a.example/one",
                                        "https://b.example/two"]
    # the "urls" key means nothing outside a web_extract scope, and a
    # malformed payload yields an empty list rather than an error
    assert other.web_extract_urls == []
    assert odd.web_extract_urls == []


def test_todo_contents_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("D1", "tool", 1_100_000, 1_200_000, name="todo",
                     session="s1", turn="t1",
                     start_data={"todos": [
                         {"id": "inventory", "status": "in_progress",
                          "content": "Inventory the existing report"},
                         {"id": "discover", "status": "pending",
                          "content": "Run refreshed discovery"},
                         {"id": "bare", "status": "pending"},
                     ]}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"todos": [{"content": "x"}], "command": "ls"}),
        *scope_lines("D2", "tool", 1_500_000, 1_600_000, name="todo",
                     session="s1", turn="t1", start_data={}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    todo, other, read = assemble_lines(lines).sessions[0].turns[0].spans
    # merge-mode items without content are skipped, not errors
    assert todo.todo_contents == ["Inventory the existing report",
                                  "Run refreshed discovery"]
    # the "todos" key means nothing outside a todo scope, and a todo call
    # with no todos is a read of the current list
    assert other.todo_contents == []
    assert read.todo_contents == []


def test_delegate_task_briefs_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("D1", "tool", 1_100_000, 1_200_000, name="delegate_task",
                     session="s1", turn="t1",
                     start_data={"tasks": [
                         {"goal": "Sweep Luma listings",
                          "context": "Window is 20-26 Jul"},
                         {"goal": "Sweep Meetup listings"},
                         {"context": "no goal — dropped"},
                     ]}),
        *scope_lines("D2", "tool", 1_300_000, 1_400_000, name="delegate_task",
                     session="s1", turn="t1",
                     start_data={"goal": "Single-mode goal",
                                 "context": "Single-mode context"}),
        *scope_lines("T1", "tool", 1_500_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"tasks": [{"goal": "x"}], "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    batch, single, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert batch.delegate_tasks == [
        {"goal": "Sweep Luma listings", "context": "Window is 20-26 Jul"},
        {"goal": "Sweep Meetup listings", "context": None},
    ]
    assert batch.delegate_goals == ["Sweep Luma listings",
                                    "Sweep Meetup listings"]
    # single-task calls carry goal/context at the payload top level
    assert single.delegate_tasks == [
        {"goal": "Single-mode goal", "context": "Single-mode context"}]
    # the keys mean nothing outside a delegate_task scope
    assert other.delegate_tasks == []


def test_string_end_data_yields_no_usage_or_finish_reason():
    # the real nemo_relay exporter emits hermes tool results as raw JSON
    # strings in the end event's data field — payload accessors must
    # type-guard, not assume dicts (regression: 500 on the turn page)
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_100_000, 1_600_000, name="mem0_search",
                     session="s1", turn="t1",
                     end_data='{"results": [{"memory": "a fact"}]}'),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    assembly = assemble_lines(lines)
    (span,) = assembly.sessions[0].turns[0].spans
    assert span.usage is None
    assert span.finish_reason is None
    assert span.duration_us == 500_000
    assert assembly.sessions[0].turns[0].tool_us == 500_000


def test_memory_scope_single_op_shape():
    """The `memory` tool's single-op shape — action plus the entry text.
    A different tool from the mem0 scopes above ($HERMES_SOURCE/tools/memory_tool.py)."""
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_200_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "replace", "target": "user",
                                 "old_text": "User does not want auto-commits",
                                 "content": "User requires a staged diff "
                                            "before commits."}),
        *scope_lines("W2", "tool", 1_250_000, 1_260_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "add", "target": "memory",
                                 "content": "Port 5090 is the observer."}),
        # a remove names only the entry it drops
        *scope_lines("W3", "tool", 1_300_000, 1_310_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "remove", "target": "user",
                                 "old_text": "Stale preference"}),
        *scope_lines("T1", "tool", 1_400_000, 1_450_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"action": "add", "target": "user",
                                 "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    replace, add, remove, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert replace.memory_action == "replace"
    assert replace.memory_target == "user"
    assert replace.memory_ops == [{
        "action": "replace",
        "old_text": "User does not want auto-commits",
        "content": "User requires a staged diff before commits.",
        "text": "User requires a staged diff before commits.",
        # nothing listed the store in this turn, so the − side stays the
        # fragment the log holds and claims nothing more
        "old_entry": None,
        "old_shown": "User does not want auto-commits",
        "old_entry_note": None,
    }]
    assert add.memory_action == "add"
    assert add.memory_target == "memory"
    assert add.memory_ops[0]["old_text"] is None
    assert add.memory_ops[0]["text"] == "Port 5090 is the observer."
    # the entry a remove drops is all it has, so that is its summary text
    assert remove.memory_ops[0]["content"] is None
    assert remove.memory_ops[0]["text"] == "Stale preference"
    # the generic "action"/"target" keys mean nothing outside a memory scope
    assert other.memory_action is None and other.memory_target is None
    assert other.memory_ops == []


def test_memory_scope_batch_shape():
    """An operations list is applied atomically and reads as one span; the
    tool dispatches on it before `action`, so it names the mode."""
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("W1", "tool", 1_100_000, 1_200_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"target": "user", "operations": [
                         {"action": "replace", "old_text": "Former HCA",
                          "content": "Former HCA Ingest architect at EBI."},
                         {"action": "remove", "old_text": "Atlas weekly"},
                         {"action": "add", "content": "Prefers short notes."},
                         "not-an-op",
                         {"content": "no action, skipped"},
                     ]}),
        # a staged batch replayed from the approval queue carries both keys;
        # the list still wins, matching the tool's dispatch order
        *scope_lines("W2", "tool", 1_250_000, 1_260_000, name="memory",
                     session="s1", turn="t1",
                     start_data={"action": "batch", "target": "memory",
                                 "operations": [{"action": "add",
                                                 "content": "One entry."}]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    batch, staged = assemble_lines(lines).sessions[0].turns[0].spans
    assert batch.memory_action == "batch"
    assert batch.memory_target == "user"
    # the two malformed entries are dropped, not raised on
    assert [op["action"] for op in batch.memory_ops] == ["replace", "remove", "add"]
    assert batch.memory_ops[0]["text"] == "Former HCA Ingest architect at EBI."
    assert batch.memory_ops[1]["text"] == "Atlas weekly"
    assert staged.memory_action == "batch"
    assert staged.memory_ops == [{"action": "add", "old_text": None,
                                  "content": "One entry.", "text": "One entry.",
                                  "old_entry": None, "old_shown": None,
                                  "old_entry_note": None}]


def test_failed_spans_carry_the_tools_own_error():
    """Every hermes tool reports a failure the same two ways — an "error"
    status on the end event and an "error" string in its payload — so one
    pair of properties serves every scope."""
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("F1", "tool", 1_100_000, 1_200_000, name="skill_view",
                     session="s1", turn="t1", end_status="error",
                     start_data={"name": "events-hunter"},
                     end_data={"success": False,
                               "error": "[Errno 24] Too many open files"}),
        # payloads reach the reader as raw JSON strings too
        *scope_lines("F2", "tool", 1_250_000, 1_300_000, name="terminal",
                     session="s1", turn="t1", end_status="error",
                     start_data={"command": "rm -rf /"},
                     end_data='{"output": "", "exit_code": -1,'
                              ' "error": "BLOCKED: no consent"}'),
        *scope_lines("OK", "tool", 1_350_000, 1_400_000, name="read_file",
                     session="s1", turn="t1", end_status="ok",
                     start_data={"path": "/home/u/a.md"},
                     end_data={"content": "hi"}),
        # still open: no end event, so no status either
        *scope_lines("OPEN", "tool", 1_450_000, name="web_search",
                     session="s1", turn="t1", start_data={"query": "q"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    skill, terminal, ok, open_ = assemble_lines(lines).sessions[0].turns[0].spans
    assert skill.failed and skill.error == "[Errno 24] Too many open files"
    assert terminal.failed and terminal.error == "BLOCKED: no consent"
    assert not ok.failed and ok.error is None
    assert not open_.failed and open_.error is None


def test_memory_results_report_the_char_budget():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        # rejected: the store is full, so the whole store comes back for the
        # model to consolidate before its entry will fit
        *scope_lines("W1", "tool", 1_100_000, 1_200_000, name="memory",
                     session="s1", turn="t1", end_status="error",
                     start_data={"action": "add", "target": "user",
                                 "content": "One more thing."},
                     end_data={"success": False, "usage": "1,338/1,375",
                               "error": "Memory at 1,338/1,375 chars. Adding "
                                        "this entry (233 chars) would exceed "
                                        "the limit.",
                               "current_entries": ["Entry one.", "Entry two."]}),
        # the retry that fits — usage is reported on success too, in the
        # tool's other format, so it is shown verbatim
        *scope_lines("W2", "tool", 1_250_000, 1_300_000, name="memory",
                     session="s1", turn="t1", end_status="ok",
                     start_data={"action": "add", "target": "user",
                                 "content": "Short."},
                     end_data={"success": True, "entry_count": 10,
                               "usage": "97% — 1,335/1,375 chars",
                               "message": "Entry added."}),
        *scope_lines("T1", "tool", 1_350_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"command": "ls"},
                     end_data={"usage": "not a memory store",
                               "current_entries": ["nope"]}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    rejected, stored, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert rejected.failed
    assert rejected.memory_stats == [{
        "label": "usage", "value": "1,338/1,375",
        "tooltip": rejected.memory_stats[0]["tooltip"]}]
    assert rejected.memory_current_entries == ["Entry one.", "Entry two."]
    assert [s["value"] for s in stored.memory_stats] == ["97% — 1,335/1,375 chars", "10"]
    # a successful write is not handed the store back
    assert stored.memory_current_entries == []
    # both keys are far too generic to read outside a memory scope
    assert other.memory_stats == [] and other.memory_current_entries == []


# --- recovering the entry a memory write matched --------------------------
# `old_text` is a fragment the tool matches by containment, and the fragment
# is all the log holds. A rejected write hands back the whole store, so a
# turn that consolidates after one — the routine reason to replace at all —
# says what the entry was. See `resolve_memory_entries`.

HOUSEHOLD = ("For household fault-finding, user prefers likely causes ranked "
             "by commonality, visual diagrams, and practical identification "
             "steps.")
CRYPTO = "Crypto: pithy bold alphabetized Pros/Neutral/Cons."


def _listing_span(uuid, start_us, end_us, entries, target="user"):
    """A write rejected for the char budget: it changed nothing and came
    back with the entire store, which is the only listing there is."""
    return scope_lines(uuid, "tool", start_us, end_us, name="memory",
                       session="s1", turn="t1", end_status="error",
                       start_data={"action": "add", "target": target,
                                   "content": "One more thing."},
                       end_data={"success": False, "usage": "1,338/1,375",
                                 "error": "Memory at 1,338/1,375 chars.",
                                 "current_entries": entries})


def _write_span(uuid, start_us, end_us, ops, target="user", success=True):
    return scope_lines(uuid, "tool", start_us, end_us, name="memory",
                       session="s1", turn="t1",
                       end_status="ok" if success else "error",
                       start_data={"target": target, "operations": ops},
                       end_data={"success": success, "entry_count": 12,
                                 "usage": "96% — 1,329/1,375 chars"})


def _memory_turn(*span_lines):
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *[line for group in span_lines for line in group],
        mark_line("hermes.turn.end", 60_000_000, session="s1", turn="t1"),
    ]
    turn = assemble_lines(lines).sessions[0].turns[0]
    resolve_memory_entries(turn)
    return turn


def test_memory_replace_recovers_the_entry_behind_the_fragment():
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO, HOUSEHOLD]),
        _write_span("W2", 5_200_000, 5_300_000, [
            {"action": "replace", "old_text": "For household fault-finding",
             "content": "Household fault-finding: rank common causes."},
            {"action": "add", "content": "Terminology: be precise."},
        ]),
    )
    replace, add = turn.spans[1].memory_ops
    assert replace["old_entry"] == HOUSEHOLD
    # the − side shows the whole entry; the fragment stays available as the
    # payload's own value
    assert replace["old_shown"] == HOUSEHOLD
    assert replace["old_text"] == "For household fault-finding"
    assert replace["old_entry_note"] == (
        "matched entry from the store listing 4 s earlier in this turn "
        "\u00b7 logged as \u201cFor household fault-finding\u201d")
    # an add matched nothing and claims nothing
    assert add["old_entry"] is None and add["old_entry_note"] is None


def test_memory_entry_unresolved_when_no_listing_precedes_it():
    turn = _memory_turn(_write_span("W1", 1_100_000, 1_200_000, [
        {"action": "remove", "old_text": "Crypto"}]))
    op, = turn.spans[0].memory_ops
    # silence, not a guess and not a note: nothing in the turn ever said
    # what the store held
    assert op["old_entry"] is None and op["old_entry_note"] is None
    assert op["old_shown"] == "Crypto"


def test_memory_listing_answers_for_the_call_that_returned_it():
    """A rejected batch is handed the store *and* names the entries it
    failed to write, so its own ops resolve against it."""
    turn = _memory_turn(scope_lines(
        "W1", "tool", 1_100_000, 1_200_000, name="memory", session="s1",
        turn="t1", end_status="error",
        start_data={"target": "user", "operations": [
            {"action": "replace", "old_text": "For household fault-finding",
             "content": "x" * 400}]},
        end_data={"success": False, "usage": "1,700/1,375",
                  "error": "Batch would exceed the limit.",
                  "current_entries": [CRYPTO, HOUSEHOLD]}))
    op, = turn.spans[0].memory_ops
    assert op["old_entry"] == HOUSEHOLD
    assert op["old_entry_note"] == (
        "matched entry from the store listing this call returned "
        "\u00b7 logged as \u201cFor household fault-finding\u201d")


def test_memory_listing_is_dropped_once_a_write_lands():
    """A successful write reports what it cost, never what the store now
    says, so the listing stops describing anything after it."""
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO, HOUSEHOLD]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "remove", "old_text": "Crypto"}]),
        _write_span("W3", 3_000_000, 3_100_000, [
            {"action": "replace", "old_text": "For household fault-finding",
             "content": "Household: rank common causes."}]),
    )
    landed, after = turn.spans[1].memory_ops[0], turn.spans[2].memory_ops[0]
    assert landed["old_entry"] == CRYPTO          # still covered by the listing
    assert after["old_entry"] is None             # past it
    assert after["old_entry_note"] is None
    assert after["old_shown"] == "For household fault-finding"


def test_memory_listing_survives_a_write_that_did_not_land():
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO, HOUSEHOLD]),
        _write_span("W2", 2_000_000, 2_100_000, success=False, ops=[
            {"action": "replace", "old_text": "Crypto", "content": "x" * 400}]),
        _write_span("W3", 3_000_000, 3_100_000, [
            {"action": "remove", "old_text": "For household fault-finding"}]),
    )
    assert turn.spans[2].memory_ops[0]["old_entry"] == HOUSEHOLD


def test_ambiguous_fragment_is_stated_rather_than_picked():
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000,
                      ["Git: commit in logical units.", "Git: stage first."]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "replace", "old_text": "Git",
             "content": "Git: stage, then commit in logical units."}]),
    )
    op, = turn.spans[1].memory_ops
    assert op["old_entry"] is None
    assert op["old_entry_note"] == (
        "2 entries in the store listing 800 ms earlier in this turn "
        "contain this text — not resolved to one")
    assert op["old_shown"] == "Git"


def test_fragment_the_listing_does_not_hold_says_so():
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "remove", "old_text": "Atlas weekly"}]),
    )
    op, = turn.spans[1].memory_ops
    assert op["old_entry"] is None
    assert op["old_entry_note"] == (
        "no entry in the store listing 800 ms earlier in this turn "
        "contains this text")


def test_a_fragment_that_is_the_whole_entry_adds_no_note():
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "remove", "old_text": CRYPTO}]),
    )
    op, = turn.spans[1].memory_ops
    assert op["old_entry"] is None and op["old_entry_note"] is None
    assert op["old_shown"] == CRYPTO


def test_later_ops_in_a_batch_see_what_earlier_ones_left():
    """Within one span the log is complete — a batch is atomic and every op
    is on the span — so the ops are replayed rather than all matched against
    the same starting list."""
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO, HOUSEHOLD]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "replace", "old_text": "Crypto",
             "content": "Crypto: bold alphabetized Pros/Cons."},
            # matches the entry the op above wrote, not the one it replaced
            {"action": "remove", "old_text": "bold alphabetized"},
        ]),
    )
    first_op, second = turn.spans[1].memory_ops
    assert first_op["old_entry"] == CRYPTO
    assert second["old_entry"] == "Crypto: bold alphabetized Pros/Cons."


def test_memory_entries_are_resolved_per_store():
    """The two stores are separate files; a listing of one says nothing
    about the other."""
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [HOUSEHOLD], target="user"),
        _write_span("W2", 2_000_000, 2_100_000, target="memory", ops=[
            {"action": "remove", "old_text": "For household fault-finding"}]),
    )
    assert turn.spans[1].memory_ops[0]["old_entry"] is None


def test_resolving_memory_entries_twice_gives_the_same_answer():
    # the assembly is cached and a live page re-renders it every few seconds
    turn = _memory_turn(
        _listing_span("W1", 1_100_000, 1_200_000, [CRYPTO, HOUSEHOLD]),
        _write_span("W2", 2_000_000, 2_100_000, [
            {"action": "remove", "old_text": "For household"}]),
    )
    before = turn.spans[1].memory_ops
    resolve_memory_entries(turn)
    assert turn.spans[1].memory_ops == before


# --- what a call was for -------------------------------------------------
# hermes stamps `call_role` on every llm span. Its value space is wider than
# the log shows (checked against the emit sites in $HERMES_SOURCE/agent/): primary,
# delegated, fallback, iteration_summary, auxiliary:<task>.


def llm_with_metadata(metadata):
    return [
        *session_scope_lines("s1", start_us=0),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 3_100_000, name="auto",
                     session="s1", turn="t1", end_data={"usage": {}}),
        mark_line("hermes.turn.end", 4_000_000, session="s1", turn="t1"),
    ], metadata


def span_with(metadata):
    lines = [
        *session_scope_lines("s1", start_us=0),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        json.dumps({"kind": "scope", "scope_category": "start", "uuid": "L1",
                    "parent_uuid": SESSION_SCOPE_UUID, "name": "auto",
                    "category": "llm", "timestamp": 1_100_000,
                    "metadata": {"session_id": "s1", "turn_id": "t1", **metadata}}),
        json.dumps({"kind": "scope", "scope_category": "end", "uuid": "L1",
                    "parent_uuid": SESSION_SCOPE_UUID, "name": "auto",
                    "category": "llm", "timestamp": 3_100_000,
                    "metadata": {"session_id": "s1", "turn_id": "t1", **metadata}}),
    ]
    return assemble_lines(lines).sessions[0].turns[0].spans[0]


def test_an_ordinary_call_shows_no_role():
    """`primary` is 216 of the 218 calls in the log; a tag every row carries
    is one no row is read for. Its absence is what says the call was
    ordinary."""
    assert span_with({"call_role": "primary"}).call_role is None
    assert span_with({}).call_role is None


def test_an_auxiliary_call_names_the_task_it_was_for():
    assert span_with({"call_role": "auxiliary:compression",
                      "auxiliary_task": "compression"}).call_role \
        == "auxiliary:compression"


def test_roles_beyond_the_auxiliary_ones_are_shown_too():
    """`auxiliary_task` cannot express these — it is absent on all of them —
    which is why call_role is the field read and it is not."""
    for role in ("delegated", "fallback", "iteration_summary"):
        assert span_with({"call_role": role}).call_role == role


def test_an_unknown_role_is_shown_as_it_arrives():
    """Auxiliary tasks are open-ended: hermes exposes register_auxiliary_task
    to plugins, so this app keeps no list to match against."""
    assert span_with({"call_role": "auxiliary:whatever-comes-next"}).call_role \
        == "auxiliary:whatever-comes-next"


def test_a_first_attempt_shows_no_retry_count():
    assert span_with({"retry_count": 0}).retry_count is None
    assert span_with({}).retry_count is None


def test_a_retried_call_says_which_attempt_answered():
    assert span_with({"retry_count": 2}).retry_count == 2


def test_retry_count_type_guards_an_odd_payload():
    assert span_with({"retry_count": "2"}).retry_count is None
    assert span_with({"retry_count": True}).retry_count is None


# --- a whole request and a whole response (ADR 12) ------------------------
#
# The five kinds of message in `annotated_request` come from the relay's
# annotator, not from hermes, so there is no tool signature to check them
# against (design principle 3) — these fixtures are the shapes the log
# holds, and everything here is written to survive shapes it does not.

def llm_span(profile=None, end_data=None):
    """One llm span, straight from a scope pair, for the payload readers."""
    lines = scope_lines("L1", "llm", 1_000, 2_000, name="openai-codex",
                        session="s1", turn="t1", profile=profile,
                        end_data=end_data)
    assembly = assemble_lines([
        mark_line("hermes.turn.start", 500, session="s1", turn="t1"),
        *lines,
        mark_line("hermes.turn.end", 3_000, session="s1", turn="t1")])
    span, = assembly.sessions[0].turns[0].spans
    return span


def labels(span):
    return [s["label"] for s in span.llm_request_messages]


def bodies(span):
    return [s["text"] for s in span.llm_request_messages]


def test_a_whole_request_keeps_every_message_in_the_order_sent():
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"}]}})
    assert labels(span) == ["user", "assistant", "user"]
    assert bodies(span) == ["first", "second", "third"]


def test_a_whole_request_leads_with_the_system_instructions():
    """`instructions` is the system prompt on the openai_responses path and
    sits beside `messages` rather than inside it, so a reader who only knew
    about `messages` would never see it."""
    span = llm_span(profile={"annotated_request": {
        "instructions": "You are Hermes Agent.",
        "messages": [{"role": "user", "content": "hello"}]}})
    assert labels(span) == ["instructions", "user"]
    assert bodies(span) == ["You are Hermes Agent.", "hello"]


def test_a_label_is_never_written_into_the_message_it_labels():
    """The whole reason this is a list of sections: a `## user` written into
    the markdown is one more heading among the model's own — a system prompt
    has a dozen — and a reader cannot tell whose words are whose."""
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "user", "content": "## My own heading\n\ntext"}]}})
    section, = span.llm_request_messages
    assert section["label"] == "user"
    assert section["text"] == "## My own heading\n\ntext"   # untouched


def test_a_tool_call_names_the_tool_in_its_label():
    """A call and its result are otherwise two identical labels with the
    interesting part inside the fence."""
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "tool_call", "name": "read_file", "call_id": "c1",
         "arguments": '{"path": "/tmp/x"}'},
        {"role": "tool_result", "call_id": "c1", "output": "file contents"}]}})
    assert labels(span) == ["tool_call · read_file", "tool_result"]
    assert '{"path": "/tmp/x"}' in bodies(span)[0]
    assert bodies(span)[1] == "file contents"


# --- results are regrouped under their calls ------------------------------
# Not the wire order, and the page says so (the `prompt` Full's note). The
# wire sends every call and then every result — 957 of 957 such requests in
# the log — so the results arrive as a block with nothing tying them to the
# calls above.

def _blocked_request(*pairs):
    """A request shaped the way the wire sends one: all calls, then all
    results."""
    calls = [{"role": "tool_call", "name": n, "call_id": c,
              "arguments": "{}"} for c, n in pairs]
    results = [{"role": "tool_result", "call_id": c, "output": f"out {c}"}
               for c, _ in pairs]
    return {"annotated_request": {"messages": [
        {"role": "user", "content": "go"}, *calls, *results]}}


def test_each_tool_result_is_moved_under_the_call_it_answers():
    span = llm_span(profile=_blocked_request(
        ("c1", "skill_view"), ("c2", "read_file"), ("c3", "read_file")))
    assert labels(span) == [
        "user",
        "tool_call · skill_view", "tool_result",
        "tool_call · read_file", "tool_result",
        "tool_call · read_file", "tool_result",
    ]


def test_two_calls_of_one_tool_keep_their_own_results():
    """The name does not identify a call — 878 of the 957 requests in the log
    that carry results call some tool more than once — so the *position* has
    to be right: nothing in the labels distinguishes these two."""
    span = llm_span(profile=_blocked_request(("c1", "read_file"),
                                             ("c2", "read_file")))
    got = bodies(span)
    assert [got[0], got[2], got[4]] == ["go", "out c1", "out c2"]
    assert "{}" in got[1] and "{}" in got[3]      # the calls' arguments


def test_both_halves_of_a_pair_are_marked_so_the_page_can_join_them():
    """The card drawn around the two is the only thing on screen that says
    they are a pair — the labels no longer say it."""
    span = llm_span(profile=_blocked_request(("c1", "read_file")))
    sections = span.llm_request_messages
    # the call knows it runs into what follows; the result knows it is inside
    assert [s.get("nests") for s in sections] == [None, True, None]
    assert [s.get("nested") for s in sections] == [None, None, True]


def test_a_result_matching_no_call_stays_where_it_arrived():
    """Never seen in the log — 26,944 results, none orphaned — but inventing
    a position for one would be the reordering doing harm. A reader seeing an
    ungrouped result should be able to trust that it really was unpaired."""
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "tool_call", "name": "read_file", "call_id": "c1",
         "arguments": "{}"},
        {"role": "tool_result", "call_id": "nothing-matches", "output": "?"},
        {"role": "tool_result", "call_id": "c1", "output": "ok"}]}})
    assert labels(span) == ["tool_call · read_file",
                            "tool_result",     # regrouped, under its call
                            "tool_result"]     # orphan, left where it was
    assert [s.get("nested") for s in span.llm_request_messages] == [
        None, True, None]                      # …and not drawn inside a card
    assert bodies(span)[2] == "?"


def test_messages_that_are_not_tool_traffic_keep_the_order_sent():
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"}]}})
    assert bodies(span) == ["first", "second", "third"]


def test_a_provider_native_message_is_named_by_its_kind():
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "provider_native", "kind": "reasoning", "provider": "openai",
         "value": {"summary": []}}]}})
    assert labels(span) == ["provider_native · reasoning"]


def test_a_structured_message_body_is_fenced_json_not_a_python_repr():
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "provider_native", "value": {"a": 1}}]}})
    body, = bodies(span)
    assert '"a": 1' in body and "'a'" not in body
    assert "```json" in body


def test_a_fence_survives_a_body_that_contains_one():
    """Tool results are routinely markdown with their own fences; a
    three-backtick fence around one ends at the first of them."""
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "provider_native", "value": {"md": "```\ncode\n```"}}]}})
    assert "````json" in bodies(span)[0]      # longer than anything inside it


def test_a_message_shape_this_app_has_never_seen_is_kept_whole():
    """Nothing is dropped for being unrecognized: the point of the page this
    feeds is that it holds everything."""
    span = llm_span(profile={"annotated_request": {"messages": [
        {"role": "future_thing", "surprising_key": {"deep": "value"}}]}})
    assert labels(span) == ["future_thing"]
    assert "surprising_key" in bodies(span)[0] and "deep" in bodies(span)[0]


def test_a_message_that_is_not_a_dict_keeps_a_section_of_its_own():
    span = llm_span(profile={"annotated_request": {
        "messages": ["just a string", 42]}})
    assert labels(span) == ["(unreadable)", "(unreadable)"]
    assert "just a string" in bodies(span)[0]
    assert "42" in bodies(span)[1]


def test_a_request_that_is_not_there_reads_as_nothing():
    for profile in (None, {}, {"annotated_request": "not a dict"},
                    {"annotated_request": {"messages": "not a list"}}):
        assert llm_span(profile=profile).llm_request_messages is None


def test_a_whole_response_is_the_string_the_excerpt_started():
    """An excerpt that is not a prefix of what its link opens would be a
    quiet lie, so both read the same key of the same payload."""
    text = "The answer is 42. " * 40
    span = llm_span(end_data={"assistant_message": {"role": "assistant",
                                                    "content": text}})
    assert span.llm_response_text == text
    assert span.llm_text["text"] == text[:400]
    assert span.llm_response_text.startswith(span.llm_text["text"])


def test_a_response_with_no_text_reads_as_nothing():
    """A call that only asked for tools said nothing, so there is no whole
    response to open — and no icon offering one."""
    for end in (None, {}, {"assistant_message": {"content": ""}},
                {"assistant_message": {"content": ["not", "a", "string"]}}):
        assert llm_span(end_data=end).llm_response_text is None


def test_the_asked_excerpt_never_carries_the_whole_prompt():
    """A compaction's instruction is 26 KB. None of it belongs on a row —
    not in the text and not in a title attribute, which would ride every
    2 s poll of a live turn."""
    prompt = "summarise this. " * 4000
    span = llm_span(profile={"annotated_request": {
        "messages": [{"role": "user", "content": prompt}]}})
    excerpt = span.request_prompt_excerpt
    assert len(excerpt["text"]) == 400
    assert excerpt["truncated"] is True
    assert prompt.strip() not in str(excerpt)      # none of the rest of it


def test_the_two_excerpts_on_a_span_row_cut_at_the_same_place():
    """They sit on adjacent rows; two different cuts would read as a
    difference in the values rather than in the code."""
    long = "x" * 5000
    span = llm_span(
        profile={"annotated_request": {
            "messages": [{"role": "user", "content": long}]}},
        end_data={"assistant_message": {"content": long}})
    assert (len(span.request_prompt_excerpt["text"])
            == len(span.llm_text["text"]))


def test_a_call_with_no_request_has_no_asked_excerpt():
    assert llm_span(profile={}).request_prompt_excerpt is None
