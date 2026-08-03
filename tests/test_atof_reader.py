"""ATOF parser tests.

The first fixtures are the verbatim example lines from the ATOF v0.1 spec
(NVIDIA/NeMo-Agent-Toolkit packages/nvidia_nat_atif/atof-event-format.md);
the hermes-shaped fixtures mirror what the hermes-agent nemo_relay plugin
emits (LLM/tool scopes with correlation metadata, hermes.turn.* marks).

The `relay-runtime` fixtures at the end mirror the *second* exporter — the
core runtime that superseded that plugin in hermes on 2026-07-19 — and are
copied from real shapes in a live log, down to the awkward ones: a message
item's content is a list of typed parts, a raw function_call's arguments
are a JSON string, and a failing tool still reports `otel.status_code: OK`.
"""

import json

import pytest

from plugins.prompts.atof_reader import (
    OBSERVER_V1,
    RELAY_RUNTIME,
    AtofParseError,
    detect_schema,
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


# --- schema eras: the relay-runtime exporter -----------------------------
# Shapes copied from a live hermes log written after the 2026-07-19 core
# runtime landed. The plugin stamped telemetry_schema_version on every event
# it wrote; the core runtime stamps nothing, which is the whole dial.

RELAY_TURN_ID = "eb8e54f7a700:eb8e54f7a700:3239e837"

RELAY_USAGE = {
    "input_tokens": 36105,
    "input_tokens_details": {"cached_tokens": 16896, "cache_write_tokens": 0},
    "output_tokens": 247,
    "output_tokens_details": {"reasoning_tokens": 43},
    "total_tokens": 36352,
}

RELAY_OUTPUT = [
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "..."},
    {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
     "phase": "final_answer",
     "content": [{"type": "output_text", "text": "Test received.",
                  "annotations": [], "logprobs": []}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_z5Rx",
     "name": "skill_view", "arguments": '{"name":"job-seeker"}',
     "status": "completed"},
]


def relay_llm_end(*, data=None, profile=None, metadata=None):
    return json.dumps({
        "kind": "scope", "scope_category": "end", "atof_version": "0.1",
        "uuid": "u-llm", "parent_uuid": "u-logical", "name": "openai-codex",
        "timestamp": 1785778552033752, "category": "llm", "attributes": [],
        "category_profile": profile if profile is not None else {
            "model_name": "gpt-5.6-sol",
            "annotated_response": {
                "finish_reason": "complete",
                "tool_calls": [{"name": "skill_view", "id": "call_z5Rx",
                                "arguments": {"name": "job-seeker"}}],
                "usage": {"prompt_tokens": 36105, "cache_read_tokens": 16896,
                          "completion_tokens": 247, "total_tokens": 36352},
            },
        },
        "data": data if data is not None else {
            "id": "resp_1", "model": "gpt-5.6-sol", "status": "completed",
            "error": None, "incomplete_details": None, "output_text": "",
            "output": RELAY_OUTPUT, "usage": RELAY_USAGE,
        },
        "metadata": metadata if metadata is not None else {
            "api_mode": "responses", "api_request_id": "req-1",
            "call_role": "primary", "retry_count": 0, "otel.status_code": "OK",
        },
    })


def relay_tool_end(*, data, metadata=None):
    return json.dumps({
        "kind": "scope", "scope_category": "end", "atof_version": "0.1",
        "uuid": "u-tool", "parent_uuid": "u-logical", "name": "read_file",
        "timestamp": 1785778552033752, "category": "tool", "attributes": [],
        "category_profile": {}, "data": data,
        "metadata": metadata if metadata is not None else {
            "api_request_id": "req-1", "task_id": "eb8e54f7a700",
            "tool_call_id": "call_1", "turn_id": RELAY_TURN_ID,
            "otel.status_code": "OK",
        },
    })


# --- era detection -------------------------------------------------------

def test_plugin_events_declare_their_schema_and_are_left_alone():
    line = hermes_event(
        scope_category="end",
        metadata={**HERMES_METADATA, "telemetry_schema_version": OBSERVER_V1},
        data={"assistant_message": {"role": "assistant", "content": "hi",
                                    "tool_calls": []},
              "finish_reason": "stop",
              "usage": {"prompt_tokens": 10, "input_tokens": 4,
                        "cache_read_tokens": 6, "request_count": 1}},
    )
    e = parse_line(line)
    assert e.schema == OBSERVER_V1 and e.schema_is_known
    # byte-for-byte what the exporter wrote: no mapping runs on this era
    assert e.data["usage"] == {"prompt_tokens": 10, "input_tokens": 4,
                               "cache_read_tokens": 6, "request_count": 1}
    assert e.data["finish_reason"] == "stop"


def test_unstamped_events_are_read_as_the_core_runtime():
    e = parse_line(relay_llm_end())
    assert e.schema == RELAY_RUNTIME and e.schema_is_known


def test_detect_schema_reads_the_declaration_not_the_shape():
    assert detect_schema({}) == RELAY_RUNTIME
    assert detect_schema({"telemetry_schema_version": OBSERVER_V1}) == OBSERVER_V1


def test_an_unrecognized_schema_is_surfaced_rather_than_guessed_at():
    """hermes' observability layer is under active change; the next envelope
    must fail loudly here instead of being mapped as one of these two."""
    line = relay_llm_end(metadata={"telemetry_schema_version": "hermes.observer.v9"})
    e = parse_line(line)
    assert e.schema == "hermes.observer.v9"
    assert not e.schema_is_known
    # and its payload is left exactly as it arrived — an unknown shape is
    # not one to rewrite
    assert "assistant_message" not in e.data


def test_both_eras_parse_from_one_stream():
    """The exporters overlap: the old runtime keeps emitting for a few
    events after the new one starts, so the dial is per event, not per file."""
    events, errors = parse_lines([
        hermes_event(metadata={**HERMES_METADATA,
                               "telemetry_schema_version": OBSERVER_V1}),
        relay_llm_end(),
    ])
    assert not errors
    assert [e.schema for e in events] == [OBSERVER_V1, RELAY_RUNTIME]


# --- llm payload normalization -------------------------------------------

def test_relay_llm_end_yields_the_canonical_assistant_message():
    e = parse_line(relay_llm_end())
    message = e.data["assistant_message"]
    assert message["role"] == "assistant"
    # joined out of output[message].content[output_text].text
    assert message["content"] == "Test received."
    assert [c["name"] for c in message["tool_calls"]] == ["skill_view"]


def test_relay_llm_end_carries_the_finish_reason_across():
    """Passed through as the exporter reports it — "complete", not the old
    envelope's tool_calls/stop split, which this app would be inferring."""
    e = parse_line(relay_llm_end())
    assert e.data["finish_reason"] == "complete"


def test_relay_tool_calls_fall_back_to_the_raw_output_items():
    """With no annotation to read, the provider's own function_call items
    still name the tools — and their arguments arrive as a JSON string."""
    e = parse_line(relay_llm_end(profile={"model_name": "gpt-5.6-sol"}))
    calls = e.data["assistant_message"]["tool_calls"]
    assert [c["name"] for c in calls] == ["skill_view"]
    assert calls[0]["arguments"] == {"name": "job-seeker"}
    assert calls[0]["id"] == "call_z5Rx"


def test_relay_usage_is_remapped_to_canonical_names():
    e = parse_line(relay_llm_end())
    assert e.data["usage"] == {
        "prompt_tokens": 36105,
        "cache_read_tokens": 16896,
        "cache_write_tokens": 0,
        "output_tokens": 247,
        "reasoning_tokens": 43,
        "total_tokens": 36352,
        # derived: the provider's input_tokens is the whole prompt, hermes'
        # meant only what was sent fresh
        "input_tokens": 36105 - 16896,
    }


def test_relay_usage_never_reports_a_request_count_it_was_not_given():
    """The old envelope always said 1; the new one says nothing, and absent
    is not the same as a count this app made up."""
    assert "request_count" not in parse_line(relay_llm_end()).data["usage"]


def test_relay_usage_omits_fresh_input_when_the_parts_do_not_partition():
    usage = {"input_tokens": 100,
             "input_tokens_details": {"cached_tokens": 400,
                                      "cache_write_tokens": 0}}
    e = parse_line(relay_llm_end(data={"output": [], "usage": usage}))
    assert e.data["usage"]["prompt_tokens"] == 100
    assert e.data["usage"]["cache_read_tokens"] == 400
    assert "input_tokens" not in e.data["usage"]


def test_relay_llm_end_keeps_the_provider_payload_beside_the_canonical_one():
    e = parse_line(relay_llm_end())
    assert e.data["id"] == "resp_1"          # nothing is dropped in mapping
    assert isinstance(e.data["output"], list)


def test_a_payload_that_is_already_canonical_is_not_rewritten():
    """An old-format event that lost its stamp still reads correctly rather
    than being mapped over."""
    canonical = {"assistant_message": {"role": "assistant", "content": "hi",
                                       "tool_calls": []},
                 "finish_reason": "stop",
                 "usage": {"prompt_tokens": 10, "input_tokens": 10}}
    e = parse_line(relay_llm_end(data=canonical, profile={}))
    assert e.data == canonical


def test_a_non_provider_payload_is_left_alone():
    """`data` is opaque per the spec — other producers put their own shapes
    here, and an empty assistant_message over one would be noise."""
    e = parse_line(SPEC_LLM_END)
    assert e.data == {"response": "Hello!"}
    assert "assistant_message" not in e.data


# --- correlation and outcome ---------------------------------------------

def test_session_id_is_recovered_from_the_composite_turn_id():
    e = parse_line(relay_tool_end(data="{}"))
    assert e.turn_id == RELAY_TURN_ID
    assert e.session_id == "eb8e54f7a700"


def test_session_id_is_not_invented_from_a_turn_id_without_one():
    e = parse_line(relay_tool_end(
        data="{}", metadata={"turn_id": "plain-turn-id"}))
    assert e.session_id is None


def test_a_declared_session_id_is_never_overwritten():
    e = parse_line(relay_tool_end(
        data="{}", metadata={"turn_id": RELAY_TURN_ID, "session_id": "real"}))
    assert e.session_id == "real"


def test_a_failing_tool_is_marked_failed_from_its_own_error_text():
    """otel.status_code reads OK on every failing tool call in the log — it
    reports the span's transport, not the tool's outcome — so the error
    string the tool itself returned is what the status is taken from."""
    line = relay_tool_end(data=json.dumps(
        {"content": "", "error": "Binary file - cannot display as text."}))
    e = parse_line(line)
    assert e.metadata["otel.status_code"] == "OK"     # unchanged, and useless
    assert e.metadata["status"] == "error"


def test_a_succeeding_tool_is_not_marked_failed():
    e = parse_line(relay_tool_end(data=json.dumps({"content": "ok"})))
    assert "status" not in e.metadata


def test_tool_end_payloads_stay_json_strings_for_the_span_to_decode():
    """The exporter writes tool results as JSON text in both eras, and the
    Span properties already decode that — normalization must not change it."""
    e = parse_line(relay_tool_end(data='{"content": "ok"}'))
    assert e.data == '{"content": "ok"}'


def test_llm_session_is_recovered_from_the_request_headers():
    """llm spans carry neither turn_id nor session_id, and neither does the
    logical_llm_call scope above them — the provider request's own headers
    are the only place the session survived."""
    e = parse_line(relay_llm_end(profile={
        "model_name": "gpt-5.6-sol",
        "annotated_request": {"extra_headers": {"session_id": "eb8e54f7a700",
                                                "x-client-request-id": "eb8e54f7a700"}},
    }))
    assert e.session_id == "eb8e54f7a700"


def test_the_turn_id_wins_over_the_request_headers():
    e = parse_line(relay_tool_end(data="{}", metadata={"turn_id": RELAY_TURN_ID}))
    assert e.session_id == "eb8e54f7a700"


def test_request_headers_without_a_session_invent_nothing():
    e = parse_line(relay_llm_end(profile={
        "annotated_request": {"extra_headers": {"authorization": "Bearer x"}}}))
    assert e.session_id is None


# --- the chat-completions route ------------------------------------------
# hermes' auxiliary work — context compression, and whatever a plugin
# registers via register_auxiliary_task — goes out over `chat_completions`
# rather than the `responses` API the main loop uses. The payload is shaped
# differently and, unlike the main route, reports no token counts of its own
# beyond a total: the real figures live in hermes' annotation.

CHAT_ANNOTATED = {
    "model_name": "gpt-5.6-sol",
    "annotated_response": {
        "api_specific": {"api": "openai_chat"},
        "finish_reason": "complete",
        "message": "## Historical Task Snapshot\nUser asked: …",
        "model": "gpt-5.6-sol",
        "usage": {"prompt_tokens": 11517, "completion_tokens": 3192,
                  "total_tokens": 14709},
    },
}

CHAT_DATA = {
    "choices": [{"finish_reason": "stop", "index": 0,
                 "message": {"role": "assistant",
                             "content": "## Historical Task Snapshot\nUser asked: …"}}],
    "model": "gpt-5.6-sol",
    "usage": {"total_tokens": 14709},
}

CHAT_METADATA = {
    "api_mode": "chat_completions",
    "api_request_id": "aux-7981c77a3bcd40ff9170fca41d3b063b",
    "auxiliary_task": "compression",
    "call_role": "auxiliary:compression",
    "otel.status_code": "OK",
    "retry_count": 0,
}


def test_chat_completions_text_is_read_from_the_annotation():
    """`annotated_response.message` is hermes' own normalization and matched
    the raw payload on every call in the log, both APIs — so it leads."""
    e = parse_line(relay_llm_end(data=CHAT_DATA, profile=CHAT_ANNOTATED,
                                 metadata=CHAT_METADATA))
    assert e.data["assistant_message"]["content"].startswith(
        "## Historical Task Snapshot")


def test_chat_completions_text_falls_back_to_choices():
    """With no annotation, the chat payload's own shape still yields it —
    `choices[].message.content`, where the responses route has `output`."""
    e = parse_line(relay_llm_end(data=CHAT_DATA, profile={},
                                 metadata=CHAT_METADATA))
    assert e.data["assistant_message"]["content"].startswith(
        "## Historical Task Snapshot")


def test_chat_completions_counts_come_from_the_annotation():
    """The raw chat payload reports only a total; without the annotation the
    span showed a duration and no tokens at all."""
    e = parse_line(relay_llm_end(data=CHAT_DATA, profile=CHAT_ANNOTATED,
                                 metadata=CHAT_METADATA))
    assert e.data["usage"]["prompt_tokens"] == 11517
    assert e.data["usage"]["output_tokens"] == 3192
    assert e.data["usage"]["total_tokens"] == 14709


def test_a_route_reporting_no_cache_gets_no_fresh_input_figure():
    """Deriving `in` from an absent cache read would state that the whole
    prompt was fresh — a claim about caching from a payload silent on it."""
    e = parse_line(relay_llm_end(data=CHAT_DATA, profile=CHAT_ANNOTATED,
                                 metadata=CHAT_METADATA))
    assert "cache_read_tokens" not in e.data["usage"]
    assert "input_tokens" not in e.data["usage"]


def test_the_raw_payload_wins_where_both_report_a_count():
    """On the responses route the raw usage is the more detailed of the two,
    carrying the cache write and the reasoning split; the annotation only
    fills what it did not say."""
    e = parse_line(relay_llm_end())      # responses-shaped, both present
    usage = e.data["usage"]
    assert usage["prompt_tokens"] == 36105        # raw, not annotated
    assert usage["cache_write_tokens"] == 0       # annotation has no such key
    assert usage["reasoning_tokens"] == 43


def test_chat_completions_tool_calls_are_read_from_the_choice():
    """Chat-shaped tool calls nest name and arguments under `function`, and
    the arguments arrive as a JSON string."""
    data = {"choices": [{"message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "skill_view",
                                     "arguments": '{"name":"job-seeker"}'}}]}}]}
    e = parse_line(relay_llm_end(data=data, profile={}, metadata=CHAT_METADATA))
    calls = e.data["assistant_message"]["tool_calls"]
    assert [c["name"] for c in calls] == ["skill_view"]
    assert calls[0]["arguments"] == {"name": "job-seeker"}
    assert calls[0]["id"] == "call_1"
