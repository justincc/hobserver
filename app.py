"""App shell for the hermes observer.

Views are plugins (Flask blueprints) named by a TOML config file and loaded by
module path — see ADR 5 and tabs.py. The shell registers each tab under its
own URL_PREFIX, renders one tab per plugin in config order, and sends / to the
first of them.

It knows nothing about any particular tab: no plugin is imported here, and a
tab's data sources are its own business, reported through its `sources` hook
for the banner. A tab that cannot load is replaced by a page saying why, so
one broken plugin never stops the others from serving.

URLs are free to move: this is a single-user tool, so nothing redirects an
old address to a new one — rename the prefix and follow it.
"""

import logging
import os
import sys

from flask import (Blueprint, Flask, redirect, render_template, request,
                   url_for)

import hermes_paths
import tabs as tabs_module
from request_log import (REFRESH_SECONDS, STATUS_PATH, RequestStats,
                         SuppressSuccessFilter, format_status)


def create_app(tabs):
    """The app for an already-loaded list of `tabs.Tab`."""
    app = Flask(__name__)
    # Re-read templates from disk when they change, so edits show up on the
    # next request (or next live-region poll) without a server restart.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.extensions["tab_settings"] = {}
    # Scope specs the loaded tabs contribute (ADR 10), collected before any
    # tab is registered: a tab that paints spans resolves its table in
    # `init_app`, which runs during registration, and would otherwise see
    # only the tabs that happened to come before it in the config file.
    # Opaque to the shell — it carries them, and never looks inside.
    app.extensions["tab_scopes"] = {
        tab.name: tab.scopes for tab in tabs if tab.bp is not None and tab.scopes}

    entries = []
    for tab in tabs:
        entries.append(_register(app, tab))

    @app.context_processor
    def inject_tabs():
        return {"tabs": entries}

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

    if entries:
        @app.route("/")
        def root():
            return redirect(url_for(entries[0]["endpoint"]))

    return app


def _register(app, tab):
    """Register one tab and return its entry for the tab bar."""
    if tab.bp is None:
        return _register_unavailable(app, tab)

    # The tab's own settings, reachable as
    # current_app.extensions["tab_settings"][<blueprint name>]. Set before
    # registration so a record_once hook can already read them.
    app.extensions["tab_settings"][tab.bp.name] = tab.settings
    # `init_app(app, settings)` — the optional hook a tab uses to resolve its
    # configuration once and to publish an accessor other tabs may call
    # (ADR 4). Called before the blueprint is registered, so anything it puts
    # on `app.config` or `app.extensions` is already there for a route or a
    # record_once. Absent means nothing to do; raising here is the tab
    # author's bug and is left to surface rather than being swallowed.
    init = getattr(tab.module, "init_app", None)
    if init is not None:
        init(app, tab.settings)
    app.register_blueprint(tab.bp, url_prefix=f"/{tab.url_prefix}")
    return {"name": tab.bp.name, "label": tab.label, "problem": None,
            "endpoint": f"{tab.bp.name}.index"}


def _register_unavailable(app, tab):
    """A page in place of a tab that could not be loaded.

    The tab stays in the bar, marked, rather than vanishing: a tab that is
    silently absent looks like a missing feature, where one that says "no such
    file" names the fix.
    """
    name = f"unavailable_{tab.name}"
    bp = Blueprint(name, __name__)
    problem, label = tab.problem, tab.label

    @bp.route("/")
    def index():
        return render_template("unavailable.html", label=label,
                               problem=problem, module=tab.module_name,
                               sources=tab.sources), 503

    prefix = tab.url_prefix or f"unavailable/{tab.name}"
    app.register_blueprint(bp, url_prefix=f"/{prefix}")
    return {"name": name, "label": label, "problem": problem,
            "endpoint": f"{name}.index"}


def startup_banner(tabs, port, config_origin, serving=True):
    """What the app resolved, and whether each tab can read what it needs.

    A missing or unreadable source is the usual reason a tab looks empty, so
    every tab's sources are listed with the rule that supplied each path. A
    tab that could not load at all leads with its problem instead.
    """
    home = os.environ.get("HERMES_HOME")
    lines = [
        "hermes-observer",
        f"  config       {config_origin}",
        f"  HERMES_HOME  {home or '(unset — using built-in fallback path)'}",
        f"  hermes dir   {hermes_paths.hermes_config_dir()}",
    ]
    if not tabs:
        lines.append("  tabs         (none configured)")
    for tab in tabs:
        state = "ok" if tab.problem is None else f"UNAVAILABLE ({tab.problem})"
        lines.append(f"  tab          {tab.label}  [{state}]"
                     f"  ({tab.module_name})")
        for source in tab.sources:
            problem = source.get("problem")
            if problem is None:
                mark = "ok"
            else:
                mark = f"{'UNUSABLE' if source.get('required') else 'MISSING'}" \
                       f" ({problem})"
            lines.append(f"    {source.get('label', 'source'):<10} "
                         f"{source.get('path', '')}  [{mark}]"
                         f" (from {source.get('from', 'default')})")
    if serving:
        lines.append(f"  listening    http://0.0.0.0:{port}/")
        lines.append(f"  status       http://localhost:{port}{STATUS_PATH}")
        lines.append("               successful requests are not logged below — only "
                     "errors show;\n               that page tallies every request.")
    return "\n".join(lines)


def main():
    port = 5090
    path = tabs_module.config_path(sys.argv[1] if len(sys.argv) > 1 else None)
    try:
        specs, origin = tabs_module.read_config(path)
        tabs = tabs_module.load_tabs(specs)
    except tabs_module.ConfigError as exc:
        sys.exit(f"hermes-observer: {exc}")

    # The reloader runs main() in both the supervisor and the worker; only
    # the worker sets WERKZEUG_RUN_MAIN, so printing in the supervisor shows
    # the banner once at launch rather than again on every .py edit.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print(startup_banner(tabs, port, origin, serving=bool(tabs)), flush=True)
    # A broken tab is that tab's problem; no tab at all leaves nothing to
    # serve, which is worth exiting for.
    if not tabs:
        sys.exit("hermes-observer: no tabs could be loaded — nothing to serve")

    # Installed on the werkzeug access logger, so it only affects the dev
    # server's own console output — the app logs nothing of its own.
    logging.getLogger("werkzeug").addFilter(SuppressSuccessFilter())
    # use_reloader restarts the server when a .py file changes; debug stays
    # False so the Werkzeug interactive debugger (arbitrary code execution)
    # is never exposed on 0.0.0.0.
    create_app(tabs).run(debug=False, use_reloader=True, host="0.0.0.0",
                         port=port)


if __name__ == "__main__":
    main()
