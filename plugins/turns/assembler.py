"""ATOF assembler — the top layer of the reader (docs/design/adr/0002).

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
- hermes' core runtime describes the same turns a second way, as a scope
  tree (see TURN_SCOPE below). Spans reach their turn by walking that tree,
  which is the only thing that places an llm span — those carry no turn_id.
  A turn scope is merged into the Turn its marks already built rather than
  becoming a second one;
- overhead is the residual: turn duration minus provider (llm) time minus
  tool time. It is reported as-is, even negative — a negative residual
  means spans overlap and should be seen, not clamped away.

Anything that does not assemble cleanly (end without start, turn.end
without turn.start, spans outside any turn) is kept and surfaced, never
dropped silently — the ADR 2 loud-failure rule.

What a span *says* is not here: `spans.py` holds the Span itself and every
reading of a hermes payload, and this module imports it. The split is by what
a question needs to answer it — one span's own payloads, or several events —
and the dependency runs one way, so a Span never knows its turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from plugins.turns.atof_reader import STREAM_MARK_NAMES, AtofEvent
from providers import chunk_usage
from plugins.turns.spans import (AGENT_CATEGORY, LLM_CATEGORY, TOOL_CATEGORY,
                                   Span, _mark_user_message)

TURN_START_MARK = "hermes.turn.start"
TURN_END_MARK = "hermes.turn.end"
SUBAGENT_START_MARK = "hermes.subagent.start"
SUBAGENT_STOP_MARK = "hermes.subagent.stop"
UNKNOWN_SESSION = "(unknown session)"

# --- the core runtime's turn tree ----------------------------------------
# The nemo_relay plugin bounded a turn with a pair of marks. The core
# runtime that replaced it (hermes 2026-07-19) emits no such marks; it emits
# a scope tree instead, and the turn is a scope in it:
#
#   agent scope                  the execution surface; empty payload
#     └── hermes.turn            start/end pair, {"outcome": …} on the end
#           ├── hermes.logical_llm_call     one logical call…
#           │     └── llm scope             …and its physical attempt
#           └── tool scopes
#
# So a turn's extent is observed again rather than inferred, and with it the
# overhead residual. Two consequences the assembler has to honour:
#
# - `hermes.logical_llm_call` has category "function", so it counts as
#   neither llm nor tool time while wrapping something that does. Left in,
#   every turn double-counts its model time. It is a container, not work.
# - The turn scope carries no turn_id and no user_message, so a turn takes
#   its session from the spans beneath it and its prompt from the request
#   that opened its first llm call.
TURN_SCOPE = "hermes.turn"
LOGICAL_LLM_SCOPE = "hermes.logical_llm_call"


# The observed tree is turn > logical_llm_call > llm — two hops. The bound
# is not about depth but about malformed data: a parent cycle must not hang
# a request.
MAX_PARENT_WALK = 8



@dataclass
class Anomaly:
    message: str
    line_no: Optional[int] = None



@dataclass
class Turn:
    session_id: str
    turn_id: Optional[str]
    start_us: int
    end_us: Optional[int] = None    # None if the end mark never arrived
    # A later turn started in this session while this one was still open: a
    # session runs one turn at a time, so this turn is over even though its
    # end mark never arrived. No end_us is invented for it — the duration
    # was never observed and is not ours to guess (ADR 2).
    superseded: bool = False
    user_message: Optional[str] = None
    spans: List[Span] = field(default_factory=list)
    marks: List[AtofEvent] = field(default_factory=list)

    @property
    def duration_us(self) -> Optional[int]:
        if self.end_us is None:
            return None
        return self.end_us - self.start_us

    @property
    def is_live(self) -> bool:
        """Still running: open, and not proven finished by a later turn.

        An open turn is not evidence of work in progress — hermes drops
        turn.end marks often enough that unclosed turns pile up.
        """
        return self.end_us is None and not self.superseded

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
    def timeline(self) -> List[Any]:
        """Spans and non-boundary marks merged in time order — the turn
        page's waterfall rows; marks render as zero-width ticks."""
        return sorted(
            [*self.spans, *self.marks],
            key=lambda e: e.timestamp_us if e.is_mark else e.start_us,
        )

    # Each subagent gets a small per-turn tag (#1, #2, … in start-mark
    # order) shown on both its start and stop rows so the pair can be
    # matched by eye; stops correlate back via child_session_id, the only
    # key present on both marks.
    @property
    def subagents(self) -> dict:
        out = {}
        for m in sorted(self.marks, key=lambda m: m.timestamp_us):
            sid = m.child_session_id
            if m.name == SUBAGENT_START_MARK and sid and sid not in out:
                out[sid] = {"ordinal": len(out) + 1, "goal": m.child_goal}
        return out

    def subagent_ordinal(self, mark) -> Optional[int]:
        entry = self.subagents.get(mark.child_session_id)
        return entry["ordinal"] if entry else None

    def subagent_goal(self, mark) -> Optional[str]:
        entry = self.subagents.get(mark.child_session_id)
        return entry["goal"] if entry else None

    @property
    def last_activity_us(self) -> int:
        """Timestamp of the last event seen in this turn — distinguishes a
        genuinely running turn from one whose end mark never arrived."""
        edges = [self.start_us]
        if self.end_us is not None:
            edges.append(self.end_us)
        # Span.last_activity_us folds in the last streamed chunk, which is
        # the only sign of life a long model call gives while it is open.
        edges.extend(s.last_activity_us for s in self.spans)
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

    @property
    def finished_subagent_sessions(self) -> set:
        """Sessions a parent has reported as stopped, via subagent.stop.

        A subagent's own session often never emits hermes.turn.end, leaving
        its turn open forever; the parent's stop mark is the authoritative
        "this agent is done" signal, and needs no staleness clock.
        """
        stopped = set()
        for session in self.sessions:
            marks = [m for turn in session.turns for m in turn.marks]
            marks.extend(session.unassigned_marks)
            for mark in marks:
                if mark.name == SUBAGENT_STOP_MARK and mark.child_session_id:
                    stopped.add(mark.child_session_id)
        return stopped


def _pair_spans(events, anomalies, stream_activity=None, stream_usage=None):
    """Match scope start/end events by uuid; also map session-scope uuids.

    Returns (spans by uuid in start order, {agent scope uuid: session_id}).

    `stream_activity` and `stream_usage` map a span's uuid to the timestamp
    of the last `llm.chunk` seen beneath it and to the token counts that
    chunk reported — see `assemble`.
    """
    stream_activity = stream_activity or {}
    stream_usage = stream_usage or {}
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
                category_profile=event.category_profile,
                start_data=event.data,
                end_data=None,
                model_name=event.model_name,
                tool_call_id=event.tool_call_id,
                api_request_id=event.api_request_id,
                turn_id=event.turn_id,
                line_no=event.line_no,
                start_ref=event.payload_ref,
                projection=dict(event.projection),
                payload_elided=event.payload_elided,
                stream_last_us=stream_activity.get(event.uuid),
                stream_usage=stream_usage.get(event.uuid),
            )
            if event.category == AGENT_CATEGORY:
                session_id = event.session_id or event.projected("data_session_id")
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
            span.end_ref = event.payload_ref
            span.payload_elided = span.payload_elided or event.payload_elided
            if event.projection:
                span.projection = {**span.projection, **event.projection}
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
                current.superseded = True
                session.turns.append(current)
            current = Turn(
                session_id=session.session_id,
                turn_id=mark.turn_id,
                start_us=mark.timestamp_us,
                user_message=_mark_user_message(mark),
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


def _enclosing_turn_uuid(span, spans: dict, turn_uuids) -> Optional[str]:
    """The uuid of the turn scope a span sits under, walking parent_uuid.

    Returns None for a span outside any turn scope — including the turn
    scopes and session scopes themselves, which have no turn above them.
    """
    seen = set()
    current = span.parent_uuid
    for _ in range(MAX_PARENT_WALK):
        if current is None or current in seen:
            return None
        if current in turn_uuids:
            return current
        seen.add(current)
        parent = spans.get(current)
        if parent is None:
            return None
        current = parent.parent_uuid
    return None


def _overlaps(turn: Turn, scope) -> bool:
    """Whether a turn and a turn scope describe the same stretch of time.

    An unfinished side is treated as still running, which is what it claims
    to be. Turns within a session do not overlap each other, so this picks
    out at most one.
    """
    turn_end = turn.end_us if turn.end_us is not None else float("inf")
    scope_end = scope.end_us if scope.end_us is not None else float("inf")
    return turn.start_us < scope_end and scope.start_us < turn_end


def _matching_turn(session: Session, scope, turn_ids, taken) -> Optional[Turn]:
    """The already-built turn a turn scope is a second account of, if any.

    The two exporters can both be live — the plugin still emits its marks
    while the core runtime emits this scope tree — and then each real turn
    is described twice, a few milliseconds apart. Building both produces a
    duplicate row whose spans all went to the other one.

    The exact key is `turn_id`: every span under a turn scope that carries
    one carries the *same* one, and it is the id the marks use. Overlap is
    the fallback for a turn whose spans carry no turn_id at all — one with
    only llm calls under it, which have none.
    """
    # `taken` holds ids, not Turns: Turn is a plain dataclass, so `in` would
    # deep-compare every span payload it carries — megabytes per turn.
    for turn in session.turns:
        if id(turn) in taken:
            continue
        if turn.turn_id is not None and turn.turn_id in turn_ids:
            return turn
    for turn in session.turns:
        if id(turn) not in taken and _overlaps(turn, scope):
            return turn
    return None


def _build_scope_turns(spans: dict, turn_uuids, session_for, anomalies) -> dict:
    """Map each `hermes.turn` scope to the Turn it describes.

    Where the marks already built that turn, this returns the existing one —
    the mark is the better account of *what* the turn was (it carries the
    session, the turn_id and hermes' own unwrapped `user_message`), while
    the scope is the better account of *what ran inside it*, which is the
    tree spans are attached by. Only a scope with no mark behind it becomes
    a Turn of its own, prompt reconstructed from its first llm request.
    """
    children = {uuid: [] for uuid in turn_uuids}
    for span in sorted(spans.values(), key=lambda s: (s.start_us, s.line_no)):
        owner = _enclosing_turn_uuid(span, spans, turn_uuids)
        if owner is not None:
            children[owner].append(span)

    # A turn's session, from the spans under it. A turn that ran no span
    # yet — one just started — names nobody, so the agent scope above is
    # asked next: an agent scope is one session by construction, and its
    # other turns have already said which. Without that, a turn opened at
    # the moment of reading would strand itself in (unknown session) and
    # then fail to recognize the mark that describes it.
    own_session = {}
    for uuid in turn_uuids:
        own_session[uuid] = next(
            (s.metadata.get("session_id") for s in children[uuid]
             if s.metadata.get("session_id")),
            None,
        )
    agent_session = {}
    for uuid, session_id in own_session.items():
        parent = spans[uuid].parent_uuid
        if session_id and parent and parent not in agent_session:
            agent_session[parent] = session_id

    turns, taken = {}, set()
    for uuid in sorted(turn_uuids, key=lambda u: spans[u].start_us):
        scope = spans[uuid]
        own = children[uuid]
        session_id = (own_session[uuid]
                      or agent_session.get(scope.parent_uuid))
        if session_id is None:
            session_id = UNKNOWN_SESSION
            anomalies.append(Anomaly(
                f"turn scope {uuid} has no span naming its session",
                scope.line_no))
        session = session_for(session_id)
        turn_ids = {s.turn_id for s in own if s.turn_id}

        existing = _matching_turn(session, scope, turn_ids, taken)
        if existing is not None:
            # One turn, two accounts of it. Take the union of the two
            # intervals: they bracket the same work a few ms apart, and the
            # union is the span that certainly contains it — which matters
            # because the waterfall lays its spans out from the turn's own
            # start.
            existing.start_us = min(existing.start_us, scope.start_us)
            if existing.end_us is not None and scope.end_us is not None:
                existing.end_us = max(existing.end_us, scope.end_us)
            elif scope.end_us is None:
                existing.end_us = None
            turns[uuid] = existing
            taken.add(id(existing))
            continue

        prompt = next((s.request_prompt for s in own
                       if s.category == LLM_CATEGORY and s.request_prompt), None)
        turn = Turn(
            session_id=session_id,
            turn_id=next(iter(turn_ids)) if len(turn_ids) == 1 else None,
            start_us=scope.start_us,
            end_us=scope.end_us,
            user_message=prompt,
        )
        session.turns.append(turn)
        turns[uuid] = turn
        taken.add(id(turn))
    return turns


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


def assemble(events: Iterable[AtofEvent],
             stream_activity: Optional[dict] = None,
             stream_usage: Optional[dict] = None) -> Assembly:
    """Build the session/turn/span waterfall model from parsed events.

    `stream_activity` and `stream_usage` map a span uuid to the timestamp of
    the last `llm.chunk` emitted beneath it and to the token counts that
    chunk carried. The index passes both because it does not carry chunks as
    events (ADR 11); a caller assembling parsed lines directly passes
    neither, and the chunks are simply in `events` — where the sweep below
    reads the same counts back out, so both paths see one model.
    """
    anomalies: List[Anomaly] = []
    # Defensive sort: the exporter appends in near-real-time order, but the
    # model must not depend on it. line_no breaks timestamp ties.
    ordered = sorted(events, key=lambda e: (e.timestamp_us, e.line_no))

    stream_usage = dict(stream_usage or {})
    for event in ordered:
        if event.is_mark and event.name in STREAM_MARK_NAMES and event.parent_uuid:
            counts = chunk_usage(event.data.get("usage")
                                 if isinstance(event.data, dict) else None)
            if counts:      # only the stream's last chunk carries any
                stream_usage[event.parent_uuid] = counts

    spans, scope_sessions = _pair_spans(ordered, anomalies, stream_activity,
                                        stream_usage)

    # The core runtime's turn tree (see TURN_SCOPE). Both eras can appear in
    # one file, so these are simply empty when only the plugin wrote it.
    turn_uuids = {u for u, s in spans.items() if s.name == TURN_SCOPE}
    # Containers, not work: a wrapper's duration is its child's, counted
    # twice if it is left in, and its category is neither llm nor tool so it
    # would land wholly in overhead.
    container_uuids = turn_uuids | {
        u for u, s in spans.items() if s.name == LOGICAL_LLM_SCOPE}

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
    for session in list(sessions.values()):
        boundary = [
            e for e in ordered
            if e.is_mark and e.name in (TURN_START_MARK, TURN_END_MARK)
            and (e.session_id or scope_sessions.get(e.parent_uuid) or UNKNOWN_SESSION)
            == session.session_id
        ]
        _build_turns(session, boundary, anomalies)

    # Then the turn scopes, which mostly recognize a turn the marks already
    # built rather than adding one. Runs after `_build_turns` for exactly
    # that reason — there has to be something to recognize.
    scope_turns = _build_scope_turns(spans, turn_uuids, session_for, anomalies)

    for session in sessions.values():
        session.turns.sort(key=lambda t: t.start_us)
        # An open turn with a later turn behind it in the same session is
        # over, whatever its missing end mark says — one turn at a time.
        for earlier, later in zip(session.turns, session.turns[1:]):
            if earlier.end_us is None and later.start_us >= earlier.start_us:
                earlier.superseded = True

    # Assign spans (session scopes are containers, not work — skip them,
    # but let them establish their session's activity window).
    for span in spans.values():
        if span.category == AGENT_CATEGORY and span.uuid in scope_sessions:
            session = session_for(scope_sessions[span.uuid])
            session._saw(span.start_us)
            if span.end_us is not None:
                session._saw(span.end_us)
    for span in sorted(spans.values(), key=lambda s: (s.start_us, s.line_no)):
        if span.category == AGENT_CATEGORY or span.uuid in container_uuids:
            continue
        owner = scope_turns.get(_enclosing_turn_uuid(span, spans, turn_uuids))
        session_id = (
            span.metadata.get("session_id")
            or scope_sessions.get(span.parent_uuid)
            or (owner.session_id if owner is not None else None)
            or UNKNOWN_SESSION
        )
        session = session_for(session_id)
        span.session_id = session_id
        session._saw(span.start_us)
        if span.end_us is not None:
            session._saw(span.end_us)
        # The scope tree is a statement of ownership by the exporter, so it
        # beats matching on turn_id or falling back to time containment.
        turn = owner if owner is not None else _turn_for(
            session, span.turn_id, span.start_us)
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
