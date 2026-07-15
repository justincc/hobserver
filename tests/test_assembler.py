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
        *scope_lines("T1", "tool", 1_500_000, 1_600_000, name="terminal",
                     session="s1", turn="t1",
                     start_data={"name": "not-a-skill", "command": "ls",
                                 "action": "not-a-skill-action"}),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    view, manage, other = assemble_lines(lines).sessions[0].turns[0].spans
    assert view.skill_name == "adaptive-information-gathering"
    assert view.skill_file_path == "references/fallbacks.md"
    assert view.skill_action is None       # skill_view has no action
    assert manage.skill_name == "job-seeker"
    assert manage.skill_action == "patch"
    assert manage.skill_file_path is None
    # the generic name/action keys mean nothing outside a skill scope
    assert other.skill_name is None
    assert other.skill_action is None
    assert other.skill_file_path is None


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
