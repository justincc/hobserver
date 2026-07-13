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

DEFAULT_DB = os.path.expanduser(
    "~/jc/knowledge/data/processing/analysis/reasoning/artificial/agents/"
    "autonomous/resident/hermes-agent/product/src/config/jmem0_logged.db"
)


def create_app(db_path, atof_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["ATOF_PATH"] = atof_path

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


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JMEM0_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    atof_path = os.environ.get("ATOF_LOG")
    create_app(db_path, atof_path=atof_path).run(debug=False, host="0.0.0.0", port=5090)


if __name__ == "__main__":
    main()
