"""Memory view — browses jmem0_logged.db, the mem0 event log.

Moved verbatim from the pre-plugin app.py; the database is always opened
read-only so it is safe to point at the live log while hermes is writing.
"""

import json
import sqlite3

from flask import Blueprint, abort, current_app, g, render_template, request

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


def get_db():
    if "memory_db" not in g:
        g.memory_db = sqlite3.connect(
            f"file:{current_app.config['DB_PATH']}?mode=ro", uri=True
        )
        g.memory_db.row_factory = sqlite3.Row
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
