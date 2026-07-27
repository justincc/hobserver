"""Memory view — browses jmem0_logged.db, the mem0 event log.

Moved verbatim from the pre-plugin app.py; the database is always opened
read-only so it is safe to point at the live log while hermes is writing.
"""

import json
import os
import sqlite3

from flask import (Blueprint, abort, current_app, g, redirect, render_template,
                   request, url_for)

bp = Blueprint("memory", __name__)
TAB_LABEL = "Memory"

# Columns shown as metadata on the detail page; everything except the
# query/result pair, which get their own sections.
METADATA_COLUMNS = (
    "id",
    "ts_utc",
    "ts_epoch",
    "event_type",
    "session_id",
    "platform",
    "result_len",
    "memory_count",
    "elapsed_ms",
    "extra",
)


def prettify_result(text):
    """Pretty-print a result blob if it is JSON (e.g. mem0_search output).

    Non-JSON results such as prefetch markdown are returned unchanged.
    """
    if not text:
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def connect_ro(path):
    """Read-only connection, so pointing at the live log is safe."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_db(path):
    """Why `path` is unusable as the mem0 event log, or None if it is fine.

    Called once at startup so a bad path fails at launch, naming itself,
    rather than as a 500 on the tab. Existence alone was not enough: a stray
    `app.py .` made DB_PATH a directory, which exists, and sqlite then failed
    per-request with a bare "disk I/O error" from inside the view (sqlite
    reads the file header at connect, and the read returns EISDIR). Actually
    reading a row also catches a file that is not an sqlite database, or is
    some other database without the events table this plugin queries.
    """
    if not os.path.exists(path):
        return "no such file"
    if not os.path.isfile(path):
        return "not a regular file"
    try:
        conn = connect_ro(path)
    except sqlite3.Error as exc:
        return f"cannot be opened by sqlite ({exc})"
    try:
        conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return f"not a mem0 event log ({exc})"
    finally:
        conn.close()
    return None


def get_db():
    if "memory_db" not in g:
        g.memory_db = connect_ro(current_app.config["DB_PATH"])
    return g.memory_db


@bp.teardown_app_request
def close_db(_exc):
    db = g.pop("memory_db", None)
    if db is not None:
        db.close()


@bp.route("/")
def index():
    events = get_db().execute(
        "SELECT id, ts_utc, event_type, session_id, platform, query,"
        " memory_count, elapsed_ms FROM events ORDER BY id DESC"
    ).fetchall()
    return render_template("memory/index.html", events=events)


@bp.route("/fragment/events")
def event_rows_fragment():
    """Table rows for events newer than ?since=<id> — polled by the index."""
    since = request.args.get("since", 0, type=int)
    events = get_db().execute(
        "SELECT id, ts_utc, event_type, session_id, platform, query,"
        " memory_count, elapsed_ms FROM events WHERE id > ?"
        " ORDER BY id DESC",
        (since,),
    ).fetchall()
    return render_template("memory/_event_rows.html", events=events)


@bp.route("/search-event")
def search_event():
    """Redirect to the logged event for one mem0_search call.

    The timing tab's mem0_search spans and this tab's events are two
    independent records of the same call, with no shared key: ATOF carries
    no event id and the db carries no span uuid. They are matched instead on
    (session_id, query), which resolved to exactly one event for all 104
    mem0_search spans in the log to date — no ambiguity, no misses. The one
    ambiguity the pair admits is the same query repeated in one session, so
    an optional ?ts= (the span's start, epoch microseconds) breaks the tie by
    nearest logged time; the db timestamps the call's completion, about a
    second after the span starts, well inside any plausible gap between two
    such searches.

    Owned by this plugin because it owns the db — the timing tab links here
    by URL and never opens the event log itself, and this stays a redirect
    rather than a lookup at turn-render time so a page full of spans polling
    every 2 s costs no queries.
    """
    session_id = request.args.get("session")
    query = request.args.get("query")
    if not session_id or query is None:
        abort(400, "session and query are required")
    rows = get_db().execute(
        "SELECT id, ts_epoch FROM events WHERE event_type = 'mem0_search'"
        " AND session_id = ? AND query = ? ORDER BY id",
        (session_id, query),
    ).fetchall()
    if not rows:
        abort(404, "No mem0_search event is logged for this session and query."
                   " The event log and the ATOF timing log are written"
                   " independently, so one can cover a span the other does not.")
    ts = request.args.get("ts", type=float)
    if ts is not None and len(rows) > 1:
        row = min(rows, key=lambda r: abs(r["ts_epoch"] - ts / 1_000_000))
    else:
        row = rows[0]
    return redirect(url_for("memory.event", event_id=row["id"]))


@bp.route("/event/<int:event_id>")
def event(event_id):
    row = get_db().execute(
        "SELECT * FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None:
        abort(404)
    metadata = [(col, row[col]) for col in METADATA_COLUMNS if col in row.keys()]
    # mem0 uses the previous 10 messages as extra context for retrieval;
    # reconstruct them from the earlier logged queries in the same session.
    # Only prefetch rows are user messages — tool-call events are not.
    context_rows = get_db().execute(
        "SELECT id, query FROM events WHERE session_id = ? AND id < ?"
        " AND event_type = 'prefetch' ORDER BY id DESC LIMIT 10",
        (row["session_id"], event_id),
    ).fetchall()
    context_text = "\n\n".join(
        f"[#{r['id']}]\n{r['query']}" for r in reversed(context_rows)
    )
    prev_row = get_db().execute(
        "SELECT id FROM events WHERE id < ? ORDER BY id DESC LIMIT 1", (event_id,)
    ).fetchone()
    next_row = get_db().execute(
        "SELECT id FROM events WHERE id > ? ORDER BY id LIMIT 1", (event_id,)
    ).fetchone()
    return render_template(
        "memory/event.html",
        event=row,
        result_text=prettify_result(row["result"]),
        metadata=metadata,
        context_text=context_text,
        prev_id=prev_row["id"] if prev_row else None,
        next_id=next_row["id"] if next_row else None,
    )
