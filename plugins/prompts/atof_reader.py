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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

KINDS = ("scope", "mark")
SCOPE_CATEGORIES = ("start", "end")

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

# Substrings of key names whose values must never be rendered. The log has
# none today — hermes' llm spans carry an empty `headers` dict — but a
# generic renderer must not be the thing that prints a bearer token onto a
# page, so the guard does not wait for the exporter to start filling it in.
GENERIC_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey",
                        "authorization", "auth", "credential", "cookie",
                        "headers")

# Above this, a value is described rather than printed. The log holds a 7 MB
# conversation_history and a 153 KB tool result, and a live turn page
# refetches itself every 2 s.
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

    return AtofEvent(
        kind=kind,
        scope_category=scope_category,
        uuid=_require_str(obj, "uuid", line_no),
        parent_uuid=parent_uuid,
        timestamp_us=normalize_timestamp(obj["timestamp"], line_no),
        name=_require_str(obj, "name", line_no),
        category=category,
        category_profile=_optional_dict(obj, "category_profile", line_no),
        attributes=attributes,
        data=obj.get("data"),
        metadata=_optional_dict(obj, "metadata", line_no),
        atof_version=obj.get("atof_version"),
        line_no=line_no,
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
