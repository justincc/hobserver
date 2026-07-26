"""Assembler tests: spans → turns → waterfall, fed via the real parser.

Streams mirror what the hermes-agent nemo_relay plugin emits: an agent
session scope, hermes.turn.start/end marks, llm and tool child scopes with
correlation metadata.
"""

import json

from plugins.timing.assembler import UNKNOWN_SESSION, assemble
from plugins.timing.atof_reader import parse_lines

SESSION_SCOPE_UUID = "scope-s1"


def _metadata(session=None, turn=None):
    metadata = {}
    if session:
        metadata["session_id"] = session
    if turn:
        metadata["turn_id"] = turn
    return metadata or None


def scope_lines(uuid, category, start_us, end_us=None, *, name="span", session=None,
                turn=None, parent=SESSION_SCOPE_UUID, profile=None,
                start_data=None, end_data=None):
    common = {
        "kind": "scope", "atof_version": "0.1", "uuid": uuid, "parent_uuid": parent,
        "name": name, "category": category, "category_profile": profile,
        "metadata": _metadata(session, turn),
    }
    lines = [json.dumps({**common, "scope_category": "start",
                         "timestamp": start_us, "data": start_data})]
    if end_us is not None:
        lines.append(json.dumps({**common, "scope_category": "end",
                                 "timestamp": end_us, "data": end_data}))
    return lines


def mark_line(name, us, *, session=None, turn=None, parent=SESSION_SCOPE_UUID, data=None):
    return json.dumps({
        "kind": "mark", "atof_version": "0.1", "uuid": f"mark-{name}-{us}",
        "parent_uuid": parent, "timestamp": us, "name": name, "data": data,
        "metadata": _metadata(session, turn),
    })


def session_scope_lines(session="s1", start_us=0, end_us=None):
    return scope_lines(SESSION_SCOPE_UUID, "agent", start_us, end_us,
                       name=f"hermes-session-{session}", session=session,
                       parent=None, start_data={"session_id": session})


def assemble_lines(lines):
    events, errors = parse_lines(lines)
    assert not errors, errors
    return assemble(events)


def two_turn_stream():
    return [
        *session_scope_lines("s1", start_us=0),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 3_100_000, name="anthropic",
                     session="s1", turn="t1",
                     profile={"model_name": "claude-sonnet-4-6"},
                     end_data={"usage": {"input_tokens": 100, "output_tokens": 50},
                               "finish_reason": "tool_calls"}),
        *scope_lines("T1", "tool", 3_200_000, 3_700_000, name="terminal",
                     session="s1", turn="t1",
                     profile={"tool_call_id": "call-1"},
                     start_data={"command": "git status --short",
                                 "workdir": "/home/u/proj", "timeout": 120},
                     end_data={"status": "ok", "duration_ms": 500}),
        *scope_lines("L2", "llm", 4_000_000, 6_000_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.turn.end", 6_500_000, session="s1", turn="t1"),
        mark_line("hermes.turn.start", 10_000_000, session="s1", turn="t2"),
        *scope_lines("L3", "llm", 10_100_000, 11_100_000, name="anthropic",
                     session="s1", turn="t2"),
        # no turn end: turn 2 is in flight
    ]


def test_waterfall_numbers_for_a_complete_turn():
    assembly = assemble_lines(two_turn_stream())
    assert not assembly.anomalies
    (session,) = assembly.sessions
    assert session.session_id == "s1"
    turn = session.turns[0]
    assert turn.turn_id == "t1"
    assert turn.duration_us == 5_500_000
    assert turn.llm_us == 4_000_000
    assert turn.tool_us == 500_000
    assert turn.overhead_us == 1_000_000       # the residual we set out to find
    assert turn.model_call_count == 2
    assert [s.uuid for s in turn.spans] == ["L1", "T1", "L2"]


def test_in_flight_turn_has_no_duration_but_keeps_spans():
    assembly = assemble_lines(two_turn_stream())
    turn = assembly.sessions[0].turns[1]
    assert turn.turn_id == "t2"
    assert turn.end_us is None
    assert turn.duration_us is None
    assert turn.overhead_us is None
    assert turn.llm_us == 1_000_000
    assert [s.uuid for s in turn.spans] == ["L3"]


def test_span_details_survive_assembly():
    assembly = assemble_lines(two_turn_stream())
    llm, tool = assembly.sessions[0].turns[0].spans[0:2]
    assert llm.model_name == "claude-sonnet-4-6"
    assert llm.usage == {"input_tokens": 100, "output_tokens": 50}
    assert llm.duration_us == 2_000_000
    assert tool.tool_call_id == "call-1"
    assert tool.end_data["duration_ms"] == 500
    assert llm.finish_reason == "tool_calls"
    assert tool.command == "git status --short"
    assert tool.workdir == "/home/u/proj"
    assert llm.command is None and llm.workdir is None


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
        # carries no V4A text is one (see $h/tools/file_tools.py patch_tool)
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


def test_mem0_add_content_from_start_payload():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, 1_200_000, name="mem0_add",
                     session="s1", turn="t1",
                     start_data={"content": "User prefers tea over coffee."}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"content": "not-a-fact", "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    mem, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert mem.memory_content == "User prefers tea over coffee."
    assert mem.memory_id is None            # an add names no existing memory
    # the generic "content" key means nothing outside a mem0_add scope
    assert other.memory_content is None


def test_mem0_update_and_delete_name_the_memory():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        # an update replaces the text of one memory: "text", not "content"
        *scope_lines("M1", "tool", 1_100_000, 1_200_000, name="mem0_update",
                     session="s1", turn="t1",
                     start_data={"memory_id": "fdd806c1-0789-4522-aaf3",
                                 "text": "User prefers coffee after all."}),
        *scope_lines("M2", "tool", 1_250_000, 1_280_000, name="mem0_delete",
                     session="s1", turn="t1",
                     start_data={"memory_id": "b3e3ade6-d852-44b2-98f4"}),
        *scope_lines("T1", "tool", 1_300_000, 1_400_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"memory_id": "not-a-memory", "text": "nope",
                                 "command": "ls"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    update, delete, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert update.memory_id == "fdd806c1-0789-4522-aaf3"
    assert update.memory_content == "User prefers coffee after all."
    # a delete carries the id alone — that is its whole payload
    assert delete.memory_id == "b3e3ade6-d852-44b2-98f4"
    assert delete.memory_content is None
    # the generic "memory_id"/"text" keys mean nothing outside a mem0 scope
    assert other.memory_id is None and other.memory_content is None


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


def test_subagent_stops_pair_with_starts_by_child_session_id():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.subagent.start", 1_100_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Luma", "child_session_id": "c1"}),
        mark_line("hermes.subagent.start", 1_200_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Meetup", "child_session_id": "c2"}),
        mark_line("hermes.approval.request", 1_500_000, session="s1", turn="t1"),
        # stops arrive in the opposite order to the starts
        mark_line("hermes.subagent.stop", 1_800_000, session="s1", turn="t1",
                  data={"child_session_id": "c2", "child_status": "ok",
                        "duration_ms": 600}),
        mark_line("hermes.subagent.stop", 1_900_000, session="s1", turn="t1",
                  data={"child_session_id": "c1", "child_status": "timeout",
                        "duration_ms": 800}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    turn = assemble_lines(lines).sessions[0].turns[0]
    marks = {(m.name, m.child_session_id): m for m in turn.marks}
    stop1 = marks[("hermes.subagent.stop", "c1")]
    stop2 = marks[("hermes.subagent.stop", "c2")]
    # ordinals follow start order regardless of stop order, and a stop
    # resolves its start's goal
    assert turn.subagent_ordinal(stop1) == 1
    assert turn.subagent_ordinal(stop2) == 2
    assert turn.subagent_goal(stop1) == "Sweep Luma"
    assert turn.subagent_goal(stop2) == "Sweep Meetup"
    assert stop1.child_status == "timeout"
    assert stop1.child_duration_ms == 800
    # non-subagent marks carry no tag
    approval = marks[("hermes.approval.request", None)]
    assert turn.subagent_ordinal(approval) is None


def test_finished_subagent_sessions_come_from_stop_marks():
    # a subagent whose own session never emits turn.end: the parent's stop
    # mark is what tells us it finished. c2 stopped, c1 is still running.
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.subagent.start", 1_100_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Luma", "child_session_id": "c1"}),
        mark_line("hermes.subagent.start", 1_200_000, session="s1", turn="t1",
                  data={"child_goal": "Sweep Meetup", "child_session_id": "c2"}),
        mark_line("hermes.subagent.stop", 1_800_000, session="s1", turn="t1",
                  data={"child_session_id": "c2", "child_status": "ok"}),
        # both children ran turns that never closed
        mark_line("hermes.turn.start", 1_150_000, session="c1", turn="ct1"),
        mark_line("hermes.turn.start", 1_250_000, session="c2", turn="ct2"),
    ]
    assert assemble_lines(lines).finished_subagent_sessions == {"c2"}


def test_finished_subagent_sessions_is_empty_without_stops():
    assert assemble_lines(two_turn_stream()).finished_subagent_sessions == set()


def test_a_later_turn_supersedes_an_unclosed_one():
    # t1 never gets its end mark; t2 starting proves it is over, since a
    # session runs one turn at a time
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000, session="s1", turn="t1"),
        mark_line("hermes.turn.start", 3_000_000, session="s1", turn="t2"),
        *scope_lines("L2", "llm", 3_100_000, 4_000_000, session="s1", turn="t2"),
    ]
    t1, t2 = assemble_lines(lines).sessions[0].turns
    assert t1.superseded and not t1.is_live
    # no end time is invented for it: the duration was never observed
    assert t1.end_us is None
    assert t1.duration_us is None
    # the newest turn is genuinely still running
    assert t2.is_live and not t2.superseded


def test_a_cleanly_ended_turn_is_never_superseded():
    turns = assemble_lines(two_turn_stream()).sessions[0].turns
    assert not any(t.superseded for t in turns)
    # two_turn_stream's second turn is open and last, so it stays live
    assert turns[0].end_us is not None and not turns[0].is_live
    assert turns[1].is_live


def test_timeline_interleaves_marks_with_spans_in_time_order():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000, name="anthropic",
                     session="s1", turn="t1"),
        mark_line("hermes.approval.request", 1_500_000, session="s1", turn="t1"),
        *scope_lines("T1", "tool", 1_600_000, 1_900_000, name="terminal",
                     session="s1", turn="t1"),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    turn = assemble_lines(lines).sessions[0].turns[0]
    assert [(e.is_mark, e.name) for e in turn.timeline] == [
        (False, "anthropic"),
        (True, "hermes.approval.request"),
        (False, "terminal"),
    ]


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


def test_session_activity_window_spans_all_events():
    assembly = assemble_lines(two_turn_stream())
    session = assembly.sessions[0]
    assert session.first_us == 0               # session scope start
    assert session.last_us == 11_100_000       # last span end


def test_open_span_is_flagged_and_excluded_from_sums():
    assembly = assemble_lines(two_turn_stream())
    open_spans = [s for t in assembly.sessions[0].turns for s in t.spans if s.is_open]
    assert open_spans == []
    lines = two_turn_stream() + scope_lines(
        "L4", "llm", 11_200_000, None, name="anthropic", session="s1", turn="t2")
    assembly = assemble_lines(lines)
    turn = assembly.sessions[0].turns[1]
    assert turn.llm_us == 1_000_000            # open span contributes nothing
    assert [s.uuid for s in turn.spans if s.is_open] == ["L4"]


def test_span_with_turn_id_matches_even_outside_turn_interval():
    # a span that started just before its turn.start mark (plugin races)
    lines = [
        *session_scope_lines("s1"),
        *scope_lines("L1", "llm", 900_000, 2_000_000, session="s1", turn="t1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 3_000_000, session="s1", turn="t1"),
    ]
    assembly = assemble_lines(lines)
    assert not assembly.anomalies
    assert [s.uuid for s in assembly.sessions[0].turns[0].spans] == ["L1"]


def test_span_without_matching_turn_id_falls_back_to_containment():
    # turn marks carry no turn_id (pre_llm_call kwargs may lack one)
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1"),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000, session="s1", turn="t9"),
        mark_line("hermes.turn.end", 3_000_000, session="s1"),
    ]
    assembly = assemble_lines(lines)
    assert not assembly.anomalies
    turn = assembly.sessions[0].turns[0]
    assert turn.turn_id is None
    assert [s.uuid for s in turn.spans] == ["L1"]


def test_session_resolution_falls_back_to_parent_uuid():
    # child events with no metadata at all still group via the session scope
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000),
        mark_line("hermes.turn.end", 3_000_000),
    ]
    assembly = assemble_lines(lines)
    assert not assembly.anomalies
    (session,) = assembly.sessions
    assert session.session_id == "s1"
    assert [s.uuid for s in session.turns[0].spans] == ["L1"]


def test_events_with_no_session_at_all_land_in_unknown_bucket():
    lines = scope_lines("X1", "llm", 1_000_000, 2_000_000, parent=None)
    events, errors = parse_lines(lines)
    assert not errors
    assembly = assemble(events)
    (session,) = assembly.sessions
    assert session.session_id == UNKNOWN_SESSION
    assert [s.uuid for s in session.unassigned_spans] == ["X1"]
    assert any("outside every turn" in a.message for a in assembly.anomalies)


def test_non_boundary_marks_attach_to_their_turn():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        mark_line("hermes.approval.request", 1_500_000, session="s1", turn="t1"),
        mark_line("hermes.turn.end", 3_000_000, session="s1", turn="t1"),
        mark_line("hermes.session.end", 9_000_000, session="s1"),
    ]
    assembly = assemble_lines(lines)
    session = assembly.sessions[0]
    assert [m.name for m in session.turns[0].marks] == ["hermes.approval.request"]
    assert [m.name for m in session.unassigned_marks] == ["hermes.session.end"]


def test_scope_end_without_start_is_an_anomaly():
    lines = [
        *session_scope_lines("s1"),
        json.dumps({"kind": "scope", "scope_category": "end", "uuid": "ghost",
                    "name": "anthropic", "category": "llm", "timestamp": 2_000_000,
                    "parent_uuid": SESSION_SCOPE_UUID}),
    ]
    events, errors = parse_lines(lines)
    assert not errors
    assembly = assemble(events)
    assert any("end without start" in a.message for a in assembly.anomalies)


def test_turn_end_without_start_is_an_anomaly():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.end", 1_000_000, session="s1"),
    ]
    events, _ = parse_lines(lines)
    assembly = assemble(events)
    assert any("turn end without turn start" in a.message for a in assembly.anomalies)
    assert assembly.sessions[0].turns == []


def test_unended_turn_interval_stops_at_next_turn_start():
    # first turn never ends (crash); its spans must not leak into turn 2
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1"),
        *scope_lines("L1", "llm", 1_100_000, 2_000_000, session="s1"),
        mark_line("hermes.turn.start", 5_000_000, session="s1"),
        *scope_lines("L2", "llm", 5_100_000, 6_000_000, session="s1"),
        mark_line("hermes.turn.end", 7_000_000, session="s1"),
    ]
    events, _ = parse_lines(lines)
    assembly = assemble(events)
    assert any("before previous turn ended" in a.message for a in assembly.anomalies)
    session = assembly.sessions[0]
    assert len(session.turns) == 2
    assert [s.uuid for s in session.turns[0].spans] == ["L1"]
    assert [s.uuid for s in session.turns[1].spans] == ["L2"]
    assert session.turns[0].end_us is None


def test_sessions_sorted_by_most_recent_activity():
    lines = [
        *scope_lines("A1", "llm", 1_000_000, 2_000_000, session="old", parent=None),
        *scope_lines("B1", "llm", 1_000_000, 9_000_000, session="busy", parent=None),
    ]
    events, _ = parse_lines(lines)
    assembly = assemble(events)
    assert [s.session_id for s in assembly.sessions] == ["busy", "old"]


def test_out_of_order_lines_assemble_identically():
    lines = two_turn_stream()
    events, _ = parse_lines(lines)
    reordered = list(reversed(events))
    turn = assemble(reordered).sessions[0].turns[0]
    assert turn.duration_us == 5_500_000
    assert turn.overhead_us == 1_000_000
    assert [s.uuid for s in turn.spans] == ["L1", "T1", "L2"]


def test_turn_last_activity_us_tracks_latest_event():
    assembly = assemble_lines(two_turn_stream())
    t1, t2 = assembly.sessions[0].turns
    assert t1.last_activity_us == 6_500_000     # its own end mark
    assert t2.last_activity_us == 11_100_000    # in flight: last span edge


def test_turn_user_message_from_start_mark_data():
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1",
                  data={"user_message": "fix the tests", "platform": "webui"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
        # real streams may also carry data as a raw JSON string
        mark_line("hermes.turn.start", 3_000_000, session="s1", turn="t2",
                  data=json.dumps({"user_message": "second prompt"})),
    ]
    t1, t2 = assemble_lines(lines).sessions[0].turns
    assert t1.user_message == "fix the tests"
    assert t2.user_message == "second prompt"
    # marks with no payload (the shared fixture) yield None, never a crash
    plain = assemble_lines(two_turn_stream()).sessions[0].turns[0]
    assert plain.user_message is None


def test_memory_scope_single_op_shape():
    """The `memory` tool's single-op shape — action plus the entry text.
    A different tool from the mem0 scopes above ($h/tools/memory_tool.py)."""
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
                                  "content": "One entry.", "text": "One entry."}]
