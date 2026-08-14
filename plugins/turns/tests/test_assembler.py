"""Assembler tests: events → spans → turns → sessions.

What a span *says* about itself is `test_spans.py`; this is what several
events add up to — pairing, turn bounds, session grouping, the anomalies
none of that can explain.

Streams mirror what the hermes-agent nemo_relay plugin emits: an agent
session scope, hermes.turn.start/end marks, llm and tool child scopes with
correlation metadata.
"""

import json

from plugins.turns.assembler import UNKNOWN_SESSION, assemble
from plugins.turns.atof_reader import parse_lines
from streams import (SESSION_SCOPE_UUID, assemble_lines, mark_line,
                     scope_lines, session_scope_lines, two_turn_stream)


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
