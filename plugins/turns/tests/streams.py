"""ATOF line fixtures — the streams the reader tests are fed.

Shared by `test_assembler.py`, `test_spans.py` and `test_turns.py`, which
all need a log to read and should be reading the *same* one: a fixture that
drifts between two test modules is two different hermes.

Streams mirror what the hermes-agent nemo_relay plugin emits: an agent
session scope, hermes.turn.start/end marks, llm and tool child scopes with
correlation metadata.
"""

import json

from plugins.turns.assembler import assemble
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
    """Lines through the real parser and assembler, which is how a reading
    test gets a Span: the tests exercise the whole reader, never a Span
    built by hand."""
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
