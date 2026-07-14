"""ATOF assembler — the top layer of the reader (docs/adr/0002).

Takes parsed AtofEvents and builds the per-turn waterfall model:

- scope start/end events sharing a uuid become one Span;
- ``hermes.turn.start`` / ``hermes.turn.end`` marks bound each Turn;
- llm/tool spans are assigned to turns by ``turn_id`` when both sides carry
  one, else by timestamp containment (the hermes nemo_relay plugin always
  stamps turn_id on spans, but turn marks only carry it when the hook
  kwargs do);
- a span's session comes from its metadata, falling back to its
  ``parent_uuid`` — children of a session scope point at it, so grouping
  survives missing metadata;
- overhead is the residual: turn duration minus provider (llm) time minus
  tool time. It is reported as-is, even negative — a negative residual
  means spans overlap and should be seen, not clamped away.

Anything that does not assemble cleanly (end without start, turn.end
without turn.start, spans outside any turn) is kept and surfaced, never
dropped silently — the ADR 2 loud-failure rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from plugins.timing.atof_reader import AtofEvent

TURN_START_MARK = "hermes.turn.start"
TURN_END_MARK = "hermes.turn.end"
AGENT_CATEGORY = "agent"
LLM_CATEGORY = "llm"
TOOL_CATEGORY = "tool"
UNKNOWN_SESSION = "(unknown session)"


@dataclass
class Anomaly:
    message: str
    line_no: Optional[int] = None


@dataclass
class Span:
    uuid: str
    name: str
    category: Optional[str]
    session_id: str
    parent_uuid: Optional[str]
    start_us: int
    end_us: Optional[int]           # None while open (in flight, or end lost)
    metadata: dict
    start_data: Any
    end_data: Any
    model_name: Optional[str]
    tool_call_id: Optional[str]
    api_request_id: Optional[str]
    turn_id: Optional[str]
    line_no: int                    # of the start event

    @property
    def duration_us(self) -> Optional[int]:
        if self.end_us is None:
            return None
        return self.end_us - self.start_us

    @property
    def is_open(self) -> bool:
        return self.end_us is None

    # data payloads are opaque per the ATOF spec and vary in practice —
    # e.g. the nemo_relay plugin emits hermes tool results as raw JSON
    # strings — so every field access must type-guard, never assume dict.
    @property
    def usage(self) -> Optional[dict]:
        if isinstance(self.end_data, dict) and isinstance(self.end_data.get("usage"), dict):
            return self.end_data["usage"]
        return None

    @property
    def finish_reason(self) -> Optional[str]:
        if isinstance(self.end_data, dict):
            value = self.end_data.get("finish_reason")
            if isinstance(value, str):
                return value
        return None


def _user_message(data: Any) -> Optional[str]:
    """The prompt from a turn-start mark's payload. Data is opaque per the
    ATOF spec — a dict or a raw JSON string in practice — so type-guard
    every step and return None rather than guess."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if isinstance(data, dict):
        value = data.get("user_message")
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class Turn:
    session_id: str
    turn_id: Optional[str]
    start_us: int
    end_us: Optional[int] = None    # None if the end mark never arrived
    user_message: Optional[str] = None
    spans: List[Span] = field(default_factory=list)
    marks: List[AtofEvent] = field(default_factory=list)

    @property
    def duration_us(self) -> Optional[int]:
        if self.end_us is None:
            return None
        return self.end_us - self.start_us

    def _category_us(self, category: str) -> int:
        return sum(
            s.duration_us for s in self.spans
            if s.category == category and s.duration_us is not None
        )

    @property
    def llm_us(self) -> int:
        return self._category_us(LLM_CATEGORY)

    @property
    def tool_us(self) -> int:
        return self._category_us(TOOL_CATEGORY)

    @property
    def overhead_us(self) -> Optional[int]:
        if self.duration_us is None:
            return None
        return self.duration_us - self.llm_us - self.tool_us

    @property
    def model_call_count(self) -> int:
        return sum(1 for s in self.spans if s.category == LLM_CATEGORY)

    @property
    def last_activity_us(self) -> int:
        """Timestamp of the last event seen in this turn — distinguishes a
        genuinely running turn from one whose end mark never arrived."""
        edges = [self.start_us]
        if self.end_us is not None:
            edges.append(self.end_us)
        edges.extend(s.start_us for s in self.spans)
        edges.extend(s.end_us for s in self.spans if s.end_us is not None)
        edges.extend(m.timestamp_us for m in self.marks)
        return max(edges)


@dataclass
class Session:
    session_id: str
    turns: List[Turn] = field(default_factory=list)
    unassigned_spans: List[Span] = field(default_factory=list)
    unassigned_marks: List[AtofEvent] = field(default_factory=list)
    first_us: Optional[int] = None
    last_us: Optional[int] = None

    def _saw(self, timestamp_us: int) -> None:
        if self.first_us is None or timestamp_us < self.first_us:
            self.first_us = timestamp_us
        if self.last_us is None or timestamp_us > self.last_us:
            self.last_us = timestamp_us


@dataclass
class Assembly:
    sessions: List[Session]        # most recent activity first
    anomalies: List[Anomaly]


def _pair_spans(events, anomalies):
    """Match scope start/end events by uuid; also map session-scope uuids.

    Returns (spans by uuid in start order, {agent scope uuid: session_id}).
    """
    spans: dict = {}
    scope_sessions: dict = {}
    for event in events:
        if event.kind != "scope":
            continue
        if event.is_scope_start:
            if event.uuid in spans:
                anomalies.append(Anomaly(
                    f"duplicate scope start for uuid {event.uuid!r}", event.line_no))
                continue
            spans[event.uuid] = Span(
                uuid=event.uuid,
                name=event.name,
                category=event.category,
                session_id="",       # resolved later
                parent_uuid=event.parent_uuid,
                start_us=event.timestamp_us,
                end_us=None,
                metadata=event.metadata,
                start_data=event.data,
                end_data=None,
                model_name=event.model_name,
                tool_call_id=event.tool_call_id,
                api_request_id=event.api_request_id,
                turn_id=event.turn_id,
                line_no=event.line_no,
            )
            if event.category == AGENT_CATEGORY:
                session_id = event.session_id
                if not session_id and isinstance(event.data, dict):
                    session_id = event.data.get("session_id")
                if session_id:
                    scope_sessions[event.uuid] = session_id
        else:
            span = spans.get(event.uuid)
            if span is None:
                anomalies.append(Anomaly(
                    f"scope end without start for uuid {event.uuid!r}", event.line_no))
                continue
            if span.end_us is not None:
                anomalies.append(Anomaly(
                    f"duplicate scope end for uuid {event.uuid!r}", event.line_no))
                continue
            span.end_us = event.timestamp_us
            span.end_data = event.data
            # end events may carry metadata the start lacked
            span.metadata = {**event.metadata, **span.metadata}
    return spans, scope_sessions


def _build_turns(session: Session, boundary_marks, anomalies) -> None:
    current: Optional[Turn] = None
    for mark in boundary_marks:
        if mark.name == TURN_START_MARK:
            if current is not None:
                anomalies.append(Anomaly(
                    f"turn started before previous turn ended in session "
                    f"{session.session_id!r}", mark.line_no))
                session.turns.append(current)
            current = Turn(
                session_id=session.session_id,
                turn_id=mark.turn_id,
                start_us=mark.timestamp_us,
                user_message=_user_message(mark.data),
            )
        else:  # TURN_END_MARK
            if current is None:
                anomalies.append(Anomaly(
                    f"turn end without turn start in session "
                    f"{session.session_id!r}", mark.line_no))
                continue
            current.end_us = mark.timestamp_us
            if current.turn_id is None:
                current.turn_id = mark.turn_id
            session.turns.append(current)
            current = None
    if current is not None:   # still in flight (or the end mark was lost)
        session.turns.append(current)


def _containing_turn(session: Session, timestamp_us: int) -> Optional[Turn]:
    """The turn whose interval holds the timestamp.

    An unended turn's interval runs until the next turn's start (or forever
    if it is the last one).
    """
    for i, turn in enumerate(session.turns):
        if timestamp_us < turn.start_us:
            return None
        end = turn.end_us
        if end is None:
            end = (
                session.turns[i + 1].start_us
                if i + 1 < len(session.turns)
                else None
            )
        if end is None or timestamp_us <= end:
            return turn
    return None


def _turn_for(session: Session, turn_id: Optional[str], timestamp_us: int) -> Optional[Turn]:
    if turn_id is not None:
        for turn in session.turns:
            if turn.turn_id == turn_id:
                return turn
    return _containing_turn(session, timestamp_us)


def assemble(events: Iterable[AtofEvent]) -> Assembly:
    """Build the session/turn/span waterfall model from parsed events."""
    anomalies: List[Anomaly] = []
    # Defensive sort: the exporter appends in near-real-time order, but the
    # model must not depend on it. line_no breaks timestamp ties.
    ordered = sorted(events, key=lambda e: (e.timestamp_us, e.line_no))

    spans, scope_sessions = _pair_spans(ordered, anomalies)

    sessions: dict = {}

    def session_for(session_id: str) -> Session:
        if session_id not in sessions:
            sessions[session_id] = Session(session_id=session_id)
        return sessions[session_id]

    # Turns first: boundary marks define the intervals spans land in.
    for event in ordered:
        if event.is_mark:
            session_id = event.session_id or scope_sessions.get(event.parent_uuid) \
                or UNKNOWN_SESSION
            session_for(session_id)._saw(event.timestamp_us)
    for session in sessions.values():
        boundary = [
            e for e in ordered
            if e.is_mark and e.name in (TURN_START_MARK, TURN_END_MARK)
            and (e.session_id or scope_sessions.get(e.parent_uuid) or UNKNOWN_SESSION)
            == session.session_id
        ]
        _build_turns(session, boundary, anomalies)

    # Assign spans (session scopes are containers, not work — skip them,
    # but let them establish their session's activity window).
    for span in spans.values():
        if span.category == AGENT_CATEGORY and span.uuid in scope_sessions:
            session = session_for(scope_sessions[span.uuid])
            session._saw(span.start_us)
            if span.end_us is not None:
                session._saw(span.end_us)
    for span in sorted(spans.values(), key=lambda s: (s.start_us, s.line_no)):
        if span.category == AGENT_CATEGORY:
            continue
        session_id = (
            span.metadata.get("session_id")
            or scope_sessions.get(span.parent_uuid)
            or UNKNOWN_SESSION
        )
        session = session_for(session_id)
        span.session_id = session_id
        session._saw(span.start_us)
        if span.end_us is not None:
            session._saw(span.end_us)
        turn = _turn_for(session, span.turn_id, span.start_us)
        if turn is not None:
            turn.spans.append(span)
        else:
            session.unassigned_spans.append(span)
            anomalies.append(Anomaly(
                f"span {span.name!r} ({span.uuid}) falls outside every turn "
                f"in session {session_id!r}", span.line_no))

    # Remaining marks (approvals, subagent events, …) attach by time.
    for event in ordered:
        if not event.is_mark or event.name in (TURN_START_MARK, TURN_END_MARK):
            continue
        session_id = event.session_id or scope_sessions.get(event.parent_uuid) \
            or UNKNOWN_SESSION
        session = session_for(session_id)
        turn = _turn_for(session, event.turn_id, event.timestamp_us)
        if turn is not None:
            turn.marks.append(event)
        else:
            session.unassigned_marks.append(event)

    return Assembly(
        sessions=sorted(
            sessions.values(),
            key=lambda s: s.last_us if s.last_us is not None else 0,
            reverse=True,
        ),
        anomalies=anomalies,
    )
