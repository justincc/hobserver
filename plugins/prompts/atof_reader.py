"""ATOF v0.1 JSONL parser — the middle layer of the reader (docs/adr/0002).

Parses raw JSONL lines into typed events per the ATOF spec
(NVIDIA/NeMo-Agent-Toolkit packages/nvidia_nat_atif/atof-event-format.md):

- Every line is one JSON object with a ``kind`` of ``"scope"`` or ``"mark"``.
- Scope events carry ``scope_category`` ("start"/"end"); the two events of a
  pair share a ``uuid``. Marks are unpaired checkpoints.
- ``timestamp`` is either an RFC 3339 string (must be "Z" or carry an
  explicit UTC offset) or integer epoch microseconds UTC; one stream may mix
  both, so both are normalized to epoch microseconds here.
- ``data`` is an opaque application payload; ``metadata`` is the
  tracing/correlation envelope (the hermes nemo_relay plugin puts
  session_id / turn_id / api_request_id / tool_call_id there).

Parsing is fail-soft: one bad line becomes a ParseError record instead of
killing the read, so the assembler and UI can surface it loudly (ADR 2)
while still rendering every good event. Unknown extra fields are tolerated;
only structurally unusable lines are rejected.

hermes writes this stream through two exporters at once, and this module is
the only one that knows it: each event is tagged with the schema that wrote
it, and the newer exporter's provider-native payloads are mapped back onto
the canonical envelope the rest of the app reads (ADR 6). See "schema eras"
below and docs/atof-reader.md for the mapping table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

KINDS = ("scope", "mark")
SCOPE_CATEGORIES = ("start", "end")

# --- schema eras ---------------------------------------------------------
# hermes has exported this stream under two different owners, and one log
# file holds both: the nemo_relay *plugin* stamped every event it emitted
# with `telemetry_schema_version`, and the core relay runtime that
# superseded it (hermes `agent/relay_runtime.py`, landed 2026-07-19) stamps
# nothing. Presence of the key is therefore the era dial.
#
# The dial is read per event, never per file. The two exporters overlap:
# when hermes restarts onto the new code the old runtime keeps emitting for
# a few events, so a file-level sniff would misparse the tail of the old
# session. One scope pair was also observed straddling the changeover, which
# is why a span takes its era from its start event (see the assembler).
SCHEMA_KEY = "telemetry_schema_version"
OBSERVER_V1 = "hermes.observer.v1"      # the nemo_relay plugin's envelope
RELAY_RUNTIME = "hermes.relay.runtime"  # core runtime; declares no version
KNOWN_SCHEMAS = frozenset({OBSERVER_V1, RELAY_RUNTIME})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)
_ERROR_LINE_PREVIEW_CHARS = 200

# --- generic payload rendering ------------------------------------------
# What to show for a scope or mark nothing renders specially. hermes' tool
# set is not this app's to know: with tools these readers have never heard
# of, the fallback is all a reader gets, so it errs towards showing a key
# and withholding only the value.

# Correlation plumbing, shown elsewhere on the row or of no interest at all.
GENERIC_SKIP_KEYS = frozenset({
    "session_id", "turn_id", "task_id", "tool_call_id", "api_request_id",
    "parent_session_id", "parent_turn_id", "parent_subagent_id",
    "child_subagent_id", "telemetry_schema_version", "sender_id",
    "middleware_trace",
})

# Substrings of key names whose values must never be rendered. Nothing fills
# them today — hermes' llm spans carry an empty `headers` dict — but a
# generic renderer must not be the thing that prints a bearer token onto a
# page, so the guard does not wait for the exporter to start filling it in.
GENERIC_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey",
                        "authorization", "auth", "credential", "cookie",
                        "headers")

# Above this, a value is described rather than printed. Payloads reach
# megabytes — a turn mark carries the whole conversation history — and a live
# turn page refetches itself every 2 s.
GENERIC_MAX_VALUE_CHARS = 2000

# All a single summary line can hold; everything else is detail-only.
GENERIC_INLINE_FIELDS = 3


def _human_size(chars: int) -> str:
    if chars < 1024:
        return f"{chars} chars"
    if chars < 1024 * 1024:
        return f"{chars / 1024:.0f} KB"
    return f"{chars / (1024 * 1024):.1f} MB"


def _skip_generic_key(key: str) -> bool:
    lowered = key.lower()
    return (key in GENERIC_SKIP_KEYS
            or any(hint in lowered for hint in GENERIC_SECRET_HINTS))


def _generic_value(value: Any) -> tuple[Optional[str], bool]:
    """(text, oversize) for one payload value, or (None, _) to skip it.

    Oversize values are named and measured rather than printed — the reader
    still learns the key exists and how big it is, which is what decides
    whether to go looking in the raw JSONL.
    """
    if value is None or value == "" or value == [] or value == {}:
        return None, False
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value), False
    if isinstance(value, str):
        if len(value) > GENERIC_MAX_VALUE_CHARS:
            return f"text, {_human_size(len(value))}", True
        return value, False
    # lists and dicts: compact JSON while small enough to be readable
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return f"{type(value).__name__}", True
    if len(text) > GENERIC_MAX_VALUE_CHARS:
        kind = "list" if isinstance(value, list) else "dict"
        count = (f"{len(value)} items" if isinstance(value, list)
                 else f"{len(value)} keys")
        return f"{kind}, {count} ({_human_size(len(text))})", True
    return text, False


def generic_payload_fields(data: Any) -> list:
    """A payload as [{key, text, inline, oversize}], in the payload's own
    order — which is the tool's argument order, so the useful keys lead.

    Shared by scopes and marks: both carry the same opaque `data`, and a
    fallback that differed between them would be the surprising thing.
    """
    if not isinstance(data, dict) or not data:
        return []
    fields, inlined = [], 0
    for key, value in data.items():
        if _skip_generic_key(key):
            continue
        text, oversize = _generic_value(value)
        if text is None:
            continue
        scalar = isinstance(value, (str, int, float, bool))
        inline = scalar and not oversize and inlined < GENERIC_INLINE_FIELDS
        if inline:
            inlined += 1
        fields.append({"key": key, "text": text, "inline": inline,
                       "oversize": oversize})
    return fields


# --- relay-runtime normalization ----------------------------------------
# The core runtime emits the provider's own response object where the plugin
# emitted hermes' canonical envelope. Rather than teach forty Span
# properties, the assembler and every template about a second shape, new
# events are mapped back onto the canonical one as they are parsed — the
# only place in this app that knows there were ever two formats.
#
# What is mapped is only what genuinely moved. Facts the new exporter never
# emits at all (hermes.turn.* marks, subagent marks, approvals) are absent,
# not renamed, and no amount of normalizing invents them.


def detect_schema(metadata: dict) -> str:
    """Which exporter wrote this event, from its own declaration.

    An unrecognized version is returned verbatim rather than guessed at:
    hermes' observability layer is under active change, and a reader that
    silently treats an unknown envelope as one it understands would
    misreport rather than fail (ADR 2). Callers check `KNOWN_SCHEMAS`.
    """
    declared = metadata.get(SCHEMA_KEY)
    if isinstance(declared, str) and declared:
        return declared
    return RELAY_RUNTIME


def _output_items(data: Any, item_type: str) -> list:
    """Items of one type from a provider response's `output` array."""
    if not isinstance(data, dict):
        return []
    output = data.get("output")
    if not isinstance(output, list):
        return []
    return [i for i in output
            if isinstance(i, dict) and i.get("type") == item_type]


def _assistant_text(data: Any, annotated: dict) -> str:
    """The assistant's words.

    `annotated_response.message` is hermes' own normalization and is
    uniform across the provider APIs it speaks — it matched the raw payload
    text exactly on every llm call in the log, `openai_responses` and
    `openai_chat` alike. So it leads, and the raw payloads are the fallback
    for a response the annotation missed.

    Reasoning items are deliberately not read on either route: that is the
    hidden reasoning, counted in the token tree but never shown.
    """
    message = annotated.get("message")
    if isinstance(message, str) and message:
        return message

    parts = []
    # openai_responses: a list of typed output items, whose message items
    # hold a list of typed content parts.
    for item in _output_items(data, "message"):
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    if parts:
        return "".join(parts)

    # openai_chat: choices, each with one message whose content is a string.
    for choice in _choices(data):
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content:
            parts.append(content)
    return "".join(parts)


def _choices(data: Any) -> list:
    """A chat-completions response's `choices`, defensively."""
    if not isinstance(data, dict):
        return []
    choices = data.get("choices")
    if not isinstance(choices, list):
        return []
    return [c for c in choices
            if isinstance(c, dict) and isinstance(c.get("message"), dict)]


def _tool_calls(data: Any, annotated: Any) -> list:
    """The tools the model asked for, as the canonical
    [{name, id, arguments}].

    `annotated_response.tool_calls` is preferred: hermes has already decoded
    the arguments there, where the raw `output` array carries them as a JSON
    string. The raw items are the fallback for a response the annotation
    missed.
    """
    if isinstance(annotated, dict):
        calls = annotated.get("tool_calls")
        if isinstance(calls, list) and calls:
            return [c for c in calls if isinstance(c, dict)]
    calls = []
    # openai_responses: function_call items in the output array.
    for item in _output_items(data, "function_call"):
        calls.append({"name": item.get("name"),
                      "id": item.get("call_id") or item.get("id"),
                      "arguments": _decoded_arguments(item.get("arguments"))})
    if calls:
        return calls
    # openai_chat: tool_calls on each choice's message, where the name and
    # arguments sit one level down under "function".
    for choice in _choices(data):
        raw = choice["message"].get("tool_calls")
        if not isinstance(raw, list):
            continue
        for call in raw:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            calls.append({
                "name": function.get("name") or call.get("name"),
                "id": call.get("id"),
                "arguments": _decoded_arguments(
                    function.get("arguments", call.get("arguments"))),
            })
    return calls


def _decoded_arguments(arguments: Any) -> Any:
    """Tool-call arguments, decoded when the provider sent them as JSON text.
    An undecodable string is kept as it arrived — still readable."""
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def _count(value: Any) -> Optional[int]:
    # bool is an int subclass and is never a token count
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _annotated_usage(annotated: dict) -> dict:
    """Token counts from hermes' own annotation, in canonical names.

    Uniform across provider APIs where the raw payload is not: on the
    `openai_chat` route the raw `usage` carries nothing but `total_tokens`,
    and these are the only counts there are. Narrower than the raw
    `openai_responses` usage, which also reports the cache write and the
    reasoning split — hence a fill, not a replacement.
    """
    usage = annotated.get("usage")
    if not isinstance(usage, dict):
        return {}
    canonical = {}
    for source, key in (("prompt_tokens", "prompt_tokens"),
                        ("cache_read_tokens", "cache_read_tokens"),
                        ("completion_tokens", "output_tokens"),
                        ("total_tokens", "total_tokens")):
        value = _count(usage.get(source))
        if value is not None:
            canonical[key] = value
    return canonical


def _canonical_usage(usage: Any, annotated: dict) -> dict:
    """Provider-native token counts remapped to hermes' canonical names.

    The provider's `input_tokens` is the whole prompt including what the
    cache served; hermes' `input_tokens` meant only what was sent fresh. So
    the name is not merely moved — passing it through unchanged would
    silently overstate fresh input by the whole cache read, which is worse
    than reporting nothing.

    `input_tokens` is therefore derived as the remainder, and omitted when
    the parts do not partition the prompt: a negative remainder is not a
    count this app can vouch for. `request_count` has no counterpart and
    stays absent — absent is not zero.
    """
    usage = usage if isinstance(usage, dict) else {}
    input_details = usage.get("input_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}

    canonical = {}
    for key, value in (
        ("prompt_tokens", _count(usage.get("input_tokens"))),
        ("cache_read_tokens", _count(input_details.get("cached_tokens"))),
        ("cache_write_tokens", _count(input_details.get("cache_write_tokens"))),
        ("output_tokens", _count(usage.get("output_tokens"))),
        ("reasoning_tokens", _count(output_details.get("reasoning_tokens"))),
        ("total_tokens", _count(usage.get("total_tokens"))),
    ):
        if value is not None:
            canonical[key] = value

    # Fill only what the raw payload did not report. The raw usage wins
    # where both speak, because on the responses route it is the more
    # detailed of the two.
    for key, value in _annotated_usage(annotated).items():
        canonical.setdefault(key, value)

    # Fresh input is the remainder of the prompt once the cache is taken
    # off, so it can only be derived where the cache read was actually
    # reported. Absent is not zero: the `openai_chat` route reports no
    # cache figures at all, and deriving there would state that the whole
    # prompt was fresh — a claim about caching from a payload that said
    # nothing about caching.
    prompt = canonical.get("prompt_tokens")
    cache_read = canonical.get("cache_read_tokens")
    if prompt is not None and cache_read is not None:
        fresh = prompt - cache_read - canonical.get("cache_write_tokens", 0)
        if fresh >= 0:
            canonical["input_tokens"] = fresh
    return canonical


def _normalize_llm_end(data: Any, category_profile: dict) -> Any:
    """A provider response object, rewritten as hermes' canonical llm end.

    Keeps the provider payload's own keys alongside the canonical ones, so
    nothing is lost to a reader who goes looking for what the provider
    actually said.
    """
    if not isinstance(data, dict) or "assistant_message" in data:
        # Already canonical. An event that declared no schema but speaks the
        # old envelope is left alone rather than rewritten — the mapping is
        # for provider-shaped payloads, and running it over a canonical one
        # would be this reader arguing with itself.
        return data
    annotated = category_profile.get("annotated_response")
    annotated = annotated if isinstance(annotated, dict) else {}
    if (not isinstance(data.get("output"), list)
            and not isinstance(data.get("choices"), list)
            and not annotated):
        # Not a provider response either. The ATOF spec makes `data` opaque
        # and other producers put their own shapes here; inventing an empty
        # assistant_message over one would be noise, not normalization.
        return data

    normalized = dict(data)
    normalized["assistant_message"] = {
        "role": "assistant",
        "content": _assistant_text(data, annotated),
        "tool_calls": _tool_calls(data, annotated),
    }
    finish_reason = annotated.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        # passed through as the exporter reports it. The old envelope's
        # "tool_calls" / "stop" split is not reconstructed here: it would be
        # this app inferring a provider's verdict from the presence of tool
        # calls, which the row beside it already shows.
        normalized["finish_reason"] = finish_reason
    usage = _canonical_usage(data.get("usage"), annotated)
    if usage:
        normalized["usage"] = usage
    return normalized


def _error_text(data: Any) -> Optional[str]:
    """A tool's own failure message from its end payload, dict or JSON string."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    return error if isinstance(error, str) and error else None


def _session_from_turn_id(turn_id: Any) -> Optional[str]:
    """The session segment of a composite "<session>:<task>:<hash>" turn id."""
    if not isinstance(turn_id, str) or ":" not in turn_id:
        return None
    return turn_id.split(":", 1)[0] or None


def _session_from_request_headers(category_profile: dict) -> Optional[str]:
    """The session an llm request was sent under, from its own headers.

    One named correlation key is read out of `extra_headers`; the dict
    itself is never rendered. The generic payload renderer withholds
    anything header-shaped precisely because such a dict is where a bearer
    token would sit, and that guard is untouched by this.
    """
    request = category_profile.get("annotated_request")
    if not isinstance(request, dict):
        return None
    headers = request.get("extra_headers")
    if not isinstance(headers, dict):
        return None
    session_id = headers.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def normalize_relay_runtime(data, metadata, category_profile,
                            category, scope_category):
    """Map one core-runtime event onto the canonical envelope.

    Returns the (data, metadata) an observer-v1 reader would have seen.
    """
    metadata = dict(metadata)

    # Session grouping, from whichever of the two places it survived.
    #
    # Tool spans keep a composite turn_id that leads with the session —
    # "<session>:<task>:<hash>". llm spans carry neither turn_id nor
    # session_id, and their parents do not help: the logical_llm_call scope
    # above them has no correlation keys, and the agent scope above that
    # carries an empty payload. What they do have is the session the request
    # was sent under, in the provider request's own headers.
    if not metadata.get("session_id"):
        session_id = _session_from_turn_id(metadata.get("turn_id"))
        if not session_id:
            session_id = _session_from_request_headers(category_profile)
        if session_id:
            metadata["session_id"] = session_id

    # Tool outcome. `otel.status_code` is NOT the old `status`: it reports
    # the span's transport, and reads "OK" on every failing tool call seen
    # here — mapping it across would render failures as successes. The
    # tool's own error string is the signal that survived, and the old
    # `status` was "error" exactly when it was present.
    if scope_category == "end" and _error_text(data) is not None:
        metadata.setdefault("status", "error")

    if category == "llm" and scope_category == "end":
        data = _normalize_llm_end(data, category_profile)
    return data, metadata


class AtofParseError(ValueError):
    """A single line that cannot be parsed into an ATOF event."""

    def __init__(self, message: str, line_no: int):
        super().__init__(f"line {line_no}: {message}")
        self.message = message
        self.line_no = line_no


@dataclass(frozen=True)
class ParseError:
    """Fail-soft record of a rejected line, for loud display in the UI."""

    line_no: int
    message: str
    line_preview: str


@dataclass(frozen=True)
class AtofEvent:
    kind: str                       # "scope" | "mark"
    scope_category: Optional[str]   # "start" | "end" for scopes, None for marks
    uuid: str
    parent_uuid: Optional[str]
    timestamp_us: int               # normalized epoch microseconds UTC
    name: str
    category: Optional[str]
    category_profile: dict
    attributes: tuple
    data: Any                       # opaque per spec; may be None
    metadata: dict
    atof_version: Optional[str]
    line_no: int                    # provenance in the source JSONL
    schema: str                     # which exporter wrote it; see SCHEMA_KEY

    @property
    def schema_is_known(self) -> bool:
        """False for an envelope this reader has never seen.

        `data` and `metadata` are then left exactly as they arrived — an
        unrecognized shape is not one to map — so the event still renders
        through the generic fallback while callers surface the schema
        itself as an anomaly rather than quietly trusting it.
        """
        return self.schema in KNOWN_SCHEMAS

    @property
    def is_scope_start(self) -> bool:
        return self.kind == "scope" and self.scope_category == "start"

    @property
    def is_scope_end(self) -> bool:
        return self.kind == "scope" and self.scope_category == "end"

    @property
    def is_mark(self) -> bool:
        return self.kind == "mark"

    # Correlation accessors — the hermes nemo_relay plugin's metadata keys.
    @property
    def session_id(self) -> Optional[str]:
        return self.metadata.get("session_id")

    @property
    def turn_id(self) -> Optional[str]:
        return self.metadata.get("turn_id")

    @property
    def api_request_id(self) -> Optional[str]:
        return self.metadata.get("api_request_id")

    @property
    def tool_call_id(self) -> Optional[str]:
        return self.category_profile.get("tool_call_id") or self.metadata.get("tool_call_id")

    @property
    def model_name(self) -> Optional[str]:
        return self.category_profile.get("model_name")

    @property
    def generic_fields(self) -> list:
        """This mark's payload, for marks nothing renders specially — the
        approval and session-end marks today, and whatever hermes adds next.
        See `generic_payload_fields`."""
        return generic_payload_fields(self.data)

    # hermes.subagent.start marks carry the delegated child's goal (the
    # child session's opening prompt) in their data payload
    @property
    def child_goal(self) -> Optional[str]:
        if self.name != "hermes.subagent.start" or not isinstance(self.data, dict):
            return None
        value = self.data.get("child_goal")
        return value if isinstance(value, str) and value else None

    def _subagent_data(self, key):
        if not self.name.startswith("hermes.subagent.") or not isinstance(self.data, dict):
            return None
        return self.data.get(key)

    # both subagent marks name the child session — the only key correlating
    # a stop back to its start (stops carry no child_subagent_id)
    @property
    def child_session_id(self) -> Optional[str]:
        value = self._subagent_data("child_session_id")
        return value if isinstance(value, str) and value else None

    # hermes.subagent.stop marks report how the child ended
    @property
    def child_status(self) -> Optional[str]:
        if self.name != "hermes.subagent.stop":
            return None
        value = self._subagent_data("child_status")
        return value if isinstance(value, str) and value else None

    @property
    def child_duration_ms(self) -> Optional[float]:
        if self.name != "hermes.subagent.stop":
            return None
        value = self._subagent_data("duration_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return None


def normalize_timestamp(value, line_no: int = 1) -> int:
    """Normalize either spec timestamp encoding to epoch microseconds UTC.

    Integers pass through; RFC 3339 strings must be timezone-aware ("Z" or
    an explicit offset). The subtraction path keeps microsecond precision
    exactly, unlike float ``datetime.timestamp()``.
    """
    if isinstance(value, bool):
        raise AtofParseError("timestamp must be an RFC 3339 string or integer microseconds", line_no)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise AtofParseError(f"unparseable RFC 3339 timestamp: {value!r}", line_no) from None
        if parsed.tzinfo is None:
            raise AtofParseError(
                f"timestamp {value!r} must end with 'Z' or carry an explicit UTC offset", line_no
            )
        return (parsed - _EPOCH) // _MICROSECOND
    raise AtofParseError("timestamp must be an RFC 3339 string or integer microseconds", line_no)


def _require_str(obj: dict, key: str, line_no: int) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise AtofParseError(f"missing or non-string required field {key!r}", line_no)
    return value


def _optional_dict(obj: dict, key: str, line_no: int) -> dict:
    value = obj.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AtofParseError(f"field {key!r} must be an object or null", line_no)
    return value


def parse_line(line: str, line_no: int = 1) -> AtofEvent:
    """Parse one JSONL line into an AtofEvent; raise AtofParseError if unusable."""
    try:
        obj = json.loads(line)
    except ValueError:
        raise AtofParseError("invalid JSON", line_no) from None
    if not isinstance(obj, dict):
        raise AtofParseError("event is not a JSON object", line_no)

    kind = _require_str(obj, "kind", line_no)
    if kind not in KINDS:
        raise AtofParseError(f"unknown kind {kind!r} (expected one of {KINDS})", line_no)

    scope_category = None
    attributes: tuple = ()
    if kind == "scope":
        scope_category = obj.get("scope_category")
        if scope_category not in SCOPE_CATEGORIES:
            raise AtofParseError(
                f"scope event needs scope_category in {SCOPE_CATEGORIES}, got {scope_category!r}",
                line_no,
            )
        raw_attributes = obj.get("attributes")
        if raw_attributes is not None:
            if not isinstance(raw_attributes, list):
                raise AtofParseError("field 'attributes' must be an array", line_no)
            attributes = tuple(raw_attributes)

    parent_uuid = obj.get("parent_uuid")
    if parent_uuid is not None and not isinstance(parent_uuid, str):
        raise AtofParseError("field 'parent_uuid' must be a string or null", line_no)

    category = obj.get("category")
    if category is not None and not isinstance(category, str):
        raise AtofParseError("field 'category' must be a string or null", line_no)

    if "timestamp" not in obj:
        raise AtofParseError("missing required field 'timestamp'", line_no)

    category_profile = _optional_dict(obj, "category_profile", line_no)
    metadata = _optional_dict(obj, "metadata", line_no)
    data = obj.get("data")

    # Everything above is the ATOF spec, identical in both eras. Only the
    # hermes-shaped payload inside it differs, so normalization happens
    # here, after the envelope has been validated.
    schema = detect_schema(metadata)
    if schema == RELAY_RUNTIME:
        data, metadata = normalize_relay_runtime(
            data, metadata, category_profile,
            category=category, scope_category=scope_category,
        )

    return AtofEvent(
        kind=kind,
        scope_category=scope_category,
        uuid=_require_str(obj, "uuid", line_no),
        parent_uuid=parent_uuid,
        timestamp_us=normalize_timestamp(obj["timestamp"], line_no),
        name=_require_str(obj, "name", line_no),
        category=category,
        category_profile=category_profile,
        attributes=attributes,
        data=data,
        metadata=metadata,
        atof_version=obj.get("atof_version"),
        line_no=line_no,
        schema=schema,
    )


def parse_lines(lines: Iterable[str], first_line_no: int = 1):
    """Parse an iterable of JSONL lines fail-soft.

    Blank lines are skipped. Returns ``(events, errors)`` where errors are
    ParseError records for every rejected line — callers must show them, not
    drop them (ADR 2's loud-failure requirement).
    """
    events: list[AtofEvent] = []
    errors: list[ParseError] = []
    for offset, line in enumerate(lines):
        line_no = first_line_no + offset
        if not line.strip():
            continue
        try:
            events.append(parse_line(line, line_no))
        except AtofParseError as exc:
            errors.append(
                ParseError(
                    line_no=line_no,
                    message=exc.message,
                    line_preview=line.strip()[:_ERROR_LINE_PREVIEW_CHARS],
                )
            )
    return events, errors
