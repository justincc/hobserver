"""ATOF parser tests.

The first fixtures are the verbatim example lines from the ATOF v0.1 spec
(NVIDIA/NeMo-Agent-Toolkit packages/nvidia_nat_atif/atof-event-format.md);
the hermes-shaped fixtures mirror what the hermes-agent nemo_relay plugin
emits (LLM/tool scopes with correlation metadata, hermes.turn.* marks).
"""

import json

import pytest

from plugins.prompts.atof_reader import (
    AtofParseError,
    normalize_timestamp,
    parse_line,
    parse_lines,
)

# Verbatim from the spec's "Example Events" section.
SPEC_LLM_START = '{"kind":"scope","scope_category":"start","atof_version":"0.1","uuid":"550e8400-e29b-41d4-a716-446655440000","parent_uuid":"550e8400-e29b-41d4-a716-446655440001","timestamp":"2026-01-01T00:00:00.000000Z","name":"gpt-4.1","attributes":["streaming"],"category":"llm","category_profile":{"model_name":"gpt-4.1"},"data":{"messages":[{"role":"user","content":"Hello"}]},"data_schema":null,"metadata":null}'
SPEC_LLM_END = '{"kind":"scope","scope_category":"end","atof_version":"0.1","uuid":"550e8400-e29b-41d4-a716-446655440000","parent_uuid":"550e8400-e29b-41d4-a716-446655440001","timestamp":"2026-01-01T00:00:01.500000Z","name":"gpt-4.1","attributes":["streaming"],"category":"llm","category_profile":{"model_name":"gpt-4.1"},"data":{"response":"Hello!"},"data_schema":null,"metadata":null}'
SPEC_MARK = '{"kind":"mark","atof_version":"0.1","uuid":"550e8400-e29b-41d4-a716-446655440002","parent_uuid":"550e8400-e29b-41d4-a716-446655440001","timestamp":"2026-01-01T00:00:02.000000Z","name":"workflow_checkpoint","category":null,"category_profile":null,"data":{"step":"validation_passed"},"data_schema":null,"metadata":null}'

HERMES_METADATA = {
    "session_id": "sess-1",
    "turn_id": "turn-7",
    "api_request_id": "req-3",
    "platform": "webui",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
}


def hermes_event(**overrides):
    base = {
        "kind": "scope",
        "scope_category": "start",
        "atof_version": "0.1",
        "uuid": "u-1",
        "parent_uuid": "u-session",
        "timestamp": 1767225600000000,
        "name": "anthropic",
        "attributes": [],
        "category": "llm",
        "category_profile": {"model_name": "claude-sonnet-4-6"},
        "data": {"turn_id": "turn-7", "api_request_id": "req-3"},
        "metadata": dict(HERMES_METADATA),
    }
    base.update(overrides)
    return json.dumps(base)


def test_spec_scope_start_example_parses():
    e = parse_line(SPEC_LLM_START)
    assert e.kind == "scope"
    assert e.is_scope_start and not e.is_scope_end and not e.is_mark
    assert e.uuid == "550e8400-e29b-41d4-a716-446655440000"
    assert e.parent_uuid == "550e8400-e29b-41d4-a716-446655440001"
    assert e.name == "gpt-4.1"
    assert e.category == "llm"
    assert e.model_name == "gpt-4.1"
    assert e.attributes == ("streaming",)
    assert e.data == {"messages": [{"role": "user", "content": "Hello"}]}
    assert e.metadata == {}
    assert e.atof_version == "0.1"


def test_spec_pair_shares_uuid_and_duration_is_derivable():
    start = parse_line(SPEC_LLM_START)
    end = parse_line(SPEC_LLM_END)
    assert start.uuid == end.uuid
    assert end.timestamp_us - start.timestamp_us == 1_500_000


def test_spec_mark_example_parses():
    e = parse_line(SPEC_MARK)
    assert e.is_mark
    assert e.scope_category is None
    assert e.category is None
    assert e.data == {"step": "validation_passed"}


def test_integer_and_string_timestamps_normalize_identically():
    # the spec gives this exact equivalent pair
    assert normalize_timestamp("2026-01-01T00:00:00.123456Z") == 1767225600123456
    assert normalize_timestamp(1767225600123456) == 1767225600123456


def test_explicit_offset_timestamp_normalizes_to_utc():
    assert normalize_timestamp("2026-01-01T01:00:00+01:00") == 1767225600000000


def test_naive_timestamp_is_rejected():
    with pytest.raises(AtofParseError, match="explicit UTC offset"):
        normalize_timestamp("2026-01-01T00:00:00")


def test_mixed_timestamp_formats_in_one_stream_are_comparable():
    lines = [
        hermes_event(uuid="a", timestamp=1767225600000000),
        hermes_event(uuid="b", timestamp="2026-01-01T00:00:01Z"),
    ]
    events, errors = parse_lines(lines)
    assert not errors
    assert events[1].timestamp_us - events[0].timestamp_us == 1_000_000


def test_hermes_correlation_accessors():
    e = parse_line(hermes_event())
    assert e.session_id == "sess-1"
    assert e.turn_id == "turn-7"
    assert e.api_request_id == "req-3"
    assert e.model_name == "claude-sonnet-4-6"


def test_tool_call_id_prefers_category_profile_over_metadata():
    e = parse_line(
        hermes_event(
            category="tool",
            category_profile={"tool_call_id": "call-profile"},
            metadata={**HERMES_METADATA, "tool_call_id": "call-metadata"},
        )
    )
    assert e.tool_call_id == "call-profile"
    e = parse_line(
        hermes_event(
            category="tool",
            category_profile={},
            metadata={**HERMES_METADATA, "tool_call_id": "call-metadata"},
        )
    )
    assert e.tool_call_id == "call-metadata"


def test_hermes_turn_mark_carries_full_hook_kwargs_in_data():
    line = json.dumps({
        "kind": "mark",
        "uuid": "m-1",
        "parent_uuid": "u-session",
        "timestamp": "2026-01-01T00:00:00Z",
        "name": "hermes.turn.start",
        "data": {"user_message": "hi", "is_first_turn": True},
        "metadata": dict(HERMES_METADATA),
    })
    e = parse_line(line)
    assert e.name == "hermes.turn.start"
    assert e.data["user_message"] == "hi"
    assert e.session_id == "sess-1"


def test_parse_lines_is_fail_soft_and_keeps_line_numbers():
    lines = [
        SPEC_LLM_START,          # 1: good
        "not json at all",       # 2: bad
        "",                      # 3: blank, skipped
        '["array","not-dict"]',  # 4: bad
        SPEC_LLM_END,            # 5: good
    ]
    events, errors = parse_lines(lines)
    assert [e.line_no for e in events] == [1, 5]
    assert [err.line_no for err in errors] == [2, 4]
    assert errors[0].message == "invalid JSON"
    assert errors[0].line_preview == "not json at all"
    assert errors[1].message == "event is not a JSON object"


def test_parse_lines_first_line_no_offsets_provenance():
    events, errors = parse_lines([SPEC_MARK, "bad"], first_line_no=100)
    assert events[0].line_no == 100
    assert errors[0].line_no == 101


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"kind": "quux"}, "unknown kind"),
        ({"kind": None}, "missing or non-string required field 'kind'"),
        ({"uuid": None}, "missing or non-string required field 'uuid'"),
        ({"name": None}, "missing or non-string required field 'name'"),
        ({"scope_category": "middle"}, "scope event needs scope_category"),
        ({"scope_category": None}, "scope event needs scope_category"),
        ({"timestamp": None}, "timestamp must be an RFC 3339 string or integer"),
        ({"timestamp": True}, "timestamp must be an RFC 3339 string or integer"),
        ({"timestamp": "yesterday"}, "unparseable RFC 3339 timestamp"),
        ({"attributes": "streaming"}, "'attributes' must be an array"),
        ({"metadata": "oops"}, "'metadata' must be an object or null"),
        ({"category_profile": 7}, "'category_profile' must be an object or null"),
        ({"parent_uuid": 3}, "'parent_uuid' must be a string or null"),
        ({"category": 3}, "'category' must be a string or null"),
    ],
)
def test_structurally_unusable_lines_are_rejected(mutation, expected):
    with pytest.raises(AtofParseError, match=expected):
        parse_line(hermes_event(**mutation))


def test_missing_timestamp_field_is_rejected():
    obj = json.loads(hermes_event())
    del obj["timestamp"]
    with pytest.raises(AtofParseError, match="missing required field 'timestamp'"):
        parse_line(json.dumps(obj))


def test_unknown_extra_fields_are_tolerated():
    e = parse_line(hermes_event(vendor_extra={"x": 1}, data_schema={"name": "s", "version": 1}))
    assert e.uuid == "u-1"


def test_mark_missing_optional_fields_defaults():
    e = parse_line('{"kind":"mark","uuid":"m","name":"n","timestamp":0}')
    assert e.parent_uuid is None
    assert e.category is None
    assert e.category_profile == {}
    assert e.metadata == {}
    assert e.data is None
    assert e.attributes == ()
    assert e.atof_version is None
    assert e.timestamp_us == 0
