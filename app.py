"""App shell for the hermes observer.

Views live in plugins/ (Flask blueprints): memory browses jmem0_logged.db,
timing shows prompt-timing waterfalls from the NeMo Relay ATOF stream. The
shell registers each plugin under /<name>/, renders one tab per plugin, and
keeps the pre-plugin URLs working via redirects.
"""

import os
import sys

from flask import Flask, redirect, request, url_for

from plugins import PLUGINS

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


def startup_banner(db, atof, port):
    """What the app resolved, and whether it is there — a missing source is
    the usual reason a tab looks empty, so say it at startup too."""
    home = os.environ.get("HERMES_HOME")
    lines = [
        "hermes-observer",
        f"  HERMES_HOME  {home or '(unset — using built-in fallback path)'}",
        f"  config dir   {hermes_config_dir()}",
    ]
    for label, (path, source) in (("memory db", db), ("ATOF log", atof)):
        state = "ok" if os.path.exists(path) else "MISSING"
        lines.append(f"  {label:<12} {path}  [{state}] (from {source})")
    lines.append(f"  listening    http://0.0.0.0:{port}/")
    return "\n".join(lines)


def main():
    port = 5090
    db, atof = resolve_sources()
    # The reloader runs main() in both the supervisor and the worker; only
    # the worker sets WERKZEUG_RUN_MAIN, so printing in the supervisor shows
    # the banner once at launch rather than again on every .py edit.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print(startup_banner(db, atof, port), flush=True)
    db_path, atof_path = db[0], atof[0]
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    # use_reloader restarts the server when a .py file changes; debug stays
    # False so the Werkzeug interactive debugger (arbitrary code execution)
    # is never exposed on 0.0.0.0.
    create_app(db_path, atof_path=atof_path).run(
        debug=False, use_reloader=True, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
