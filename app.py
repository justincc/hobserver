"""Browser webapp for inspecting jmem0_logged.db mem0 event logs."""

import os
import sqlite3
import sys

from flask import Flask, abort, g, render_template

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

    @app.route("/event/<int:event_id>")
    def event(event_id):
        row = get_db().execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            abort(404)
        metadata = [(col, row[col]) for col in METADATA_COLUMNS if col in row.keys()]
        return render_template("event.html", event=row, metadata=metadata)

    return app


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JMEM0_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    create_app(db_path).run(debug=True, port=5090)


if __name__ == "__main__":
    main()
