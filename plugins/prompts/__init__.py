"""The Prompts view (blueprint `prompts`) — per-turn waterfalls from the
NeMo Relay ATOF stream.

Reader per docs/adr/0002: tailer (byte-offset incremental read) → parser
(JSONL line → typed event) → assembler (events → sessions → turns →
waterfall). Views run the tailer on each request and assemble in memory.

Per ADR 2's fail-open caveat, the page states loudly when the source is
missing or silent instead of rendering an empty timeline,
and parse errors / assembly anomalies are always surfaced, never dropped —
as collapsed problem sections on the turn-list page only, so turn pages
stay uncluttered.
"""

import importlib
import os
import time
from datetime import datetime, timezone

from flask import (Blueprint, abort, current_app, render_template,
                   url_for)
from werkzeug.routing import BuildError

import hermes_paths
from plugins.prompts.assembler import assemble
from plugins.prompts.scope_spec import (SpecTable, check_table, render_macro,
                                        rows_for)
from plugins.prompts.scopes import SCOPES, SCOPES_BY_CATEGORY
from plugins.prompts.tailer import AtofTailer

PLUGIN_API = 1
bp = Blueprint("prompts", __name__, template_folder="templates")
TAB_LABEL = "Prompts"
URL_PREFIX = "prompts"

# An in-flight turn silent this long is probably a lost end mark, not a
# running prompt: still listed in the strip, but never auto-followed.
STALE_AFTER_US = 10 * 60 * 1_000_000


def atof_path(settings):
    """The ATOF log to read: the `atof_log` setting, else $ATOF_LOG, else the
    nemo_relay exporter's default under the hermes config dir. A path is
    always returned — if it does not exist this tab says so itself, naming the
    path it looked at, which beats the vaguer unconfigured state."""
    if settings.get("atof_log"):
        return settings["atof_log"], "settings"
    if os.environ.get("ATOF_LOG"):
        return os.environ["ATOF_LOG"], "ATOF_LOG"
    return (os.path.join(hermes_paths.hermes_config_dir(), "nemo-relay",
                         "atof", "hermes-atof.jsonl"),
            f"default ({hermes_paths.config_dir_origin()})")


def spec_modules(settings):
    """Modules contributing scope specs, from the `scope_specs` setting.

    A string is accepted as well as a list, because writing one module path
    without brackets is the obvious thing to try.
    """
    value = settings.get("scope_specs") or []
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def tab_spec_tables(app):
    """Scope specs the other loaded tabs contribute (ADR 10).

    A tab that owns a kind of span describes it itself — mem0's spans are
    declared in `plugins/mem0/scopes.py`, not here — and the shell collects
    them before any tab registers. Reading them from `app.extensions` rather
    than importing anything keeps ADR 4's rule intact: this tab knows no
    other plugin by name.

    Their lifetime is the contributing tab's. Disable that tab and its specs
    go with it, which is what stops a span linking to a page nobody serves.
    """
    out = []
    for name, tables in (app.extensions.get("tab_scopes") or {}).items():
        by_name = tables.get("SCOPES") or {}
        by_category = tables.get("SCOPES_BY_CATEGORY") or {}
        # This tab exposes SCOPES too — it owns the specs for hermes' own
        # tools — and the shell offers them back like any other tab's. They
        # are already the base here, so skip them rather than merging a table
        # over itself and reporting it as a contribution.
        if by_name is SCOPES or by_category is SCOPES_BY_CATEGORY:
            continue
        faults = check_table(by_name, by_category)
        out.append({"tab": name, "by_name": by_name,
                    "by_category": by_category,
                    "problem": "; ".join(faults) if faults else None})
    return out


def spec_table(settings, app=None):
    """This tree's scope specs with any contributed module laid over them.

    Later wins: someone whose tool payload differs from hermes' replaces our
    handling of it rather than losing to it, and the override is named in the
    startup banner rather than applied silently (ADR 7). A module that cannot
    be imported, or that offers no table, is reported and skipped — the
    scopes it would have described fall back to the generic payload renderer,
    which is what they had before it was installed.
    """
    table = SpecTable(dict(SCOPES), dict(SCOPES_BY_CATEGORY))
    notes = []
    # Three layers, each overriding the last: this tree's defaults, then what
    # the loaded tabs contribute, then modules named in settings — most
    # explicit wins, and every override is named in the banner.
    for contributed in (tab_spec_tables(app) if app is not None else []):
        note = {"label": "scope spec", "path": f"tab {contributed['tab']}",
                "from": "contributed", "required": False,
                "problem": contributed["problem"]}
        if note["problem"] is None:
            taken = table.overrides_of(contributed["by_name"],
                                       contributed["by_category"])
            if taken:
                note["from"] = f"contributed, overriding {', '.join(taken)}"
            table = table.merged_with(contributed["by_name"],
                                      contributed["by_category"])
        notes.append(note)
    for path in spec_modules(settings):
        note = {"label": "scope spec", "path": path, "from": "settings",
                "required": False, "problem": None}
        try:
            module = importlib.import_module(path)
            by_name = getattr(module, "SCOPES", None) or {}
            by_category = getattr(module, "SCOPES_BY_CATEGORY", None) or {}
            if not isinstance(by_name, dict) or not isinstance(by_category, dict):
                raise TypeError("SCOPES must be a dict of scope name to Scope")
            if not by_name and not by_category:
                raise ValueError("no SCOPES in module")
            faults = check_table(by_name, by_category)
            if faults:
                raise ValueError("; ".join(faults))
        except Exception as exc:  # noqa: BLE001 - third-party module
            note["problem"] = f"{type(exc).__name__}: {exc}"
            notes.append(note)
            continue
        taken = table.overrides_of(by_name, by_category)
        if taken:
            note["from"] = f"settings, overriding {', '.join(taken)}"
        table = table.merged_with(by_name, by_category)
        notes.append(note)
    return table, notes


def sources(settings):
    """What this tab reads, for the startup banner (ADR 5).

    Not required: the log is allowed to be absent — hermes may simply not have
    run with the exporter on — and the tab explains that itself rather than
    being replaced by a shell error page. Contributed spec modules are listed
    the same way, so an override or a failed import is visible at startup
    beside the log it renders.
    """
    path, origin = atof_path(settings)
    entries = [{"label": "ATOF log", "path": path, "from": origin,
                "required": False,
                "problem": None if os.path.exists(path) else "no such file"}]
    entries.extend(spec_table(settings)[1])
    return entries


def init_app(app, settings):
    """Resolve the source once, at registration, so every request and the
    banner agree on which file this tab is reading.

    The spec table is resolved here too: importing a contributed module is
    startup work, not per-request work, and a turn page polling every 2 s
    must not pay for it.
    """
    app.config["ATOF_PATH"] = atof_path(settings)[0]
    app.config["SCOPE_SPECS"] = spec_table(settings, app)[0]


def get_tailer() -> AtofTailer:
    """The app-lifetime tailer for the configured ATOF path."""
    path = current_app.config["ATOF_PATH"]
    tailer = current_app.extensions.get("atof_tailer")
    if tailer is None or tailer.path != path:
        tailer = AtofTailer(path)
        current_app.extensions["atof_tailer"] = tailer
    return tailer


@bp.app_template_filter("us_dur")
def us_dur(us):
    """Microseconds → compact human duration; em dash when unknown."""
    if us is None:
        return "—"
    sign, us = ("-", -us) if us < 0 else ("", us)
    if us >= 1_000_000:
        return f"{sign}{us / 1_000_000:.2f} s"
    if us >= 1_000:
        return f"{sign}{us / 1_000:.0f} ms"
    return f"{sign}{us} µs"


@bp.app_template_filter("tilde")
def tilde(path):
    """Collapse this host's home-dir prefix to ~ for display. Workdirs from
    another host pass through untouched; the full path stays in the title
    attribute either way."""
    home = os.path.expanduser("~")
    if path and home != "~":
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home):]
    return path


@bp.app_template_global("spec_link")
def spec_link(endpoint, params):
    """A URL for a spec's `Link`, or None when nothing serves that endpoint.

    Tying spec lifetime to the contributing tab (ADR 10) is what normally
    stops a link outliving its target. This is the second line: a spec named
    in settings can point at a tab that is not loaded, and a typo in an
    endpoint is an ordinary mistake. `url_for` raises BuildError for both,
    which on a turn page would be a 500 for every span of that scope — where
    the right answer is to drop one row and render everything else.
    """
    try:
        return url_for(endpoint, **(params or {}))
    except BuildError:
        return None


@bp.app_template_filter("us_time")
def us_time(us):
    if us is None:
        return "—"
    stamp = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _source_problem():
    """The loud no-source state; None when the file is readable.

    There is no "unconfigured" case any more: a path is always resolved (the
    setting, then $ATOF_LOG, then the default), so an absent log is always a
    path that does not exist — which is the more useful thing to say.
    """
    atof_path = current_app.config["ATOF_PATH"]
    if not os.path.exists(atof_path):
        return render_template("prompts/index.html", state="missing",
                               atof_path=atof_path)
    return None


def _assembly():
    tailer = get_tailer()
    tailer.refresh()
    return assemble(tailer.events), tailer.errors


def _inflight_entries(assembly):
    """Still-running turns, newest first, dressed for the status strip.

    Two kinds of open turn are not running and are dropped: one superseded
    by a later turn in its session (Turn.is_live), and a subagent the
    parent has already reported stopped.
    """
    now_us = int(time.time() * 1_000_000)
    stopped = assembly.finished_subagent_sessions
    turns = sorted(
        (t for s in assembly.sessions for t in s.turns
         if t.is_live and t.session_id not in stopped),
        key=lambda t: t.start_us,
        reverse=True,
    )
    return [{
        "turn": t,
        "elapsed_us": now_us - t.start_us,
        "stale": now_us - t.last_activity_us > STALE_AFTER_US,
    } for t in turns]


def _neighbours(assembly, turn):
    """The turns either side of this one in the turn list's own ordering.

    The list is every session's turns interleaved by start time, so
    neighbours cross session boundaries just as the list does. Returns
    (older, newer); either may be None at the ends.
    """
    ordered = sorted(
        (t for s in assembly.sessions for t in s.turns),
        key=lambda t: t.start_us,
    )
    i = next((n for n, t in enumerate(ordered) if t is turn), None)
    if i is None:
        return None, None
    return (ordered[i - 1] if i > 0 else None,
            ordered[i + 1] if i + 1 < len(ordered) else None)


@bp.route("/")
def index():
    problem = _source_problem()
    if problem is not None:
        return problem
    assembly, parse_errors = _assembly()
    turns = sorted(
        (turn for session in assembly.sessions for turn in session.turns),
        key=lambda t: t.start_us,
        reverse=True,
    )
    last_us = max((s.last_us for s in assembly.sessions
                   if s.last_us is not None), default=None)
    return render_template(
        "prompts/index.html",
        state="ok",
        atof_path=current_app.config["ATOF_PATH"],
        turns=turns,
        last_us=last_us,
        inflight=_inflight_entries(assembly),
        anomalies=assembly.anomalies,
        parse_errors=parse_errors,
    )


def _accessors():
    """The accessors other plugins have published, for specs that name one.

    Passed whole rather than looked up here: which accessor a scope wants is
    the spec's business, not this tab's (ADR 9). Before that, this function
    named `mem0_prior_text` and keyed on `span.memory_id` — one plugin
    reachable because this tab knew about it, which is exactly the privilege
    ADR 9 removed. `app.extensions` also holds this tab's own entries; a spec
    can only reach what it names, and names are the published contract.
    """
    return current_app.extensions


@bp.route("/turn/<session_id>/<int:start_us>")
def turn(session_id, start_us):
    problem = _source_problem()
    if problem is not None:
        return problem
    assembly, _ = _assembly()
    found = next(
        (t for s in assembly.sessions if s.session_id == session_id
         for t in s.turns if t.start_us == start_us),
        None,
    )
    if found is None:
        abort(404)
    # Scale for the bars: a turn still in flight is drawn against the last
    # thing we saw in it.
    span_edges = [s.end_us or s.start_us for s in found.spans]
    scale_end = found.end_us or max(span_edges, default=found.start_us)
    scale_us = max(scale_end - found.start_us, 1)
    older, newer = _neighbours(assembly, found)
    # The spec table resolved at registration; a page with no init_app behind
    # it (a bare test app, say) still renders, on this tree's own specs.
    table = current_app.config.get("SCOPE_SPECS") or SpecTable(
        dict(SCOPES), dict(SCOPES_BY_CATEGORY))
    accessors = _accessors()
    return render_template(
        "prompts/turn.html",
        turn=found,
        current=found,
        scale_us=scale_us,
        inflight=_inflight_entries(assembly),
        older=older,
        newer=newer,
        scope_rows=lambda span: rows_for(span, table, accessors),
        scope_render=lambda span: render_macro(span, table),
    )
