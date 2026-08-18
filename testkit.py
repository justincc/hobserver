"""Shared, plugin-neutral test helpers.

A plain module rather than the root conftest so it has a unique import name:
with a conftest at more than one depth, `from conftest import …` is ambiguous,
but `from testkit import …` always means this file. Fixtures still live in the
conftests; only the importable helpers live here.
"""

import pathlib

import tabs as tabs_module
from app import create_app

# The repo root — the one fixed point every test can anchor on, whichever depth
# it lives at (tests/ or plugins/<name>/tests/).
REPO_ROOT = pathlib.Path(__file__).parent


def make_app(entries):
    """An app serving exactly the given plugin entries, in TESTING mode.

    Plugin-neutral: it names no tab. A test that wants the in-tree tabs passes
    them; the shell's own tests pass stub plugins (tests/stubs.py); each
    plugin's tests build their own (plugins/<name>/tests/). Tests describe tabs
    the way hobserver.toml does — a list of entries — so what they exercise is
    the real config path, not a private shortcut.
    """
    specs = tabs_module.parse_config({"plugins": entries})
    app = create_app(tabs_module.load_tabs(specs))
    app.config["TESTING"] = True
    return app
