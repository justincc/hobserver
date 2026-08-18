"""Stub plugins for the shell's own tests.

The shell knows nothing about any particular tab, so its tests should not
either: they build apps from these neutral stubs instead of the in-tree tabs,
and prove the shell works with *any* plugin. Written to tmp_path and imported by
module path, the same way a real out-of-tree plugin loads.
"""

import sys

# A minimal but complete tab: the required attributes, an index route and a
# sub-route, and a `sources` hook whose state a test drives through settings
# (path / required / problem) — enough to exercise the banner and the
# unavailable-source paths without naming a real plugin.
STUB = '''\
from flask import Blueprint

PLUGIN_API = 1
bp = Blueprint("{name}", __name__)
TAB_LABEL = "{label}"
URL_PREFIX = "{prefix}"


def sources(settings):
    return [{{"label": "src", "path": settings.get("path", "/nowhere"),
             "required": settings.get("required", False),
             "problem": settings.get("problem")}}]


@bp.route("/")
def index():
    from flask import render_template
    return render_template("base.html")   # the shell chrome: tab bar, status link


@bp.route("/thing/<item>")
def thing(item):
    return "thing " + item
'''


def write_stub(tmp_path, name, label, prefix):
    """Write a stub plugin to tmp_path, importable as `name`."""
    (tmp_path / f"{name}.py").write_text(
        STUB.format(name=name, label=label, prefix=prefix))
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    sys.modules.pop(name, None)
    return name
