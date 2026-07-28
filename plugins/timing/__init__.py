"""The Prompts view (blueprint `timing`) — per-turn waterfalls from the
NeMo Relay ATOF stream.

Reader per docs/adr/0002: tailer (byte-offset incremental read) → parser
(JSONL line → typed event) → assembler (events → sessions → turns →
waterfall). Views run the tailer on each request and assemble in memory.

Per ADR 2's fail-open caveat, the page states loudly when the source is
unconfigured, missing, or silent instead of rendering an empty timeline,
and parse errors / assembly anomalies are always surfaced, never dropped —
as collapsed problem sections on the turn-list page only, so turn pages
stay uncluttered.
"""

import os
import time
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, render_template

from plugins.timing.assembler import assemble
from plugins.timing.tailer import AtofTailer

bp = Blueprint("timing", __name__)
TAB_LABEL = "Prompts"

# An in-flight turn silent this long is probably a lost end mark, not a
# running prompt: still listed in the strip, but never auto-followed.
STALE_AFTER_US = 10 * 60 * 1_000_000


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


@bp.app_template_filter("us_time")
def us_time(us):
    if us is None:
        return "—"
    stamp = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _source_problem():
    """The loud no-source states; None when the file is readable."""
    atof_path = current_app.config.get("ATOF_PATH")
    if not atof_path:
        return render_template("timing/index.html", state="unconfigured")
    if not os.path.exists(atof_path):
        return render_template("timing/index.html", state="missing",
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
        "timing/index.html",
        state="ok",
        atof_path=current_app.config["ATOF_PATH"],
        turns=turns,
        last_us=last_us,
        inflight=_inflight_entries(assembly),
        anomalies=assembly.anomalies,
        parse_errors=parse_errors,
    )


def _prior_memory_texts(turn):
    """{span uuid: prior-text record} for the turn's mem0_update/mem0_delete
    spans — what each memory said before the span changed it.

    The lookup belongs to the memory plugin, which owns the event log; this
    tab reaches it through `app.extensions` and simply does without when that
    plugin is not registered (ADR 4 — link and call, never open another
    plugin's source). One indexed row per such span, and most turns have
    none, so this costs nothing on the 2 s poll.
    """
    lookup = current_app.extensions.get("memory_prior_text")
    if lookup is None:
        return {}
    prior = {}
    for span in turn.spans:
        if span.memory_id is None:
            continue
        record = lookup(span.memory_id, span.start_us / 1_000_000)
        if record is not None:
            prior[span.uuid] = record
    return prior


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
    return render_template(
        "timing/turn.html",
        turn=found,
        current=found,
        scale_us=scale_us,
        inflight=_inflight_entries(assembly),
        older=older,
        newer=newer,
        prior_memory=_prior_memory_texts(found),
    )
