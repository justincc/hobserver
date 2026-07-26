"""App shell for the hermes observer.

Views live in plugins/ (Flask blueprints): memory browses jmem0_logged.db,
timing shows prompt-timing waterfalls from the NeMo Relay ATOF stream. The
shell registers each plugin under /<name>/, renders one tab per plugin, and
keeps the pre-plugin URLs working via redirects.
"""

import logging
import os
import sys

from flask import Flask, redirect, request, url_for

from plugins import PLUGINS
from plugins.memory import check_db
from request_log import (REFRESH_SECONDS, STATUS_PATH, RequestStats,
                         SuppressSuccessFilter, format_status)

# Both data sources live under the hermes-agent config directory, which the
# agent itself points at with HERMES_HOME — deriving them from it keeps this
# machine's absolute paths out of the repo and means neither source needs to
# be passed on the command line. The literal is the fallback for a shell that
# never exported HERMES_HOME.
FALLBACK_CONFIG_DIR = os.path.expanduser(
    "~/jc/knowledge/data/processing/analysis/reasoning/artificial/agents/"
    "autonomous/resident/hermes-agent/product/src/config"
)


def hermes_config_dir():
    """The hermes-agent config directory: $HERMES_HOME, else the fallback.
    HERMES_HOME is conventionally set to <checkout>/hermes-agent/../config,
    so it is normalized rather than used raw."""
    home = os.environ.get("HERMES_HOME")
    return os.path.normpath(home) if home else FALLBACK_CONFIG_DIR


def default_db_path():
    return os.path.join(hermes_config_dir(), "jmem0_logged.db")


def default_atof_path():
    """Where the nemo_relay ATOF exporter writes by default. A path is always
    returned: if it does not exist the timing tab says so loudly, naming the
    path it looked at, which beats the vaguer unconfigured state."""
    return os.path.join(hermes_config_dir(), "nemo-relay", "atof",
                        "hermes-atof.jsonl")


def create_app(db_path, atof_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["ATOF_PATH"] = atof_path
    # Re-read templates from disk when they change, so edits show up on the
    # next request (or next live-region poll) without a server restart.
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    tabs = []
    for plugin in PLUGINS:
        app.register_blueprint(plugin.bp, url_prefix=f"/{plugin.bp.name}")
        tabs.append(
            {
                "name": plugin.bp.name,
                "label": plugin.TAB_LABEL,
                "endpoint": f"{plugin.bp.name}.index",
            }
        )

    @app.context_processor
    def inject_tabs():
        return {"tabs": tabs}

    # Request tally behind /_status: the console suppresses successful
    # responses, so this is what answers "is anything still reaching the
    # server?".
    stats = RequestStats()
    app.extensions["request_stats"] = stats

    @app.after_request
    def _count_request(response):
        # /_status itself is excluded: checking the tally must not look like
        # traffic, or its own "last" time would always read as fresh.
        if request.path != STATUS_PATH:
            stats.record(request.path, response.status_code)
        return response

    @app.route(STATUS_PATH)
    def request_status():
        # Refresh header rather than the live-poll script: it keeps the body
        # plain text, so the page reads the same curled or in a browser, and
        # needs no template. curl ignores the header.
        return format_status(stats.snapshot()), 200, {
            "Content-Type": "text/plain; charset=utf-8",
            "Refresh": str(REFRESH_SECONDS)}

    @app.route("/")
    def root():
        return redirect(url_for(tabs[0]["endpoint"]))

    # Pre-plugin URLs: bookmarks and still-open index pages (whose poll loop
    # hits /fragment/events) from before the memory view moved to /memory/.
    @app.route("/event/<int:event_id>")
    def legacy_event(event_id):
        return redirect(url_for("memory.event", event_id=event_id))

    @app.route("/fragment/events")
    def legacy_event_rows_fragment():
        return redirect(
            url_for("memory.event_rows_fragment", **request.args.to_dict())
        )

    return app


def resolve_sources():
    """Both data sources, each with the reason it resolved that way, so
    startup can show where the app is actually reading from."""
    if len(sys.argv) > 1:
        db_path, db_from = sys.argv[1], "command line"
    elif os.environ.get("JMEM0_DB"):
        db_path, db_from = os.environ["JMEM0_DB"], "JMEM0_DB"
    else:
        db_path, db_from = default_db_path(), "default"

    if os.environ.get("ATOF_LOG"):
        atof_path, atof_from = os.environ["ATOF_LOG"], "ATOF_LOG"
    else:
        atof_path, atof_from = default_atof_path(), "default"
    return (db_path, db_from), (atof_path, atof_from)


def startup_banner(db, atof, port, db_problem=None):
    """What the app resolved, and whether it is usable — a missing source is
    the usual reason a tab looks empty, so say it at startup too. The memory
    db's state comes from check_db (passed in, already computed by main)
    rather than mere existence, so a path that exists but cannot be read as
    an event log says so here instead of reading [ok] and then 500ing."""
    home = os.environ.get("HERMES_HOME")
    lines = [
        "hermes-observer",
        f"  HERMES_HOME  {home or '(unset — using built-in fallback path)'}",
        f"  config dir   {hermes_config_dir()}",
    ]
    db_state = "ok" if db_problem is None else f"UNUSABLE ({db_problem})"
    atof_state = "ok" if os.path.exists(atof[0]) else "MISSING"
    for label, (path, source), state in (("memory db", db, db_state),
                                         ("ATOF log", atof, atof_state)):
        lines.append(f"  {label:<12} {path}  [{state}] (from {source})")
    # Nothing is served when the db is unusable — main exits right after this
    # — so the banner must not claim to be listening; the resolved paths above
    # are the whole point of still printing it.
    if db_problem is None:
        lines.append(f"  listening    http://0.0.0.0:{port}/")
        lines.append(f"  status       http://localhost:{port}{STATUS_PATH}")
        lines.append("               successful requests are not logged below — only "
                     "errors show;\n               that page tallies every request.")
    return "\n".join(lines)


def main():
    port = 5090
    db, atof = resolve_sources()
    db_path, atof_path = db[0], atof[0]
    db_problem = check_db(db_path)
    # The reloader runs main() in both the supervisor and the worker; only
    # the worker sets WERKZEUG_RUN_MAIN, so printing in the supervisor shows
    # the banner once at launch rather than again on every .py edit.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print(startup_banner(db, atof, port, db_problem=db_problem), flush=True)
    if db_problem is not None:
        sys.exit(f"Memory database unusable: {db_path} — {db_problem}")
    # Installed on the werkzeug access logger, so it only affects the dev
    # server's own console output — the app logs nothing of its own.
    logging.getLogger("werkzeug").addFilter(SuppressSuccessFilter())
    # use_reloader restarts the server when a .py file changes; debug stays
    # False so the Werkzeug interactive debugger (arbitrary code execution)
    # is never exposed on 0.0.0.0.
    create_app(db_path, atof_path=atof_path).run(
        debug=False, use_reloader=True, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
