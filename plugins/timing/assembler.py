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
SUBAGENT_START_MARK = "hermes.subagent.start"
SUBAGENT_STOP_MARK = "hermes.subagent.stop"
AGENT_CATEGORY = "agent"
LLM_CATEGORY = "llm"
TOOL_CATEGORY = "tool"
UNKNOWN_SESSION = "(unknown session)"


@dataclass
class Anomaly:
    message: str
    line_no: Optional[int] = None


def _as_dict(data: Any) -> Optional[dict]:
    """Payload as a dict when it is one — directly or as a JSON string.
    Data is opaque per the ATOF spec, so never assume shape."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


# V4A patch operation headers, per hermes tools/patch_parser.py
_PATCH_FILE_HEADERS = ("*** Update File:", "*** Add File:",
                       "*** Delete File:", "*** Move File:")


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

    # symmetry with AtofEvent.is_mark so templates branch on one flag
    @property
    def is_mark(self) -> bool:
        return False

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

    def _start_str(self, key: str) -> Optional[str]:
        data = _as_dict(self.start_data)
        if data is not None:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    # terminal tool scopes carry the invocation in their start payload
    @property
    def command(self) -> Optional[str]:
        return self._start_str("command")

    @property
    def workdir(self) -> Optional[str]:
        return self._start_str("workdir")

    # file tool scopes (patch, read_file, write_file, search_files, …)
    # carry the operated-on path in their start payload
    @property
    def path(self) -> Optional[str]:
        return self._start_str("path")

    # patch-mode patch scopes carry no top-level path: the touched files
    # live in the V4A patch text's "*** <Op> File:" headers (see hermes
    # tools/patch_parser.py); a move header keeps its "old -> new" whole
    @property
    def patch_paths(self) -> List[str]:
        if self.name != "patch":
            return []
        text = self._start_str("patch")
        if not text:
            return []
        paths = []
        for line in text.splitlines():
            for header in _PATCH_FILE_HEADERS:
                if line.startswith(header):
                    value = line[len(header):].strip()
                    if value and value not in paths:
                        paths.append(value)
        return paths

    # search_files scopes carry the query in their start payload; "pattern"
    # is too generic a key to trust on other scopes
    @property
    def search_pattern(self) -> Optional[str]:
        return self._start_str("pattern") if self.name == "search_files" else None

    @property
    def file_glob(self) -> Optional[str]:
        return self._start_str("file_glob") if self.name == "search_files" else None

    # web_search and mem0_search scopes carry their search query in the
    # start payload; "query" is too generic a key to trust on other scopes
    @property
    def search_query(self) -> Optional[str]:
        if self.name in ("web_search", "mem0_search"):
            return self._start_str("query")
        return None

    # mem0_add scopes carry the remembered fact in their start payload;
    # "content" is too generic a key to trust on other scopes
    @property
    def memory_content(self) -> Optional[str]:
        return self._start_str("content") if self.name == "mem0_add" else None

    # execute_code scopes carry the program in their start payload; the
    # first line stands in for it inline, the full text goes in the title
    @property
    def code(self) -> Optional[str]:
        return self._start_str("code") if self.name == "execute_code" else None

    @property
    def code_first_line(self) -> Optional[str]:
        code = self.code
        return code.split("\n", 1)[0] if code else None

    # web_extract scopes carry a list of target urls in their start payload
    @property
    def web_extract_urls(self) -> list:
        if self.name != "web_extract":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        urls = data.get("urls")
        if not isinstance(urls, list):
            return []
        return [u for u in urls if isinstance(u, str) and u]

    # todo scopes carry the full task list in their start payload; a call
    # without "todos" is a read, and merge-mode items may omit content —
    # both render nothing rather than erroring
    @property
    def todo_contents(self) -> list:
        if self.name != "todo":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        todos = data.get("todos")
        if not isinstance(todos, list):
            return []
        return [
            t["content"] for t in todos
            if isinstance(t, dict)
            and isinstance(t.get("content"), str) and t["content"]
        ]

    # delegate_task scopes carry the subagent briefs in their start
    # payload — batch mode as a "tasks" list of {goal, context} dicts,
    # single mode as top-level goal/context keys
    @property
    def delegate_tasks(self) -> list:
        if self.name != "delegate_task":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            tasks = [data]
        out = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            goal = t.get("goal")
            if not (isinstance(goal, str) and goal):
                continue
            context = t.get("context")
            out.append({
                "goal": goal,
                "context": context if isinstance(context, str) and context else None,
            })
        return out

    @property
    def delegate_goals(self) -> list:
        return [t["goal"] for t in self.delegate_tasks]

    # skill scopes (skill_view/skill_manage) describe the skill touched in
    # their start payload — name, optional file within the skill, and for
    # skill_manage the action; the keys are too generic to trust elsewhere
    @property
    def _is_skill_scope(self) -> bool:
        return self.name in ("skill_view", "skill_manage")

    @property
    def skill_name(self) -> Optional[str]:
        return self._start_str("name") if self._is_skill_scope else None

    @property
    def skill_file_path(self) -> Optional[str]:
        return self._start_str("file_path") if self._is_skill_scope else None

    @property
    def skill_action(self) -> Optional[str]:
        return self._start_str("action") if self.name == "skill_manage" else None

    @property
    def skill_category(self) -> Optional[str]:
        # only skill_manage "create" payloads carry a category; absent on
        # patch/write_file, so this is None for those actions
        return self._start_str("category") if self.name == "skill_manage" else None


def _user_message(data: Any) -> Optional[str]:
    """The prompt from a turn-start mark's payload."""
    data = _as_dict(data)
    if data is not None:
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
                current.superseded = True
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
