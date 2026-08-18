"""Fixtures for the shell's own tests, scoped to tests/.

Apps here are built from stub plugins (stubs.py), never the in-tree tabs, so
the shell's suite proves the shell works with any plugin and does not break when
a tab is added, removed, or lifted out of the tree.
"""

import pytest

from testkit import make_app
from stubs import write_stub


@pytest.fixture
def stub_entries(tmp_path):
    """Two neutral tabs — Alpha at /alpha/, Beta at /nested/beta/ (a
    multi-segment prefix) — for the shell tests that need a running app."""
    alpha = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    beta = write_stub(tmp_path, "beta_tab", "Beta", "nested/beta")
    return [{"plugin": alpha}, {"plugin": beta}]


@pytest.fixture
def client(stub_entries):
    return make_app(stub_entries).test_client()
