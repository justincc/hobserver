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
