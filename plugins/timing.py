"""Prompt-timing view — per-turn waterfalls from the NeMo Relay ATOF stream.

Stub: the ATOF reader (tailer/parser/assembler, see docs/adr/0002) is not
built yet. The page reports the configured source and, per ADR 2's fail-open
caveat, states loudly when there is nothing to read instead of rendering an
empty timeline.
"""

import os

from flask import Blueprint, current_app, render_template

bp = Blueprint("timing", __name__)
TAB_LABEL = "Prompt timing"


@bp.route("/")
def index():
    atof_path = current_app.config.get("ATOF_PATH")
    atof_exists = bool(atof_path) and os.path.exists(atof_path)
    return render_template(
        "timing/index.html", atof_path=atof_path, atof_exists=atof_exists
    )
