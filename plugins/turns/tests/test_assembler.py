"""Assembler tests: spans → turns → waterfall, fed via the real parser.

Streams mirror what the hermes-agent nemo_relay plugin emits: an agent
session scope, hermes.turn.start/end marks, llm and tool child scopes with
correlation metadata.
"""

import json

from plugins.turns.assembler import UNKNOWN_SESSION, assemble
from plugins.turns.atof_reader import parse_lines

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
                start_data=None, end_data=None, end_status=None):
    """A scope's start (and end) lines. end_status stamps metadata.status,
    which hermes sets to "ok" or "error" on the end event only."""
    common = {
        "kind": "scope", "atof_version": "0.1", "uuid": uuid, "parent_uuid": parent,
        "name": name, "category": category, "category_profile": profile,
        "metadata": _metadata(session, turn),
    }
    lines = [json.dumps({**common, "scope_category": "start",
                         "timestamp": start_us, "data": start_data})]
    if end_us is not None:
        end_metadata = {**(common["metadata"] or {})}
        if end_status:
            end_metadata["status"] = end_status
        lines.append(json.dumps({**common, "scope_category": "end",
                                 "timestamp": end_us, "data": end_data,
                                 "metadata": end_metadata or None}))
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
                     end_data={"usage": {"prompt_tokens": 120,
                                         "cache_read_tokens": 20,
                                         "input_tokens": 100,
                                         "output_tokens": 50},
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
    assert llm.usage == {"prompt_tokens": 120, "cache_read_tokens": 20,
                         "input_tokens": 100, "output_tokens": 50}
    assert llm.duration_us == 2_000_000
    assert tool.tool_call_id == "call-1"
    assert tool.end_data["duration_ms"] == 500
    assert llm.finish_reason == "tool_calls"
    assert tool.command == "git status --short"
    assert tool.workdir == "/home/u/proj"
    assert llm.command is None and llm.workdir is None


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


def test_mem0_search_results_from_end_payload():
    # the log carries the tool result as a JSON *string*, ranked by score
    span = _one_span(
        "mem0_search", {"query": "job preferences", "top_k": 10},
        '{"count": 3, "results":'
        ' [{"id": "b760576d", "memory": "top fact", "score": 0.8042},'
        '  {"id": "f9c1f7ee", "memory": "next fact", "score": 0.5339},'
        '  {"id": "fb54073a", "memory": "third fact", "score": 0.4229}]}')
    assert [r["id"] for r in span.mem0_results] == ["b760576d", "f9c1f7ee",
                                                   "fb54073a"]
    assert span.mem0_results[0]["memory"] == "top fact"
    assert span.mem0_results[0]["score"] == 0.8042
    assert span.mem0_result_count == 3


def test_mem0_search_results_only_on_mem0_search_scopes():
    # "results" is far too generic a key to trust on another scope
    span = _one_span("session_search", {"query": "q"},
                     '{"count": 1, "results": [{"memory": "not mem0"}]}')
    assert span.mem0_results == []
    assert span.mem0_result_count is None


def test_mem0_search_results_read_defensively():
    # payloads are opaque per the ATOF spec: a missing key drops to None and
    # a non-dict entry is skipped, rather than 500ing the turn page
    span = _one_span("mem0_search", {"query": "q"},
                     '{"results": [{"memory": "no id, no score"},'
                     ' "not a dict", {"id": "abc"}]}')
    assert span.mem0_results == [
        {"id": None, "memory": "no id, no score", "score": None},
        {"id": "abc", "memory": None, "score": None},
    ]
    # no count in the payload, so it falls back to the list length
    assert span.mem0_result_count == 2


def test_mem0_search_still_open_has_no_results():
    lines = [
        *session_scope_lines("s1"),
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("M1", "tool", 1_100_000, name="mem0_search",
                     session="s1", turn="t1", start_data={"query": "q"}),
    ]
    span = assemble_lines(lines).sessions[0].turns[0].spans[0]
    assert span.is_open and span.mem0_results == []
    assert span.mem0_result_count is None


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


# --- the core runtime's turn tree ----------------------------------------
# From hermes 2026-07-19 there are no hermes.turn.* marks. A turn is a scope
# with its work nested under it by parent_uuid:
#
#   agent > hermes.turn > hermes.logical_llm_call > llm
#                       > tool
#
# These streams carry no session_id and no telemetry_schema_version, exactly
# as the core runtime writes them: the session is recovered from the
# composite turn_id on tool spans and from the request headers on llm spans.

RELAY_AGENT = "relay-agent"
RELAY_TURN = "relay-turn-1"
RELAY_SESSION = "eb8e54f7a700"
RELAY_TURN_ID = f"{RELAY_SESSION}:{RELAY_SESSION}:3239e837"


def relay_request(prompt):
    return {"annotated_request": {
        "extra_headers": {"session_id": RELAY_SESSION},
        "messages": [{"role": "user", "content": prompt}]}}


def relay_llm_lines(uuid, start_us, end_us, *, parent, prompt=None, wrapper=None):
    """A logical_llm_call wrapper and the llm scope inside it."""
    lines = []
    if wrapper:
        lines += scope_lines(wrapper, "function", start_us - 1_000, end_us + 1_000,
                             name="hermes.logical_llm_call", parent=parent,
                             end_data={"outcome": "success"})
        parent = wrapper
    profile = relay_request(prompt) if prompt else {}
    profile["model_name"] = "gpt-5.6-sol"
    lines += scope_lines(uuid, "llm", start_us, end_us, name="openai-codex",
                         parent=parent, profile=profile,
                         end_data={"output": [], "usage": {}})
    return lines


def relay_stream(*, turn_end=6_000_000, prompt="please produce a jobs report"):
    return [
        *scope_lines(RELAY_AGENT, "agent", 0, name="hermes-session", parent=None),
        *scope_lines(RELAY_TURN, "function", 1_000_000, turn_end,
                     name="hermes.turn", parent=RELAY_AGENT,
                     end_data={"outcome": "success"}),
        *relay_llm_lines("RL1", 1_100_000, 3_100_000,
                         parent=RELAY_TURN, wrapper="RW1", prompt=prompt),
        *scope_lines("RT1", "tool", 3_200_000, 3_700_000, name="terminal",
                     parent=RELAY_TURN, turn=RELAY_TURN_ID,
                     start_data={"command": "ls"}),
    ]


def test_a_turn_scope_becomes_a_turn_with_an_observed_duration():
    """The extent is read off the scope pair, not inferred from its spans —
    which is what makes the overhead residual mean something again."""
    session = assemble_lines(relay_stream()).sessions[0]
    assert session.session_id == RELAY_SESSION
    turn, = session.turns
    assert turn.start_us == 1_000_000
    assert turn.end_us == 6_000_000
    assert turn.duration_us == 5_000_000


def test_the_containers_never_appear_as_spans():
    """hermes.turn and hermes.logical_llm_call wrap work rather than being
    it; left in, a turn double-counts the model time they enclose."""
    turn, = assemble_lines(relay_stream()).sessions[0].turns
    assert sorted(s.name for s in turn.spans) == ["openai-codex", "terminal"]


def test_model_time_is_not_double_counted_through_the_wrapper():
    turn, = assemble_lines(relay_stream()).sessions[0].turns
    assert turn.llm_us == 2_000_000          # the llm scope alone
    assert turn.tool_us == 500_000
    assert turn.overhead_us == 5_000_000 - 2_000_000 - 500_000


def test_spans_reach_their_turn_through_the_parent_chain():
    """The llm scope is two hops below its turn, the tool scope one."""
    turn, = assemble_lines(relay_stream()).sessions[0].turns
    llm = next(s for s in turn.spans if s.category == "llm")
    tool = next(s for s in turn.spans if s.category == "tool")
    assert llm.parent_uuid == "RW1"          # the wrapper, not the turn
    assert tool.parent_uuid == RELAY_TURN


def test_a_turn_takes_its_session_from_the_spans_beneath_it():
    """The turn scope carries no session of its own, and the agent scope
    above it has an empty payload."""
    turn, = assemble_lines(relay_stream()).sessions[0].turns
    assert turn.session_id == RELAY_SESSION
    assert all(s.session_id == RELAY_SESSION for s in turn.spans)


def test_a_turn_takes_its_prompt_from_its_first_llm_request():
    turn, = assemble_lines(relay_stream()).sessions[0].turns
    assert turn.user_message == "please produce a jobs report"


def test_the_prompt_is_unwrapped_of_hermes_own_envelope():
    """The turn mark used to carry the bare prompt; the wire message it has
    to be read from now is wrapped, and the two known wrappers come off."""
    wrapped = ("[Workspace::v1: /home/u/workspace]\n"
               "please produce a jobs report\n\n"
               "<memory-context>\n[System note: recalled memory]\n</memory-context>")
    turn, = assemble_lines(relay_stream(prompt=wrapped)).sessions[0].turns
    assert turn.user_message == "please produce a jobs report"


def test_an_unrecognized_wrapping_leaves_the_prompt_whole():
    """Cutting at a guess would be this app inventing a prompt boundary."""
    prompt = "Review the conversation above and update the skill library."
    turn, = assemble_lines(relay_stream(prompt=prompt)).sessions[0].turns
    assert turn.user_message == prompt


def test_an_open_turn_scope_is_live():
    turn, = assemble_lines(relay_stream(turn_end=None)).sessions[0].turns
    assert turn.end_us is None
    assert turn.duration_us is None      # never observed, never guessed
    assert turn.overhead_us is None
    assert turn.is_live


def test_an_open_turn_with_a_later_turn_behind_it_is_over():
    lines = [
        *relay_stream(turn_end=None),
        *scope_lines("relay-turn-2", "function", 7_000_000, 9_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
        *relay_llm_lines("RL2", 7_100_000, 8_000_000, parent="relay-turn-2",
                         wrapper="RW2", prompt="and again"),
    ]
    first, second = assemble_lines(lines).sessions[0].turns
    assert first.end_us is None and first.superseded and not first.is_live
    assert second.is_live is False or second.end_us is not None


def test_a_span_outside_every_turn_scope_is_surfaced_not_absorbed():
    """An llm call parented straight to the session scope belongs to no
    turn, and saying so is better than filing it under a neighbour."""
    lines = [
        *relay_stream(),
        *relay_llm_lines("RL9", 20_000_000, 20_500_000, parent=RELAY_AGENT,
                         prompt="orphan"),
    ]
    assembly = assemble_lines(lines)
    orphans = [s for sess in assembly.sessions for s in sess.unassigned_spans]
    assert [s.uuid for s in orphans] == ["RL9"]
    assert any("falls outside every turn" in a.message for a in assembly.anomalies)


def test_both_eras_assemble_into_one_session_in_time_order():
    """A log spanning the changeover holds mark-bounded turns and
    scope-bounded ones, and they are the same session's turns."""
    lines = [
        *session_scope_lines(RELAY_SESSION, start_us=0),
        mark_line("hermes.turn.start", 100_000, session=RELAY_SESSION, turn="t-old"),
        *scope_lines("OLD1", "llm", 110_000, 200_000, name="anthropic",
                     session=RELAY_SESSION, turn="t-old"),
        mark_line("hermes.turn.end", 300_000, session=RELAY_SESSION, turn="t-old"),
        *scope_lines(RELAY_TURN, "function", 1_000_000, 6_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
        *relay_llm_lines("RL1", 1_100_000, 3_100_000, parent=RELAY_TURN,
                         wrapper="RW1", prompt="after the update"),
    ]
    session = next(s for s in assemble_lines(lines).sessions
                   if s.session_id == RELAY_SESSION)
    assert [t.start_us for t in session.turns] == [100_000, 1_000_000]
    assert session.turns[0].turn_id == "t-old"
    assert session.turns[1].user_message == "after the update"


def test_a_turn_scope_with_nothing_under_it_is_still_a_turn():
    """An empty turn is a fact about the run; dropping it would hide a turn
    that started and did nothing."""
    lines = [
        *scope_lines(RELAY_AGENT, "agent", 0, name="hermes-session", parent=None),
        *scope_lines(RELAY_TURN, "function", 1_000_000, 2_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
    ]
    assembly = assemble_lines(lines)
    turn, = assembly.sessions[0].turns
    assert turn.spans == []
    assert turn.duration_us == 1_000_000
    # and it says loudly that it could not be placed in a session
    assert turn.session_id == UNKNOWN_SESSION
    assert any("no span naming its session" in a.message for a in assembly.anomalies)


# --- both exporters live at once -----------------------------------------
# The plugin never stopped emitting its marks: the core runtime's scope tree
# arrived *alongside* them, so each real turn is described twice, a few
# milliseconds apart. Building a Turn from each produced a duplicate row
# whose spans had all gone to the other one.


def relay_and_mark_stream(*, mark_turn_id=RELAY_TURN_ID, mark_offset=30_000):
    """One real turn, as both exporters describe it.

    The mark always lands just after the scope opens, and its turn_id is the
    one the tool spans beneath the scope carry.
    """
    return [
        *scope_lines(RELAY_AGENT, "agent", 0, name="hermes-session", parent=None),
        *scope_lines(RELAY_TURN, "function", 1_000_000, 6_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
        mark_line("hermes.turn.start", 1_000_000 + mark_offset,
                  session=RELAY_SESSION, turn=mark_turn_id,
                  data={"user_message": "please produce a jobs report"}),
        *relay_llm_lines("RL1", 1_100_000, 3_100_000,
                         parent=RELAY_TURN, wrapper="RW1", prompt="ignored"),
        *scope_lines("RT1", "tool", 3_200_000, 3_700_000, name="terminal",
                     parent=RELAY_TURN, turn=mark_turn_id,
                     start_data={"command": "ls"}),
        mark_line("hermes.turn.end", 5_900_000,
                  session=RELAY_SESSION, turn=mark_turn_id),
    ]


def test_one_turn_described_by_both_exporters_is_one_turn():
    session, = assemble_lines(relay_and_mark_stream()).sessions
    assert len(session.turns) == 1


def test_the_merged_turn_keeps_the_marks_account_of_what_it_was():
    """The mark carries hermes' own unwrapped prompt and the turn_id; the
    scope carries neither, so the mark wins on identity."""
    turn, = assemble_lines(relay_and_mark_stream()).sessions[0].turns
    assert turn.turn_id == RELAY_TURN_ID
    assert turn.user_message == "please produce a jobs report"


def test_the_merged_turn_keeps_the_scopes_account_of_what_ran():
    turn, = assemble_lines(relay_and_mark_stream()).sessions[0].turns
    assert sorted(s.name for s in turn.spans) == ["openai-codex", "terminal"]
    assert turn.llm_us == 2_000_000
    assert turn.tool_us == 500_000


def test_the_merged_turn_covers_both_intervals():
    """The two bracket the same work milliseconds apart; the union is the
    one that certainly contains it, and the waterfall lays spans out from
    the turn's own start."""
    turn, = assemble_lines(relay_and_mark_stream()).sessions[0].turns
    assert turn.start_us == 1_000_000          # the scope opened first
    assert turn.end_us == 6_000_000            # the scope closed last
    assert all(s.start_us >= turn.start_us for s in turn.spans)


def test_the_pair_is_matched_on_turn_id_not_on_timing():
    """A mark landing three seconds after its scope still belongs to it —
    the observed offsets run that wide."""
    lines = relay_and_mark_stream(mark_offset=3_100_000)
    session, = assemble_lines(lines).sessions
    assert len(session.turns) == 1
    assert session.turns[0].turn_id == RELAY_TURN_ID


def test_a_turn_whose_spans_carry_no_turn_id_still_matches_by_overlap():
    """A turn with only llm calls under it has no turn_id anywhere — llm
    spans carry none — so the fallback is that the two describe the same
    stretch of time."""
    lines = [
        *scope_lines(RELAY_AGENT, "agent", 0, name="hermes-session", parent=None),
        *scope_lines(RELAY_TURN, "function", 1_000_000, 6_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
        mark_line("hermes.turn.start", 1_030_000, session=RELAY_SESSION,
                  turn="some-turn-id", data={"user_message": "test"}),
        *relay_llm_lines("RL1", 1_100_000, 3_100_000, parent=RELAY_TURN,
                         wrapper="RW1", prompt="test"),
        mark_line("hermes.turn.end", 5_900_000, session=RELAY_SESSION,
                  turn="some-turn-id"),
    ]
    session, = assemble_lines(lines).sessions
    turn, = session.turns
    assert turn.user_message == "test"
    assert [s.name for s in turn.spans] == ["openai-codex"]


def test_a_just_started_turn_inherits_its_session_from_its_agent_scope():
    """A turn that has run no span names no session of its own. Without the
    agent scope above it, it would strand itself in (unknown session) and
    then fail to recognize the mark that describes it — an empty duplicate
    row for every turn in flight."""
    lines = [
        *relay_and_mark_stream(),
        # a second turn under the same agent, opened but not yet worked
        *scope_lines("relay-turn-2", "function", 8_000_000,
                     name="hermes.turn", parent=RELAY_AGENT),
        mark_line("hermes.turn.start", 8_005_000, session=RELAY_SESSION,
                  turn="turn-2-id", data={"user_message": "still there?"}),
    ]
    session, = assemble_lines(lines).sessions
    assert session.session_id == RELAY_SESSION
    assert len(session.turns) == 2
    latest = session.turns[-1]
    assert latest.spans == []
    assert latest.user_message == "still there?"
    assert latest.is_live


def test_two_concurrent_subagent_turns_are_not_mistaken_for_duplicates():
    """Delegated subagents start within milliseconds of each other. They are
    separate sessions and must stay separate turns."""
    lines = [
        *scope_lines("agent-a", "agent", 0, name="hermes-session", parent=None),
        *scope_lines("turn-a", "function", 1_000_000, 5_000_000,
                     name="hermes.turn", parent="agent-a"),
        *scope_lines("tool-a", "tool", 1_100_000, 1_200_000, name="terminal",
                     parent="turn-a", turn="sess-a:sa-0:aaaa"),
        *scope_lines("agent-b", "agent", 0, name="hermes-session", parent=None),
        *scope_lines("turn-b", "function", 1_000_002, 5_000_002,
                     name="hermes.turn", parent="agent-b"),
        *scope_lines("tool-b", "tool", 1_100_002, 1_200_002, name="terminal",
                     parent="turn-b", turn="sess-b:sa-1:bbbb"),
    ]
    assembly = assemble_lines(lines)
    ids = sorted(s.session_id for s in assembly.sessions)
    assert ids == ["sess-a", "sess-b"]
    assert all(len(s.turns) == 1 for s in assembly.sessions)


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
