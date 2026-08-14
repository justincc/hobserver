"""The Mem0 view (blueprint `mem0`) — browses jmem0_logged.db, the mem0
event log.

The database is always opened read-only, so it is safe to point at the live
log while hermes is writing.

The tab is named for the provider, not for "memory" in general: hermes also
keeps its own in-prompt stores (MEMORY.md / USER.md, the `memory` tool) and
may yet be pointed at other external providers, and those get their own tabs
rather than being folded in here — under /memory/ alongside this one, see
URL_PREFIX. The module, the blueprint and the tab all read `mem0`; the URL
keeps the `memory/` namespace, which is the one place the three names
deliberately differ.
"""

import json
import os
import sqlite3

from flask import (Blueprint, abort, current_app, g, redirect, render_template,
                   request, url_for)

import hermes_paths

PLUGIN_API = 1

# How this plugin's own spans show on a turn page (ADR 10) and how their
# payloads are read (ADR 17). Contributed to whichever tab paints spans, and
# gone the moment this tab is disabled — so a link into a page that is no
# longer served cannot be left behind. Both halves are mem0's: nothing in the
# Turns tab knows what a mem0 search result looks like.
from plugins.mem0.scopes import SCOPES  # noqa: E402  (after PLUGIN_API)
from plugins.mem0.spans import SPAN_READERS  # noqa: E402  (after PLUGIN_API)
bp = Blueprint("mem0", __name__, template_folder="templates")
TAB_LABEL = "Mem0"
# Namespaced under memory/ because mem0 is one memory system of several to
# come: hermes' own in-prompt stores and any other provider get a sibling
# (/memory/internal/, …), and /memory/ itself is left free to become an
# index over them. Hierarchy rather than a punctuated single segment
# (/memory:mem0/) — a colon in the first segment of a *relative* URL parses
# as a scheme, which would silently break any link not built by url_for.
URL_PREFIX = "memory/mem0"

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

# The `query` column holds whatever the call's subject was, which is only
# literally a query for the two retrieval events. Labelling it "Query"
# everywhere made a mem0_add's stored fact and a mem0_delete's bare memory
# id both read as something the user had searched for.
QUERY_LABELS = {
    "mem0_add": "Added text",
    "mem0_update": "New text",
    "mem0_delete": "Deleted memory id",
}
DEFAULT_QUERY_LABEL = "Query"


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

    Called from `sources` at load time, so a bad path shows as a tab that
    names the problem rather than as a 500 from inside a view. Existence alone
    was not enough: a stray
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


def db_path(settings):
    """The event log to read: the `db` setting, else the default under the
    hermes config dir."""
    if settings.get("db"):
        return settings["db"]
    return os.path.join(hermes_paths.hermes_config_dir(), "jmem0_logged.db")


def sources(settings):
    """What this tab reads, for the startup banner (ADR 5).

    Required: unlike a missing ATOF log, an unreadable db has nothing to
    render and every page would 500 from inside a view. The shell replaces the
    tab with the problem instead, and the rest of the app serves on.
    """
    path = db_path(settings)
    return [{"label": "event db", "path": path,
             "required": True, "problem": check_db(path)}]


def init_app(app, settings):
    """Resolve the source once, at registration, so every request and the
    banner agree on which file this tab is reading."""
    app.config["DB_PATH"] = db_path(settings)


def get_db():
    if "mem0_db" not in g:
        g.mem0_db = connect_ro(current_app.config["DB_PATH"])
    return g.mem0_db


def _gap_text(seconds):
    """A gap in words. Formatted here rather than as a template filter: both
    tabs render it, and a filter one plugin registers for another to use is
    the coupling ADR 4 rules out."""
    seconds = max(seconds, 0)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _memory_id_arg(extra):
    """The memory_id a mem0_update/mem0_delete row names, from its `extra`
    JSON ({"args": {"memory_id": …}}), or None if it is not there."""
    try:
        parsed = json.loads(extra or "")
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    args = parsed.get("args")
    if not isinstance(args, dict):
        return None
    memory_id = args.get("memory_id")
    return memory_id if isinstance(memory_id, str) and memory_id else None


def prior_memory_text(memory_id, before_epoch):
    """The last text logged for `memory_id` before `before_epoch`, or None.

    mem0 is never asked. Once a memory is deleted the platform cannot return
    it at all, and hermes' Mem0Backend exposes only search/add/update/delete
    — no get, no history — so the pre-change text is not retrievable from
    mem0 for either case. The local event log has it anyway: a mem0_search
    result carries each hit's full text beside its id, and the agent can only
    learn a memory id *from* a search, so every update and delete is preceded
    by the search that surfaced it — in practice within the same session, and
    seconds to minutes earlier.

    This is therefore the memory as of that search, not a guaranteed
    pre-change snapshot: a change made outside hermes in between would not
    show. Callers must say where the text came from — the returned event id
    and gap are for exactly that.
    """
    row = get_db().execute(
        "SELECT id, ts_utc, ts_epoch, result FROM events"
        " WHERE event_type = 'mem0_search' AND ts_epoch < ?"
        " AND result LIKE ? ORDER BY ts_epoch DESC LIMIT 1",
        (before_epoch, f"%{memory_id}%"),
    ).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(row["result"] or "")
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    for item in parsed.get("results") or []:
        if isinstance(item, dict) and item.get("id") == memory_id:
            text = item.get("memory")
            if not isinstance(text, str) or not text:
                return None
            gap_s = before_epoch - row["ts_epoch"]
            return {
                "text": text,
                "event_id": row["id"],
                "ts_utc": row["ts_utc"],
                "gap_s": gap_s,
                "gap_text": _gap_text(gap_s),
            }
    return None


@bp.record_once
def _register_lookup(state):
    """Publish the prior-text lookup on the app, per ADR 4.

    The Turns tab needs a memory's previous text but must not open the
    event log itself; it calls this through `app.extensions` and does
    without when the mem0 plugin is not registered. The db stays behind
    the plugin that owns it.
    """
    state.app.extensions["mem0_prior_text"] = prior_memory_text


@bp.teardown_app_request
def close_db(_exc):
    db = g.pop("mem0_db", None)
    if db is not None:
        db.close()


@bp.route("/")
def index():
    events = get_db().execute(
        "SELECT id, ts_utc, event_type, session_id, platform, query,"
        " memory_count, elapsed_ms FROM events ORDER BY id DESC"
    ).fetchall()
    return render_template("mem0/index.html", events=events)


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
    return render_template("mem0/_event_rows.html", events=events)


@bp.route("/search-event")
def search_event():
    """Redirect to the logged event for one mem0_search call.

    The Turns tab's mem0_search spans and this tab's events are two
    independent records of the same call, with no shared key: ATOF carries
    no event id and the db carries no span uuid. They are matched instead on
    (session_id, query), which has resolved to exactly one event for every
    span checked — no ambiguity, no misses. The one
    ambiguity the pair admits is the same query repeated in one session, so
    an optional ?ts= (the span's start, epoch microseconds) breaks the tie by
    nearest logged time; the db timestamps the call's completion, about a
    second after the span starts, well inside any plausible gap between two
    such searches.

    Owned by this plugin because it owns the db — the Turns tab links here
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
    return redirect(url_for("mem0.event", event_id=row["id"]))


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
    # An update or delete names a memory by id and says nothing about what
    # that memory said — the one thing a reader wants. Reconstruct it from
    # the search that surfaced the id; the template states the provenance.
    prior = None
    if row["event_type"] in ("mem0_update", "mem0_delete"):
        memory_id = _memory_id_arg(row["extra"])
        if memory_id:
            prior = prior_memory_text(memory_id, row["ts_epoch"])
    return render_template(
        "mem0/event.html",
        event=row,
        result_text=prettify_result(row["result"]),
        metadata=metadata,
        context_text=context_text,
        query_label=QUERY_LABELS.get(row["event_type"], DEFAULT_QUERY_LABEL),
        prior=prior,
        prev_id=prev_row["id"] if prev_row else None,
        next_id=next_row["id"] if next_row else None,
    )
