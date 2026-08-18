"""Root conftest: one plugin-neutral, autouse fixture for the whole tree.

Importable test helpers (`make_app`, `REPO_ROOT`) live in `testkit.py`, which
has a unique name — with a conftest at more than one depth, `from conftest
import …` would be ambiguous. This file names no tab: the shell's own tests
build apps from stub plugins (tests/conftest.py), and each plugin's tests build
their own and own their test data (plugins/<name>/tests/), so removing a plugin
cannot break the shell's suite or another plugin's.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep any cache a tab derives out of the developer's real cache dir.

    The Turns index (ADR 11) derives its default path from the log path, and
    tests point at a different log each run, so without this a test session
    leaves a fresh SQLite file in ~/.cache for every app it builds.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
