"""Prompt-timing view — per-turn waterfalls from the NeMo Relay ATOF stream.

Reader per docs/adr/0002: tailer (byte-offset incremental read) → parser
(JSONL line → typed event) → assembler (events → sessions → turns →
waterfall). Views run the tailer on each request and assemble in memory.

Per ADR 2's fail-open caveat, the page states loudly when the source is
unconfigured, missing, or silent instead of rendering an empty timeline,
and parse errors / assembly anomalies are always shown, never dropped.
"""

import os
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, render_template

from plugins.timing.assembler import assemble
from plugins.timing.tailer import AtofTailer

bp = Blueprint("timing", __name__)
TAB_LABEL = "Prompt timing"


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
        anomalies=assembly.anomalies,
        parse_errors=parse_errors,
    )


@bp.route("/turn/<session_id>/<int:start_us>")
def turn(session_id, start_us):
    problem = _source_problem()
    if problem is not None:
        return problem
    assembly, parse_errors = _assembly()
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
    return render_template(
        "timing/turn.html",
        turn=found,
        scale_us=scale_us,
        parse_errors=parse_errors,
        anomalies=assembly.anomalies,
    )
