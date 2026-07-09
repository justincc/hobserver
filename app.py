"""Browser webapp for inspecting jmem0_logged.db mem0 event logs."""

import os
import sqlite3
import sys

from flask import Flask, abort, g, render_template, request

DEFAULT_DB = os.path.expanduser(
    "~/jc/knowledge/data/processing/analysis/reasoning/artificial/agents/"
    "autonomous/resident/hermes-agent/product/src/config/jmem0_logged.db"
)

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


def create_app(db_path):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(f"file:{app.config['DB_PATH']}?mode=ro", uri=True)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def index():
        events = get_db().execute(
            "SELECT id, ts_utc, event_type, session_id, platform, query,"
            " memory_count, elapsed_ms FROM events ORDER BY id DESC"
        ).fetchall()
        return render_template("index.html", events=events)

    @app.route("/fragment/events")
    def event_rows_fragment():
        """Table rows for events newer than ?since=<id> — polled by the index."""
        since = request.args.get("since", 0, type=int)
        events = get_db().execute(
            "SELECT id, ts_utc, event_type, session_id, platform, query,"
            " memory_count, elapsed_ms FROM events WHERE id > ?"
            " ORDER BY id DESC",
            (since,),
        ).fetchall()
        return render_template("_event_rows.html", events=events)

    @app.route("/event/<int:event_id>")
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
            "event.html",
            event=row,
            metadata=metadata,
            context_text=context_text,
            prev_id=prev_row["id"] if prev_row else None,
            next_id=next_row["id"] if next_row else None,
        )

    return app


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JMEM0_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    create_app(db_path).run(debug=False, host="0.0.0.0", port=5090)


if __name__ == "__main__":
    main()
