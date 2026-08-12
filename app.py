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

import errno
import logging
import os
import sys

from flask import (Blueprint, Flask, redirect, render_template, request,
                   url_for)
from waitress import serve

import hermes_paths
import tabs as tabs_module
from request_log import (REFRESH_SECONDS, STATUS_PATH, RequestStats,
                         QuietWerkzeugFilter, format_status,
                         log_error_response)

PORT = 5090


def create_app(tabs, dev=False):
    """The app for an already-loaded list of `tabs.Tab`.

    `dev` is the `--dev` switch, and decides one thing here: whether the
    running app tracks edits to this checkout (ADR 15).
    """
    app = Flask(__name__)
    # Templates re-read from disk under --dev only, so that one switch covers
    # every kind of edit: without it neither a .py nor a template reaches a
    # running app, and a tool nobody is hacking on cannot be changed by a
    # stray keystroke in a file it happens to serve.
    app.config["TEMPLATES_AUTO_RELOAD"] = dev
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
        # Not excluded from the log, though — a failing /_status is exactly the
        # kind of thing the console must still say out loud. The query string
        # goes with it when there is one, since a failing poll is identified by
        # its `since=` as much as by its path.
        log_error_response(app.logger, request.method,
                           request.full_path if request.query_string
                           else request.path,
                           response.status_code)
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


def startup_banner(tabs, port, config_origin, serving=True, dev=False):
    """What the app resolved, and whether each tab can read what it needs.

    A missing or unreadable source is the usual reason a tab looks empty, so
    every tab's sources are listed with the rule that supplied each path. A
    tab that could not load at all leads with its problem instead.
    """
    home = os.environ.get("HERMES_HOME")
    resolved = [
        "hermes-observer",
        f"  config       {config_origin}",
        f"  hermes dir   {hermes_paths.hermes_config_dir()}"
        f" ({'from HERMES_HOME' if home else 'default location, HERMES_HOME not set'})",
    ]

    tab_lines = []
    if not tabs:
        tab_lines.append("  tabs         (none configured)")
    for tab in tabs:
        state = "ok" if tab.problem is None else f"UNAVAILABLE ({tab.problem})"
        tab_lines.append(f"  tab          {tab.label} ({tab.module_name})"
                         f"  [{state}]")
        for source in tab.sources:
            problem = source.get("problem")
            if problem is None:
                mark = "ok"
            else:
                mark = f"{'UNUSABLE' if source.get('required') else 'MISSING'}" \
                       f" ({problem})"
            supplier = source.get("from")
            tab_lines.append(f"    {source.get('label', 'source'):<10} "
                             f"{source.get('path', '')}  [{mark}]"
                             + (f" (from {supplier})" if supplier else ""))

    serving_lines = []
    if serving:
        # Off prints too: it answers why an edit changed nothing, so it
        # carries the switch. The server is not here — it never varies.
        serving_lines.append("  reloading    " + (
            "yes — .py edits restart, template edits land on the next request"
            if dev else
            "no (specify --dev to pick up .py and template edits)"))
        serving_lines.append(f"  listening    http://0.0.0.0:{port}/")
        serving_lines.append(f"  status       http://localhost:{port}{STATUS_PATH}")
        serving_lines.append(
            "               successful requests are not logged below — only "
            "errors show;\n               that page tallies every request.")

    # Blank lines between the groups, and an empty group takes none with it.
    return "\n\n".join("\n".join(group) for group
                       in (resolved, tab_lines, serving_lines) if group)


def serve_forever(app, port=PORT):
    """Serve until stopped, reporting if the port is already taken."""
    try:
        serve(app, host="0.0.0.0", port=port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"hermes-observer: port {port} is already in use — another "
              "observer is probably still running", file=sys.stderr, flush=True)
        # os._exit, not sys.exit: under --dev this runs in a thread the
        # reloader starts, where SystemExit would end that thread alone.
        os._exit(1)


def parse_args(argv):
    """`(config path or None, dev)` from the arguments after the program name.

    One flag and one positional, so nothing here justifies argparse. `--dev` is
    filtered out rather than counted by position, so it may be written on
    either side of the config file.
    """
    dev = "--dev" in argv
    rest = [arg for arg in argv if arg != "--dev"]
    return (rest[0] if rest else None), dev


def main():
    config_arg, dev = parse_args(sys.argv[1:])
    path = tabs_module.config_path(config_arg)
    try:
        specs, origin = tabs_module.read_config(path)
        tabs = tabs_module.load_tabs(specs)
    except tabs_module.ConfigError as exc:
        sys.exit(f"hermes-observer: {exc}")

    # Under --dev the reloader runs main() in both the supervisor and the
    # worker; only the worker sets WERKZEUG_RUN_MAIN, so printing in the
    # supervisor shows the banner once at launch rather than again on every
    # .py edit. Without --dev there is one process and the variable is unset.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print(startup_banner(tabs, PORT, origin, serving=bool(tabs), dev=dev),
              flush=True)
    # A broken tab is that tab's problem; no tab at all leaves nothing to
    # serve, which is worth exiting for.
    if not tabs:
        sys.exit("hermes-observer: no tabs could be loaded — nothing to serve")

    # Werkzeug reaches this console only as the --dev reloader; installed in
    # both modes so the two print the same thing.
    logging.getLogger("werkzeug").addFilter(QuietWerkzeugFilter())
    # Configured here, and first: `waitress.serve` calls `logging.basicConfig()`
    # itself, so leaving this out does not mean plain output — it means
    # inheriting waitress's, which carries no clock. An error line needs one to
    # be matched against the page that produced it. WARNING as the level is
    # also what keeps waitress's own "Serving on ..." (INFO) out of a console
    # the banner has already told where to look.
    logging.basicConfig(level=logging.WARNING, datefmt="%H:%M:%S",
                        format="[%(asctime)s] %(levelname)s %(message)s")

    def start():
        # The app is built here rather than in main() so that under --dev the
        # supervisor never builds one: it does not serve, and a tab's init_app
        # may open files. The worker re-execs the whole script, so it gets a
        # fresh app on every restart either way.
        serve_forever(create_app(tabs, dev=dev))

    if not dev:
        start()
        return
    # The reloader is a supervisor around any callable, so --dev adds
    # restart-on-edit to the same waitress server rather than swapping in a
    # different one — no development-only serving path to diverge (ADR 14).
    # Imported here because it is a private name: if it ever moves, --dev
    # fails loudly at the moment it is asked for, and ordinary running, which
    # never reaches this line, is untouched.
    from werkzeug._reloader import run_with_reloader
    run_with_reloader(start)


if __name__ == "__main__":
    main()
