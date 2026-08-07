"""The ATOF index — a rebuildable cache of the log (docs/design/adr/0011).

The log outgrew being held in memory: 1.19 GB across 281,490 lines, of which
5,319 lines carry 85% of the bytes, because an llm request repeats the whole
conversation and a turn mark repeats it again. What will not fit is the
payloads, and payloads are what a page needs least of — a turn list needs
none, a turn page needs the fifty belonging to one turn.

So this module stores a **projection** of each event — where its line is,
its spine, its correlation envelope, and the two payload-derived strings
assembly cannot proceed without — and leaves the payloads in the log, to be
read back by byte offset when a page actually renders one (`hydrate_turn`).

It is a cache and never a source of truth. Everything in it is derived from
the log, a full rebuild takes seconds, and there is no migration path: a
doubtful index is deleted, not upgraded. Every check in `_staleness` is
therefore biased towards rebuilding.

`llm.chunk` marks are 94% of the log's lines and reach no template — they
carry no session or turn id, so today they do not even reach a turn. They
are folded into one row per streaming llm span, holding the two things they
can say: that a long model call is still alive, and what it cost in tokens.
The second is not a nicety — providers on the chat-completions route report
usage *only* on the stream's final chunk and leave the span's own end
payload with a null `usage`, so without it those calls show no token counts
at all.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from plugins.turns import assembler as _assembler
from plugins.turns import atof_reader as _atof_reader
from plugins.turns import providers as _providers
from plugins.turns.assembler import (AGENT_CATEGORY, LLM_CATEGORY,
                                       TURN_START_MARK,
                                       request_prompt_from_profile,
                                       user_message_from_data)
from plugins.turns.atof_reader import (STREAM_MARK_NAMES, AtofEvent,
                                         AtofParseError, LineRef, ParseError,
                                         parse_line)
from plugins.turns.providers import chunk_usage
from plugins.turns.tailer import (file_size, read_at, read_bytes_at,
                                    read_lines)

# Bump when the stored *shape* changes. The code fingerprint below catches
# changes in what the stored values mean; this catches changes in which
# columns exist, which no fingerprint would notice until a query failed.
INDEX_VERSION = 2

# Head of the log hashed as a fingerprint. An append-only file never changes
# here, so a mismatch means the file was rewritten in place — which `>` on
# an open path does without changing the inode.
HEAD_FINGERPRINT_BYTES = 64 * 1024

# A payload value at or below this is small enough to keep in the index; a
# larger one stays in the log and is read back on demand. The threshold is
# on size, not on key names: this app is not the authority on which of
# hermes' payload keys are the big ones, and the answer changes per tool.
PAYLOAD_INLINE_MAX_BYTES = 4096

# Rows per executemany during a build.
_BATCH = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    line_no INTEGER PRIMARY KEY,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL,
    kind TEXT NOT NULL,
    scope_category TEXT,
    uuid TEXT NOT NULL,
    parent_uuid TEXT,
    timestamp_us INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    atof_version TEXT,
    schema TEXT NOT NULL,
    attributes TEXT NOT NULL,
    metadata TEXT NOT NULL,
    category_profile TEXT NOT NULL,
    data TEXT,
    payload_elided INTEGER NOT NULL,
    projection TEXT
);
CREATE TABLE IF NOT EXISTS parse_errors (
    line_no INTEGER PRIMARY KEY,
    message TEXT NOT NULL,
    line_preview TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_activity (
    uuid TEXT PRIMARY KEY,
    last_us INTEGER NOT NULL,
    chunks INTEGER NOT NULL,
    usage TEXT
);
"""


# --- where the index lives ------------------------------------------------

def default_index_path(log_path: str) -> str:
    """A cache file of our own, named after the log it indexes.

    Never beside the log: that directory is hermes'. Keyed by the log's
    absolute path so two configured logs do not share one index, and two
    observers reading the same log do share it.
    """
    base = (os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    digest = hashlib.sha256(os.path.abspath(log_path).encode()).hexdigest()[:16]
    return os.path.join(base, "hermes-observer", f"atof-index-{digest}.sqlite3")


def code_fingerprint(usage_shapes=None) -> str:
    """Hash of every module whose code decides what the index *means*.

    The stored spine is normalized by `atof_reader`, read per provider by
    `providers`, and projected through `assembler`; edit any of them and the
    stored values no longer mean what they say. Hashing the source
    invalidates them automatically, rather than relying on someone
    remembering to bump a constant while chasing a hermes schema change. It
    over-invalidates — a docstring edit forces a rebuild — which at seconds
    per rebuild is the right side to be wrong on.

    **Contributed provider modules are hashed too** (ADR 13). A third-party
    `UsageShape` decides what the stored token counts mean just as much as
    this tree does, so editing one has to invalidate an index built before
    the edit. Without this the counts would be stale in exactly the way a
    cache is never allowed to be (ADR 11).
    """
    digest = hashlib.sha256()
    modules = [_atof_reader, _providers, _assembler, sys.modules[__name__]]
    for name in _providers.shape_modules(usage_shapes or ()):
        module = sys.modules.get(name)
        if module is not None and module not in modules:
            modules.append(module)
    for module in modules:
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:          # installed without sources: fall back to
            digest.update(b"?")  # INDEX_VERSION alone
    return digest.hexdigest()


# --- projections ----------------------------------------------------------

def _json_len(value: Any) -> Tuple[str, int]:
    text = json.dumps(value, default=str)
    return text, len(text)


def _trim_payload(value: Any) -> Tuple[Optional[str], bool]:
    """(JSON text to store, elided?) for one event's `data`.

    Small payloads are kept whole, so the subagent marks the turn *list*
    reads — and any other small payload — need no trip to the log. A large
    one is dropped down to whichever of its keys are small, which keeps
    short fields like `error` and `child_session_id` reachable while leaving
    the conversation history where it is.
    """
    text, size = _json_len(value)
    if size <= PAYLOAD_INLINE_MAX_BYTES:
        return text, False
    if isinstance(value, dict):
        kept = {k: v for k, v in value.items()
                if _json_len(v)[1] <= PAYLOAD_INLINE_MAX_BYTES}
        return json.dumps(kept, default=str), True
    return None, True


def _trim_profile(profile: dict) -> Tuple[str, bool]:
    """The category_profile with its large values left in the log.

    `annotated_request` and `annotated_response` are 17 KB on average and
    299 MB across the log; `model_name` and `tool_call_id` are the rest, and
    31 bytes.
    """
    kept, elided = {}, False
    for key, value in profile.items():
        if _json_len(value)[1] <= PAYLOAD_INLINE_MAX_BYTES:
            kept[key] = value
        else:
            elided = True
    return json.dumps(kept, default=str), elided


def project(event: AtofEvent) -> dict:
    """Payload facts assembly needs from an event whose payload we drop.

    Deliberately short. Every entry here is a payload key this app has
    decided to know about outside a scope spec, which is the thing ADR 7 and
    the fork test exist to limit — so a fact earns a place only by blocking
    the assembly of turns themselves, not by being interesting to display.
    """
    out: dict = {}
    if event.is_mark and event.name == TURN_START_MARK:
        # hermes' own unwrapped prompt, inside the largest payload in the log
        message = user_message_from_data(event.data)
        if message:
            out["user_message"] = message
    if event.is_scope_start:
        if event.category == LLM_CATEGORY:
            # the reconstruction that names a turn no mark described
            prompt = request_prompt_from_profile(event.category_profile)
            if prompt:
                out["request_prompt"] = prompt
        elif event.category == AGENT_CATEGORY and not event.session_id:
            # an agent scope naming its session in the payload rather than
            # the envelope; without it every span beneath it is orphaned
            data = event.data
            if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                out["data_session_id"] = data["session_id"]
    return out


# --- the index ------------------------------------------------------------

class IndexState:
    """What the last refresh did, for the console and `/_status`."""

    def __init__(self):
        self.action = "none"        # none | unchanged | extended | rebuilt
        self.reason: Optional[str] = None
        self.lines = 0
        self.seconds = 0.0
        self.generation = 0
        self.indexed_bytes = 0
        self.indexed_lines = 0
        self.event_rows = 0


class AtofIndex:
    """Byte-offset index over one ATOF log, cached in SQLite.

    One instance per app; `refresh()` is called at the top of every request
    and is a stat and two small reads when nothing has been appended.
    """

    def __init__(self, log_path: str, db_path: str, usage_shapes=None):
        self.log_path = log_path
        self.db_path = db_path
        # The provider table this index's token counts were read with (ADR
        # 13). Held rather than reached for, because it is also hashed into
        # the fingerprint: an index and the shapes that filled it belong
        # together, and a changed table has to invalidate it.
        self.usage_shapes = tuple(usage_shapes) if usage_shapes else None
        self.state = IndexState()
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        """Make the cache directory, on every open rather than at startup.

        Deleting the index is a supported thing to do — it is a cache, and
        the documentation says so — and people delete the directory, not the
        file. Doing this once in `__init__` meant a live app whose cache
        directory had been removed under it raised `unable to open database
        file` on every subsequent request, having told the reader deleting it
        was safe.
        """
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    @contextmanager
    def _db(self):
        """A connection per operation, committed and closed.

        Flask serves this app threaded and SQLite connections are not
        shareable across threads; opening one per operation is simpler than
        a pool and costs microseconds against work measured in seconds.
        """
        self._ensure_dir()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- reading ---------------------------------------------------------

    def events(self) -> List[AtofEvent]:
        """Every indexed event, payload-free where the payload was large."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY line_no").fetchall()
        return [_event_from_row(row) for row in rows]

    def parse_errors(self) -> List[ParseError]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM parse_errors ORDER BY line_no").fetchall()
        return [ParseError(line_no=r["line_no"], message=r["message"],
                           line_preview=r["line_preview"]) for r in rows]

    def stream_activity(self) -> Dict[str, int]:
        """Span uuid → timestamp of the last chunk streamed beneath it."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT uuid, last_us FROM stream_activity").fetchall()
        return {r["uuid"]: r["last_us"] for r in rows}

    def stream_usage(self) -> Dict[str, dict]:
        """Span uuid → token counts its final streamed chunk reported.

        Separate from `stream_activity` because the two are read for
        different reasons — liveness on every span, counts only on llm ones
        — and only a minority of spans have anything to say here.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT uuid, usage FROM stream_activity "
                "WHERE usage IS NOT NULL").fetchall()
        return {r["uuid"]: json.loads(r["usage"]) for r in rows}

    # --- refreshing ------------------------------------------------------

    def refresh(self) -> IndexState:
        with self._lock:
            started = time.time()
            with self._db() as conn:
                meta = _read_meta(conn)
                reason = self._staleness(conn, meta)
                if reason is not None:
                    state = self._rebuild(conn, reason)
                else:
                    state = self._extend(conn, meta)
            state.seconds = time.time() - started
            self.state = state
            return state

    def _staleness(self, conn, meta) -> Optional[str]:
        """Why this index cannot be trusted, or None. See ADR 11."""
        if not meta:
            return "no index yet"
        if meta.get("index_version") != str(INDEX_VERSION):
            return "index built by a different index version"
        if meta.get("code_fingerprint") != code_fingerprint(self.usage_shapes):
            return "the reader that derived it has changed"

        try:
            stat = os.stat(self.log_path)
        except OSError:
            return "the log is not there"
        if (meta.get("log_dev") != str(stat.st_dev)
                or meta.get("log_ino") != str(stat.st_ino)):
            return "the log is a different file (rotated or replaced)"

        indexed_bytes = int(meta.get("indexed_bytes") or 0)
        if stat.st_size < indexed_bytes:
            return "the log is shorter than the part already indexed"
        if indexed_bytes == 0:
            return None

        head_len = int(meta.get("head_len") or 0)
        try:
            head = read_bytes_at(self.log_path, 0, head_len)
        except OSError:
            return "the log's head could not be re-read"
        if hashlib.sha256(head).hexdigest() != meta.get("head_sha256"):
            return "the log's head has changed (rewritten in place)"

        # The seam: the last line we indexed must still be exactly where and
        # what we recorded. This is the check that survives a rewrite which
        # preserved both the size and the head.
        tail_offset = int(meta.get("tail_offset") or 0)
        tail_length = int(meta.get("tail_length") or 0)
        try:
            tail = read_bytes_at(self.log_path, tail_offset, tail_length)
        except OSError:
            return "the last indexed line could not be re-read"
        if hashlib.sha256(tail).hexdigest() != meta.get("tail_sha256"):
            return "the bytes under the index have moved"
        if not tail.endswith(b"\n"):
            return "the indexed region does not end at a line boundary"
        return None

    def _rebuild(self, conn, reason: str) -> IndexState:
        generation = int((_read_meta(conn).get("generation") or 0)) + 1
        # Dropped and recreated, not emptied. ADR 11's rule is that a
        # doubtful index is deleted rather than upgraded, and one of the
        # reasons we get here is that the stored *shape* is wrong — a new
        # column, say, which `CREATE TABLE IF NOT EXISTS` would not add to a
        # table already standing and `DELETE FROM` would leave missing until
        # the first insert failed. `meta` is kept: it holds the generation.
        for table in ("events", "parse_errors", "stream_activity"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(_SCHEMA)
        state = self._ingest(conn, from_offset=0, from_line_no=1,
                             generation=generation)
        state.action = "rebuilt"
        state.reason = reason
        return state

    def _extend(self, conn, meta) -> IndexState:
        indexed_bytes = int(meta.get("indexed_bytes") or 0)
        if file_size(self.log_path) == indexed_bytes:
            state = IndexState()
            state.action = "unchanged"
            _fill_state(state, conn, meta)
            return state
        state = self._ingest(
            conn,
            from_offset=indexed_bytes,
            from_line_no=int(meta.get("indexed_lines") or 0) + 1,
            generation=int(meta.get("generation") or 1),
            carry=meta,
        )
        state.action = "extended" if state.lines else "unchanged"
        return state

    def _ingest(self, conn, from_offset: int, from_line_no: int,
                generation: int, carry: Optional[dict] = None) -> IndexState:
        """Read forward from `from_offset`, writing rows as we go.

        Streams: a full rebuild of a multi-gigabyte log never holds more than
        one read chunk plus one batch of rows.
        """
        rows: List[tuple] = []
        errors: List[tuple] = []
        stream: Dict[str, List[int]] = {}
        line_no = from_line_no - 1
        consumed = from_offset
        last: Optional[Tuple[int, int]] = None

        for offset, length, text in read_lines(self.log_path, from_offset):
            line_no += 1
            consumed = offset + length
            last = (offset, length)
            if not text.strip():
                continue
            try:
                event = parse_line(text, line_no, self.usage_shapes)
            except AtofParseError as exc:
                errors.append((line_no, exc.message,
                               text.strip()[:_atof_reader._ERROR_LINE_PREVIEW_CHARS]))
                continue
            if event.name in STREAM_MARK_NAMES:
                parent = event.parent_uuid
                if parent:
                    # Only the stream's last chunk reports usage, so the
                    # last non-empty one seen wins and the rest, which are
                    # all of them, leave it alone.
                    counts = chunk_usage(event.data.get("usage")
                                         if isinstance(event.data, dict) else None)
                    entry = stream.get(parent)
                    if entry is None:
                        stream[parent] = [event.timestamp_us, 1, counts or None]
                    else:
                        entry[0] = max(entry[0], event.timestamp_us)
                        entry[1] += 1
                        if counts:
                            entry[2] = counts
                continue
            rows.append(_row_from_event(event, offset, length))
            if len(rows) >= _BATCH:
                _write_events(conn, rows)
                rows = []
        _write_events(conn, rows)
        _write_errors(conn, errors)
        _write_stream(conn, stream)

        state = IndexState()
        state.lines = line_no - (from_line_no - 1)
        state.generation = generation
        # Only complete lines were consumed, so the cursor always lands on a
        # newline; `last` is None when nothing was appended.
        if last is None and carry is not None:
            state.indexed_bytes = int(carry.get("indexed_bytes") or 0)
            state.indexed_lines = int(carry.get("indexed_lines") or 0)
            tail_offset = int(carry.get("tail_offset") or 0)
            tail_length = int(carry.get("tail_length") or 0)
            tail_sha = carry.get("tail_sha256") or ""
            head_len = int(carry.get("head_len") or 0)
            head_sha = carry.get("head_sha256") or ""
        else:
            state.indexed_bytes = consumed
            state.indexed_lines = line_no
            tail_offset, tail_length = last if last else (0, 0)
            tail_sha = (hashlib.sha256(
                read_bytes_at(self.log_path, tail_offset, tail_length)).hexdigest()
                if last else "")
            head_len = min(HEAD_FINGERPRINT_BYTES, consumed)
            head_sha = (hashlib.sha256(
                read_bytes_at(self.log_path, 0, head_len)).hexdigest()
                if head_len else "")

        try:
            stat = os.stat(self.log_path)
            dev, ino = stat.st_dev, stat.st_ino
        except OSError:
            dev = ino = 0
        _write_meta(conn, {
            "index_version": str(INDEX_VERSION),
            "code_fingerprint": code_fingerprint(self.usage_shapes),
            "log_path": os.path.abspath(self.log_path),
            "log_dev": str(dev),
            "log_ino": str(ino),
            "head_len": str(head_len),
            "head_sha256": head_sha,
            "tail_offset": str(tail_offset),
            "tail_length": str(tail_length),
            "tail_sha256": tail_sha,
            "indexed_bytes": str(state.indexed_bytes),
            "indexed_lines": str(state.indexed_lines),
            "generation": str(generation),
        })
        state.event_rows = conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"]
        return state


# --- row <-> event --------------------------------------------------------

_EVENT_COLUMNS = (
    "line_no", "offset", "length", "kind", "scope_category", "uuid",
    "parent_uuid", "timestamp_us", "name", "category", "atof_version",
    "schema", "attributes", "metadata", "category_profile", "data",
    "payload_elided", "projection",
)


def _row_from_event(event: AtofEvent, offset: int, length: int) -> tuple:
    data_text, data_elided = _trim_payload(event.data)
    profile_text, profile_elided = _trim_profile(event.category_profile)
    projection = project(event)
    return (
        event.line_no, offset, length, event.kind, event.scope_category,
        event.uuid, event.parent_uuid, event.timestamp_us, event.name,
        event.category, event.atof_version, event.schema,
        json.dumps(list(event.attributes), default=str),
        json.dumps(event.metadata, default=str),
        profile_text, data_text,
        1 if (data_elided or profile_elided) else 0,
        json.dumps(projection) if projection else None,
    )


def _event_from_row(row: sqlite3.Row) -> AtofEvent:
    """Rebuild the event. Nothing is re-normalized: the stored envelope is
    already what `parse_line` made of it, schema era and all (ADR 6)."""
    return AtofEvent(
        kind=row["kind"],
        scope_category=row["scope_category"],
        uuid=row["uuid"],
        parent_uuid=row["parent_uuid"],
        timestamp_us=row["timestamp_us"],
        name=row["name"],
        category=row["category"],
        category_profile=json.loads(row["category_profile"]),
        attributes=tuple(json.loads(row["attributes"])),
        data=json.loads(row["data"]) if row["data"] is not None else None,
        metadata=json.loads(row["metadata"]),
        atof_version=row["atof_version"],
        line_no=row["line_no"],
        schema=row["schema"],
        payload_ref=LineRef(row["offset"], row["length"]),
        projection=json.loads(row["projection"]) if row["projection"] else {},
        payload_elided=bool(row["payload_elided"]),
    )


def _write_events(conn, rows) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" * len(_EVENT_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO events ({', '.join(_EVENT_COLUMNS)}) "
        f"VALUES ({placeholders})", rows)


def _write_errors(conn, rows) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO parse_errors (line_no, message, line_preview) "
        "VALUES (?, ?, ?)", rows)


def _write_stream(conn, stream) -> None:
    """Fold this batch of chunks into the one row per span.

    An extend picks up a stream mid-flight, so the row is accumulated rather
    than replaced. `usage` only ever arrives once — on the final chunk — so
    a batch that saw none must not erase one an earlier batch stored.
    """
    if not stream:
        return
    conn.executemany(
        "INSERT INTO stream_activity (uuid, last_us, chunks, usage) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(uuid) DO UPDATE SET "
        "  last_us = max(last_us, excluded.last_us), "
        "  chunks = chunks + excluded.chunks, "
        "  usage = coalesce(excluded.usage, usage)",
        [(uuid, last_us, count, json.dumps(usage) if usage else None)
         for uuid, (last_us, count, usage) in stream.items()])


def _read_meta(conn) -> dict:
    return {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM meta")}


def _write_meta(conn, meta: dict) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        list(meta.items()))


def _fill_state(state: IndexState, conn, meta) -> None:
    state.generation = int(meta.get("generation") or 0)
    state.indexed_bytes = int(meta.get("indexed_bytes") or 0)
    state.indexed_lines = int(meta.get("indexed_lines") or 0)
    state.event_rows = conn.execute(
        "SELECT count(*) AS n FROM events").fetchone()["n"]


# --- hydration ------------------------------------------------------------

def hydrate_turn(turn, log_path: str, usage_shapes=None) -> None:
    """Read one turn's payloads back out of the log, in place.

    The whole point of the index: this runs for the turn being viewed and no
    other. A payload that cannot be re-read — the log rotated between
    assembly and render — leaves `payload_problem` set for the page to state
    rather than a blank field (ADR 2).

    `usage_shapes` has to be the same table the index was built with (ADR
    13): re-reading an llm end payload re-derives its token counts, and a
    hydrated span disagreeing with the indexed one would be this app
    contradicting itself between two renders of the same span.
    """
    for span in turn.spans:
        _hydrate_span(span, log_path, usage_shapes)
    turn.marks[:] = [_hydrated_mark(mark, log_path, usage_shapes)
                     for mark in turn.marks]


def hydrate_span(span, log_path: str, usage_shapes=None) -> None:
    """Read one span's payloads back, for a page showing that span alone.

    `hydrate_turn` is the usual door in; this is the one the full-value page
    (ADR 12) uses, where the whole page is one value of one span and reading
    its forty siblings would be reading a megabyte to render a page that
    does not show them.
    """
    _hydrate_span(span, log_path, usage_shapes)


def _hydrate_span(span, log_path: str, usage_shapes=None) -> None:
    if not span.payload_elided:
        return
    try:
        if span.start_ref is not None:
            start = _reparse(log_path, span.start_ref, span.line_no, usage_shapes)
            span.start_data = start.data
            span.category_profile = start.category_profile
        if span.end_ref is not None:
            span.end_data = _reparse(log_path, span.end_ref, span.line_no,
                                     usage_shapes).data
    except (OSError, AtofParseError) as exc:
        span.payload_problem = str(exc)
        return
    span.payload_elided = False


def _hydrated_mark(mark: AtofEvent, log_path: str, usage_shapes=None) -> AtofEvent:
    if not mark.payload_elided or mark.payload_ref is None:
        return mark
    try:
        fresh = _reparse(log_path, mark.payload_ref, mark.line_no, usage_shapes)
    except (OSError, AtofParseError):
        return mark          # generic_fields shows the trimmed payload
    return dataclasses.replace(mark, data=fresh.data,
                               category_profile=fresh.category_profile,
                               payload_elided=False)


def _reparse(log_path: str, ref: LineRef, line_no: int,
             usage_shapes=None) -> AtofEvent:
    return parse_line(read_at(log_path, ref.offset, ref.length), line_no,
                      usage_shapes)
