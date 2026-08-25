"""The Turns view (blueprint `turns`) — per-turn waterfalls from the
NeMo Relay ATOF stream.

Reader per docs/design/adr/0002: tailer (byte-offset incremental read) → parser
(JSONL line → typed event) → index (spine cached in SQLite, payloads left in
the log, docs/design/adr/0011) → assembler (events → sessions → turns →
waterfall), with `spans` reading what each span holds. Views refresh the
index on each request and assemble in memory; the turn page reads back the
payloads of the one turn it is showing.

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

from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, url_for)
from werkzeug.routing import BuildError

import hermes_paths
from console import console
from plugins.turns import fulltext, skills
from plugins.turns.assembler import assemble, containing_turn
from plugins.turns.atof_index import (AtofIndex, default_index_path,
                                        hydrate_span, hydrate_turn)
from providers import USAGE_SHAPES, check_shapes
from scope_spec import (SpecTable, check_readers,
                                        check_table, full_for, full_link,
                                        render_macro, resolve_full,
                                        resolve_source, rows_for)
from plugins.turns.scopes import SCOPES, SCOPES_BY_CATEGORY
from plugins.turns.spans import Span, resolve_memory_entries

PLUGIN_API = 1
bp = Blueprint("turns", __name__, template_folder="templates")
TAB_LABEL = "Turns"
URL_PREFIX = "turns"

# An in-flight turn silent this long is treated as a lost end mark, not a
# running prompt, and dropped from the in-flight strip. Two hours: an agentic
# turn can legitimately go quiet for many minutes — a slow model call or a long
# tool run emits no ATOF events meanwhile — so a short cutoff would retire turns
# that are still working.
STALE_AFTER_US = 2 * 60 * 60 * 1_000_000


def atof_path(settings):
    """The ATOF log to read: the `atof_log` setting, else the nemo_relay
    exporter's default under the hermes config dir. A path is always returned
    — if it does not exist this tab says so itself, naming the path it looked
    at, which beats the vaguer unconfigured state."""
    if settings.get("atof_log"):
        return settings["atof_log"]
    return os.path.join(hermes_paths.hermes_config_dir(), "nemo-relay",
                        "atof", "hermes-atof.jsonl")


def index_db_path(settings):
    """Where this tab caches its index of the log (ADR 11).

    The `index_db` setting, else a file of our own named after the log. Never
    beside the log — that directory is hermes'. Deleting the file costs a
    rebuild and nothing else; it holds no fact the log does not.
    """
    if settings.get("index_db"):
        return settings["index_db"]
    return default_index_path(atof_path(settings))


def spec_modules(settings):
    """Modules contributing scope specs, from the `scope_specs` setting.

    A string is accepted as well as a list, because writing one module path
    without brackets is the obvious thing to try.
    """
    value = settings.get("scope_specs") or []
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def provider_modules(settings):
    """Modules contributing provider usage shapes, from `provider_specs`.

    A string is accepted as well as a list, for the same reason `scope_specs`
    accepts one: writing a single module path without brackets is the obvious
    thing to try.
    """
    value = settings.get("provider_specs") or []
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def usage_shape_table(settings):
    """This tree's provider shapes with any contributed module in front (ADR 13).

    Returns `(shapes, notes)`. Contributed shapes are tried **first**, not
    merged: a shape is a probe, and the built-in `openai_compatible` claims
    any payload carrying a recognisable OpenAI-ish key. Something more
    specific can only win by being asked first, which is the same
    "most explicit wins" rule `spec_table` applies by merging.

    A module that cannot be imported, offers no shapes, or offers malformed
    ones is reported and skipped — its provider's counts fall back to the
    built-in reading, which is what they were before it was installed. It
    never takes the tab down: the log still renders, minus one provider's
    token rows.
    """
    shapes = list(USAGE_SHAPES)
    notes = []
    for path in provider_modules(settings):
        note = {"label": "provider spec", "path": path, "from": "settings",
                "required": False, "problem": None}
        try:
            module = importlib.import_module(path)
            contributed = getattr(module, "USAGE_SHAPES", None)
            if not contributed:
                raise ValueError("no USAGE_SHAPES in module")
            faults = check_shapes(contributed)
            if faults:
                raise ValueError("; ".join(faults))
        except Exception as exc:  # noqa: BLE001 - third-party module
            note["problem"] = f"{type(exc).__name__}: {exc}"
            notes.append(note)
            continue
        named = ", ".join(s.name for s in contributed)
        note["from"] = f"settings, tried before {USAGE_SHAPES[0].name} ({named})"
        shapes = list(contributed) + shapes
        notes.append(note)
    return tuple(shapes), notes


def tab_spec_tables(app):
    """Scope specs and span readers the other loaded tabs contribute
    (ADR 10, ADR 17).

    A tab that owns a kind of span describes it itself — mem0's spans are
    declared in `plugins/memory/mem0/scopes.py` and read in `plugins/memory/mem0/spans.py`,
    not here — and the shell collects them before any tab registers. Reading
    them from `app.extensions` rather than importing anything keeps ADR 4's
    rule intact: this tab knows no other plugin by name.

    Their lifetime is the contributing tab's. Disable that tab and its specs
    go with it, which is what stops a span linking to a page nobody serves.

    A fault in either table skips the whole contribution: the two arrive
    from one module and describe one tool between them, and half of it —
    rows naming readings that never loaded — is worse than the payload dump
    they replace.
    """
    out = []
    for name, tables in (app.extensions.get("tab_scopes") or {}).items():
        by_name = tables.get("SCOPES") or {}
        by_category = tables.get("SCOPES_BY_CATEGORY") or {}
        readers = tables.get("SPAN_READERS") or {}
        # This tab exposes SCOPES too — it owns the specs for hermes' own
        # tools — and the shell offers them back like any other tab's. They
        # are already the base here, so skip them rather than merging a table
        # over itself and reporting it as a contribution.
        if by_name is SCOPES or by_category is SCOPES_BY_CATEGORY:
            continue
        faults = check_table(by_name, by_category) + check_readers(readers)
        out.append({"tab": name, "by_name": by_name,
                    "by_category": by_category, "readers": readers,
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
                                       contributed["by_category"],
                                       contributed["readers"])
            taken += _shadowed_properties(contributed["readers"])
            if taken:
                note["from"] = f"contributed, overriding {', '.join(taken)}"
            table = table.merged_with(contributed["by_name"],
                                      contributed["by_category"],
                                      contributed["readers"])
        notes.append(note)
    for path in spec_modules(settings):
        note = {"label": "scope spec", "path": path, "from": "settings",
                "required": False, "problem": None}
        try:
            module = importlib.import_module(path)
            by_name = getattr(module, "SCOPES", None) or {}
            by_category = getattr(module, "SCOPES_BY_CATEGORY", None) or {}
            readers = getattr(module, "SPAN_READERS", None) or {}
            if not isinstance(by_name, dict) or not isinstance(by_category, dict):
                raise TypeError("SCOPES must be a dict of scope name to Scope")
            # Readers alone are a legitimate module: replacing this app's
            # reading of a payload without touching how it shows is exactly
            # the override design principle 1 allows (ADR 17). What is not
            # legitimate is a module that does nothing, which is what this
            # catches.
            if not by_name and not by_category and not readers:
                raise ValueError("no SCOPES or SPAN_READERS in module")
            faults = check_table(by_name, by_category) + check_readers(readers)
            if faults:
                raise ValueError("; ".join(faults))
        except Exception as exc:  # noqa: BLE001 - third-party module
            note["problem"] = f"{type(exc).__name__}: {exc}"
            notes.append(note)
            continue
        taken = (table.overrides_of(by_name, by_category, readers)
                 + _shadowed_properties(readers))
        if taken:
            note["from"] = f"settings, overriding {', '.join(taken)}"
        table = table.merged_with(by_name, by_category, readers)
        notes.append(note)
    return table, notes


def _shadowed_properties(readers) -> list:
    """Which of these readers stand in front of a `Span` property of the
    same name, for the startup line.

    A reader beating a property is legitimate — someone whose payload
    differs from hermes' replaces our reading of it (design principle 1) —
    but it is the kind of override nothing else would show: the rows keep
    rendering, with different values in them. `SpecTable.overrides_of`
    cannot see it, because a property is not in any table.
    """
    return sorted(f"Span.{name}" for name in (readers or {})
                  if hasattr(Span, name))


def sources(settings):
    """What this tab reads, for the startup banner (ADR 5).

    Not required: the log is allowed to be absent — hermes may simply not have
    run with the exporter on — and the tab explains that itself rather than
    being replaced by a shell error page. Contributed spec modules are listed
    the same way, so an override or a failed import is visible at startup
    beside the log it renders.
    """
    path = atof_path(settings)
    entries = [{"label": "ATOF log", "path": path, "required": False,
                "problem": None if os.path.exists(path) else "no such file"}]
    entries.append({"label": "ATOF index",
                    "path": index_db_path(settings),
                    "required": False, "problem": None})
    entries.extend(spec_table(settings)[1])
    entries.extend(usage_shape_table(settings)[1])
    # The skill roots the skill view (ADR 22) is confined to. Not required —
    # a root that is absent simply holds no skills to open — but named here
    # because they are files this tab now reads, and a reader looking for
    # "what does this touch" should find them.
    for root in skills.skill_roots(settings):
        entries.append({"label": "skill root", "path": root, "required": False,
                        "problem": None if os.path.isdir(root)
                        else "no such directory"})
    return entries


def init_app(app, settings):
    """Resolve the source once, at registration, so every request and the
    banner agree on which file this tab is reading.

    The spec table is resolved here too: importing a contributed module is
    startup work, not per-request work, and a turn page polling every 2 s
    must not pay for it.
    """
    app.config["ATOF_PATH"] = atof_path(settings)
    app.config["ATOF_INDEX_DB"] = index_db_path(settings)
    app.config["SCOPE_SPECS"] = spec_table(settings, app)[0]
    app.config["USAGE_SHAPES"] = usage_shape_table(settings)[0]
    # Resolved once, like the sources above: the skill view (ADR 22) confines
    # every read to these, and reading config.yaml on each request is startup
    # work a polling page must not pay for.
    app.config["SKILL_ROOTS"] = skills.skill_roots(settings)
    _warm_index(app)


def _index_report(state) -> str:
    """One console line saying what bringing the index up to date came to.

    The distinction the reader is usually after is reused-vs-rebuilt: a
    rebuild re-reads the whole log (~20 s on a multi-gigabyte one) and is the
    reason a first page can stall, so it names why it happened and how long it
    took. Reuse and a small extend are the fast, ordinary cases.
    """
    # "ATOF log lines" not just "lines": the count is how far through the log
    # the index reaches, not a number of turns, spans or index rows.
    lines = f"{state.indexed_lines:,} ATOF log lines"
    if state.action == "rebuilt":
        return (f"ATOF index rebuilt in {state.seconds:.1f}s ({lines}) — "
                f"{state.reason}")
    if state.action == "extended":
        return (f"ATOF index extended in {state.seconds:.1f}s "
                f"(+{state.lines:,} lines, {lines} total)")
    return f"ATOF index reused from cache ({lines})"


def _warm_index(app):
    """Bring the index up to date once at startup, and say on the console
    whether that reused the cache or rebuilt it.

    The refresh happens on every request anyway (`_assembly`); doing it here
    as well moves a possible full rebuild out of the first page load — where
    it looks like a hang — and into startup, where the line below explains the
    pause. The index is stored under the key `get_index` reads, so the first
    request finds it already current rather than refreshing a second time.

    Degrades per component (design principle 1): a log that is absent, or an
    index that will not open, is the tab's own to report at request time, so
    neither stops the app coming up.
    """
    path = app.config["ATOF_PATH"]
    if not os.path.exists(path):
        return
    db_path = app.config.get("ATOF_INDEX_DB") or default_index_path(path)
    shapes = app.config.get("USAGE_SHAPES")
    index = AtofIndex(path, db_path, shapes)
    try:
        # Said before the work, not after: a from-scratch rebuild is the one
        # case that goes quiet for many seconds, and a reader watching an
        # unexplained pause is exactly who this line is for.
        reason = index.rebuild_pending()
        if reason is not None:
            console(f"Turns: ATOF index is stale ({reason}); rebuilding — this "
                    "can take ~20s on a large log…")
        state = index.refresh()
    except Exception as exc:  # noqa: BLE001 - a bad index must not stop serving
        console(f"Turns: could not read the ATOF index "
                f"({type(exc).__name__}: {exc}); will retry on first request")
        return
    app.extensions["atof_index"] = index
    console(f"Turns: {_index_report(state)}")


def get_index() -> AtofIndex:
    """The app-lifetime index for the configured ATOF path."""
    path = current_app.config["ATOF_PATH"]
    db_path = current_app.config.get("ATOF_INDEX_DB") or default_index_path(path)
    shapes = current_app.config.get("USAGE_SHAPES")
    index = current_app.extensions.get("atof_index")
    if (index is None or index.log_path != path or index.db_path != db_path
            or index.usage_shapes != (tuple(shapes) if shapes else None)):
        index = AtofIndex(path, db_path, shapes)
        current_app.extensions["atof_index"] = index
    return index


@bp.app_template_filter("us_dur")
def us_dur(us):
    """Microseconds → a duration in seconds, two decimals; em dash when unknown.

    Always seconds — never ms or µs — so every turn and span time reads on one
    scale and the columns line up. Anything under 5 ms rounds to 0.00 s, which
    is fine when the point is where a turn's time went.
    """
    if us is None:
        return "—"
    return f"{us / 1_000_000:.2f} s"


@bp.app_template_filter("us_mmss")
def us_mmss(us):
    """Microseconds → m:ss (minutes and whole seconds); em dash when unknown.

    The all-turns table uses this instead of us_dur: turns easily run for
    minutes, and a raw second count ("372 s") is hard to read at a glance where
    6:12 is not. Per-turn pages and everywhere else keep the two-decimal us_dur.
    Rounded to the nearest second; durations here are never negative.
    """
    if us is None:
        return "—"
    total = int(us / 1_000_000 + 0.5)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


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


@bp.app_template_filter("bytes_human")
def bytes_human(n):
    """A byte count in the largest unit that keeps it short: 3.2 GB, 48 MB,
    900 KB, 512 B. Decimal (1000-based) units, and one decimal place only
    where it adds information — below 10 in the chosen unit."""
    if n is None:
        return "—"
    size = float(n)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if size < 1000 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"
        size /= 1000
    return f"{size:.0f} {units[-1]}"


@bp.app_template_filter("us_time")
def us_time(us):
    if us is None:
        return "—"
    stamp = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _source_problem():
    """The loud no-source state; None when the file is readable.

    There is no "unconfigured" case any more: a path is always resolved (the
    setting, else the default), so an absent log is always a path that does
    not exist — which is the more useful thing to say.
    """
    atof_path = current_app.config["ATOF_PATH"]
    if not os.path.exists(atof_path):
        return render_template("turns/index.html", state="missing",
                               atof_path=atof_path)
    return None


def _assembly():
    """The turn model, rebuilt only when the index has actually moved.

    A live page polls every 2–3 s and assembly is the expensive step left
    once payloads are out of the picture, so the result is kept until the
    index reports a different extent or a rebuild. Both pages call this, so
    the strip and the waterfall always describe the same moment.
    """
    index = get_index()
    state = index.refresh()
    key = (state.generation, state.indexed_bytes, state.indexed_lines)
    cached = current_app.extensions.get("atof_assembly")
    if cached is not None and cached[0] == key:
        return cached[1], cached[2]
    assembly = assemble(index.events(), index.stream_activity(),
                        index.stream_usage())
    errors = index.parse_errors()
    current_app.extensions["atof_assembly"] = (key, assembly, errors)
    return assembly, errors


def _inflight_entries(assembly):
    """Still-running turns, newest first, dressed for the status strip.

    Three kinds of open turn are not running and are dropped: one superseded
    by a later turn in its session (Turn.is_live), a subagent the parent has
    already reported stopped, and one silent past STALE_AFTER_US — a lost end
    mark left open, which would otherwise sit in the strip indefinitely.
    """
    now_us = int(time.time() * 1_000_000)
    stopped = assembly.finished_subagent_sessions
    turns = sorted(
        (t for s in assembly.sessions for t in s.turns
         if t.is_live and t.session_id not in stopped
         and now_us - t.last_activity_us <= STALE_AFTER_US),
        key=lambda t: t.start_us,
        reverse=True,
    )
    return [{
        "turn": t,
        "elapsed_us": now_us - t.start_us,
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
    atof_path = current_app.config["ATOF_PATH"]
    # refresh() is the same cheap call _assembly just made; it carries the
    # count of log lines the index has read — one entry per JSONL line.
    index_state = get_index().refresh()
    return render_template(
        "turns/index.html",
        state="ok",
        atof_path=atof_path,
        atof_size=os.path.getsize(atof_path),
        atof_entries=index_state.indexed_lines,
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


def _find_turn(assembly, session_id, start_us):
    """The turn a URL names, or None. The pair is what identifies one.

    A turn's `start_us` is its URL key, but assembly can revise it earlier: a
    `hermes.turn` scope and its prompt boundary mark are seen a few ms apart
    (the two exporters race), the scope merges into the mark-built turn, and
    the start is pulled back to the earliest of the two (assembler `min`). A
    URL captured before that revision — follow mode navigating to a turn the
    strip showed mid-race — then names a start no turn has any more. So match
    exactly first, and otherwise fall back to the turn whose interval now holds
    that instant; the route redirects a fallback hit to the turn's live key.
    """
    session = next((s for s in assembly.sessions
                    if s.session_id == session_id), None)
    if session is None:
        return None
    exact = next((t for t in session.turns if t.start_us == start_us), None)
    return exact or containing_turn(session, start_us)


@bp.route("/turn/<session_id>/<int:start_us>")
def turn(session_id, start_us):
    problem = _source_problem()
    if problem is not None:
        return problem
    assembly, _ = _assembly()
    found = _find_turn(assembly, session_id, start_us)
    if found is None:
        abort(404)
    # Matched by the interval fallback, not exactly: the key in the URL is a
    # start this turn no longer carries. Send the reader to its live key so the
    # address bar settles and the page's own polling stops chasing the old one.
    if found.start_us != start_us:
        return redirect(url_for("turns.turn", session_id=session_id,
                                start_us=found.start_us))
    # The payloads live in the log; read back the ones this turn needs and
    # no others (ADR 11). Hydrating into the cached assembly means a reload
    # of the same turn costs nothing, and the next append drops the lot.
    hydrate_turn(found, current_app.config["ATOF_PATH"],
                 current_app.config.get("USAGE_SHAPES"))
    # After hydration and only for this turn: it reads one memory span's
    # store listing to make sense of another's, which needs the end payloads
    # the line above just fetched.
    resolve_memory_entries(found)
    # Scale for the bars: a turn still in flight is drawn against the last
    # thing we saw in it.
    span_edges = [s.end_us or s.start_us for s in found.spans]
    scale_end = found.end_us or max(span_edges, default=found.start_us)
    scale_us = max(scale_end - found.start_us, 1)
    older, newer = _neighbours(assembly, found)
    table = _spec_table()
    accessors = _accessors()
    return render_template(
        "turns/turn.html",
        turn=found,
        current=found,
        scale_us=scale_us,
        inflight=_inflight_entries(assembly),
        older=older,
        newer=newer,
        scope_rows=lambda span: rows_for(span, table, accessors),
        scope_render=lambda span: render_macro(span, table),
        # For the hand-written macros, which have no Field to carry a
        # `full=` and so ask for the link by the key their scope declared.
        scope_full=lambda span, key: full_link(span, table, key, accessors),
    )


def _spec_table():
    """The resolved spec table, or this tree's own when nothing resolved it.

    Both the turn page and the full page need it, and a bare test app with
    no `init_app` behind it still renders.
    """
    return current_app.config.get("SCOPE_SPECS") or SpecTable(
        dict(SCOPES), dict(SCOPES_BY_CATEGORY))


def _find_span(assembly, span_uuid):
    """(turn, span) for a uuid, or (None, None).

    Turns first, then the spans that landed in none — an llm call parented
    to the session rather than to a turn is exactly the kind whose prompt is
    otherwise nowhere on the site, so addressing it by uuid is what makes it
    readable at all.
    """
    for session in assembly.sessions:
        for turn in session.turns:
            for span in turn.spans:
                if span.uuid == span_uuid:
                    return turn, span
    for session in assembly.sessions:
        for span in session.unassigned_spans:
            if span.uuid == span_uuid:
                return None, span
    return None, None


@bp.route("/span/<span_uuid>/<key>")
def span_full(span_uuid, key):
    """One whole value of one span, on a page of its own (ADR 12).

    Addressed by span uuid rather than through its turn: the uuid is already
    on the row and in the copy button beside it, it does not change when the
    assembly does, and it reaches a span no turn claimed.

    `raw` shows the same value as characters. Markdown is a reading of the
    text, and a reader who suspects the reading needs somewhere to check —
    which is also the only view a value's whitespace survives.
    """
    problem = _source_problem()
    if problem is not None:
        return problem
    assembly, _ = _assembly()
    turn, span = _find_span(assembly, span_uuid)
    if span is None:
        abort(404)
    table = _spec_table()
    full = full_for(span, table, key)
    if full is None:
        abort(404)
    # One span's payloads, not its turn's: this page shows one value.
    hydrate_span(span, current_app.config["ATOF_PATH"],
                 current_app.config.get("USAGE_SHAPES"))
    accessors = _accessors()
    # The readers as well as the accessors: a whole value can be a reading of
    # a payload just as a row can, and the two pages must resolve a source
    # the same way or a `Full` would open on nothing (ADR 17).
    readers = table.readers
    rendered = fulltext.render(resolve_full(span, full, accessors, readers),
                               full.render)
    return render_template(
        "turns/full.html",
        span=span,
        turn=turn,
        full=full,
        title=resolve_source(full.title, span, accessors, readers) or full.key,
        note=resolve_source(full.note, span, accessors, readers),
        rendered=rendered,
        raw=request.args.get("raw") is not None,
    )


@bp.route("/skill")
def skill():
    """A skill on disk, on a page of its own (ADR 22).

    Reached from a `skill_view`/`skill_manage` row. The `name` and `path` a
    scope carries come from the log and are not trusted: `path` is admitted
    only when it resolves inside a configured root, and everything served —
    the manifest, a sidebar file, a chosen `sel` — is held to the same
    containment. A skill outside every root, or no roots at all, is refused
    rather than read.
    """
    roots = current_app.config.get("SKILL_ROOTS") or []
    name = request.args.get("name") or None
    path = request.args.get("path") or None
    sel = request.args.get("sel") or None
    span_uuid = request.args.get("span") or None
    raw = request.args.get("raw") is not None

    # The turn this skill was opened from, for the "back to the turn" link —
    # found by span uuid the same way the full-value page finds it. Best-effort:
    # if the log is absent the skill still reads, the link just cannot point
    # at a turn it could not assemble.
    turn = None
    if span_uuid and _source_problem() is None:
        turn, _ = _find_span(_assembly()[0], span_uuid)

    # No roots configured (or pyyaml/config.yaml unreadable and no standard
    # root on disk): the feature is simply unavailable, said on its own page
    # rather than as a 404 that reads like a missing skill.
    if not roots:
        return render_template("turns/skill.html", available=False,
                               skill_name=name, turn=turn), 404

    skill_dir = skills.resolve_skill_dir(roots, name, path)
    if skill_dir is None:
        abort(404)

    # Which file to show: an explicit sidebar pick (validated), else the
    # scope's own file when it is one inside this skill, else the manifest.
    target, rel, text, problem = None, None, None, None
    rendered, frontmatter = None, None
    if sel is not None:
        target = skills.safe_target(skill_dir, sel)
        if target is None:
            abort(404)
    elif path and skills.containing_root(path, [skill_dir]) \
            and os.path.isfile(os.path.realpath(path)):
        target = os.path.realpath(path)
    else:
        manifest = os.path.join(skill_dir, skills.MANIFEST)
        target = manifest if os.path.isfile(manifest) else None

    if target is not None:
        rel = os.path.relpath(target, skill_dir)
        text, problem = skills.read_text(target)
        if text is not None and skills.is_markdown(target) and not raw:
            # Held out of the markdown and shown verbatim: a `key:` line closed
            # by `---` is a setext heading to CommonMark, so the frontmatter
            # would otherwise render as one bold heading (ADR 22).
            frontmatter, body = skills.split_frontmatter(text)
            rendered = fulltext.render(body, "markdown")
        elif text is not None:
            rendered = fulltext.render(text, "text")

    return render_template(
        "turns/skill.html",
        available=True,
        skill_name=name or os.path.basename(skill_dir),
        skill_dir=skill_dir,
        files=skills.list_skill_files(skill_dir),
        current=rel,
        rendered=rendered,
        frontmatter=frontmatter,
        read_problem=problem,
        turn=turn,
        nav_params={"name": name, "path": path, "span": span_uuid},
        raw=raw,
    )
