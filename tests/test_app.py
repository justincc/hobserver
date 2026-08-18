"""Shell tests: plugin registration, tab bar, the root redirect, the banner.

The shell knows nothing about any particular tab, so these tests do not either:
they build apps from stub plugins (tests/conftest.py, stubs.py) — Alpha at
/alpha/, Beta at /nested/beta/ — and never name an in-tree tab. Where a tab's
own path resolution is tested, that lives with the tab (plugins/<name>/tests/).
"""

import logging
import os

import app as app_module
import hermes_paths
import tabs as tabs_module

from stubs import write_stub


def load(entries):
    """Config entries -> loaded tabs, the way main() does it."""
    return tabs_module.load_tabs(tabs_module.parse_config({"plugins": entries}))


def test_root_redirects_to_the_first_tab(client):
    # the leftmost tab is the landing page, so tab order decides this
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/alpha/")


def test_tab_bar_lists_all_plugins_in_order(client):
    page = client.get("/alpha/").get_data(as_text=True)
    assert "/nested/beta/" in page
    assert page.index("Alpha") < page.index("Beta")


def test_tab_bar_marks_active_tab(client):
    alpha_page = client.get("/alpha/").get_data(as_text=True)
    assert '/alpha/" class="active"' in alpha_page
    beta_page = client.get("/nested/beta/").get_data(as_text=True)
    assert '/nested/beta/" class="active"' in beta_page


def test_a_plugin_is_served_under_its_own_url_prefix(client):
    # URL_PREFIX is independent of the blueprint name, and may be a path
    assert client.get("/nested/beta/thing/1").status_code == 200
    assert client.get("/alpha/").status_code == 200


def test_templates_reload_under_dev_and_not_otherwise(client, stub_entries):
    """One switch for every kind of edit (ADR 15). Both directions are
    asserted because each is a promise: --dev must pick a template edit up on
    the next request without a restart, and a plain run must not pick it up
    at all, however long it serves for."""
    tabs = load(stub_entries)
    dev = app_module.create_app(tabs, dev=True)
    assert dev.config["TEMPLATES_AUTO_RELOAD"] is True
    assert dev.jinja_env.auto_reload is True

    plain = app_module.create_app(tabs)
    assert plain.config["TEMPLATES_AUTO_RELOAD"] is False
    assert plain.jinja_env.auto_reload is False

    # …and the default is the plain one, so nothing gets it by accident
    assert client.application.jinja_env.auto_reload is False


# --- the hermes config directory (hermes_paths is the shell's own) ----------


def test_hermes_home_gives_the_config_dir(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    assert hermes_paths.hermes_config_dir() == "/srv/hermes/config"


def test_hermes_home_is_normalized(monkeypatch):
    # the agent conventionally exports <checkout>/hermes-agent/../config
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/hermes-agent/../config")
    assert hermes_paths.hermes_config_dir() == "/srv/hermes/config"


def test_config_dir_falls_back_without_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert hermes_paths.hermes_config_dir() == hermes_paths.FALLBACK_CONFIG_DIR


def test_the_fallback_matches_the_agents_own_default(monkeypatch):
    """The fallback is what hermes-agent itself uses with HERMES_HOME unset —
    `~/.hermes`, not a `config` subdirectory of it. An installation that
    configured nothing has to find the files the agent actually wrote; anyone
    whose hermes lives elsewhere exports HERMES_HOME and never meets this."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fallback = hermes_paths.FALLBACK_CONFIG_DIR
    assert fallback == os.path.join(os.path.expanduser("~"), ".hermes")


# --- the startup banner ------------------------------------------------------


def test_banner_reports_each_tabs_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    a = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    b = write_stub(tmp_path, "beta_tab", "Beta", "nested/beta")
    tabs = load([
        {"plugin": a, "settings": {"path": "/nope/absent.log",
                                   "problem": "no such file"}},
        {"plugin": b, "settings": {"path": "/tmp/here.log"}},
    ])
    banner = app_module.startup_banner(tabs, 5090, "hobserver.toml")
    assert "/tmp/here.log  [ok]" in banner
    assert "/nope/absent.log  [MISSING" in banner   # optional source, absent
    # the mark ends the line, as it does on the source lines below it
    assert "Alpha (alpha_tab)  [ok]" in banner
    assert "Beta (beta_tab)  [ok]" in banner
    assert "/srv/hermes/config" in banner
    assert "hobserver.toml" in banner
    assert "http://127.0.0.1:5090/" in banner   # loopback by default


def test_banner_lists_a_path_without_saying_what_supplied_it(tmp_path):
    a = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    for settings in ({}, {"path": "/tmp/a.log"}):
        banner = app_module.startup_banner(
            load([{"plugin": a, "settings": settings}]), 5090, "hobserver.toml")
        source_lines = [line for line in banner.splitlines()
                        if line.startswith("    ")]
        assert source_lines
        assert all("(from" not in line for line in source_lines)


def test_banner_reports_an_unusable_required_source(tmp_path):
    """A path that exists but is unusable must not read [ok], and takes its tab
    out of service — the state a bare 500 used to hide."""
    a = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    tabs = load([{"plugin": a, "settings": {
        "path": "/tmp/bad", "required": True, "problem": "not a regular file"}}])
    banner = app_module.startup_banner(tabs, 5090, "hobserver.toml")
    assert "UNAVAILABLE (src: not a regular file)" in banner
    assert "/tmp/bad  [UNUSABLE (not a regular file)]" in banner


def test_banner_flags_an_unset_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    banner = app_module.startup_banner([], 5090, "hobserver.toml")
    assert "HERMES_HOME not set" in banner
    assert "/.hermes (default location" in banner   # …and what stood in for it


def test_banner_names_where_the_hermes_dir_came_from(monkeypatch):
    # one line, not the variable and the path it resolves to
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/hermes-agent/../config")
    banner = app_module.startup_banner([], 5090, "hobserver.toml")
    assert "hermes dir   /srv/hermes/config (from HERMES_HOME)" in banner


def test_banner_points_a_fresh_checkout_at_the_example():
    # the one run where the user has seen no config file: say it works with
    # none, and name the example to copy. Only on the built-in origin.
    fresh = app_module.startup_banner([], 5090, tabs_module.BUILTIN_ORIGIN)
    assert "running out of the box" in fresh
    assert "copy hobserver.example.toml to hobserver.toml" in fresh

    configured = app_module.startup_banner([], 5090, "hobserver.toml")
    assert "running out of the box" not in configured
    assert "hobserver.example.toml" not in configured


def test_banner_separates_what_resolved_from_tabs_from_serving(tmp_path):
    a = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    tabs = load([{"plugin": a}])
    banner = app_module.startup_banner(tabs, 5090, "hobserver.toml")
    groups = banner.split("\n\n")
    assert len(groups) == 3
    assert groups[0].startswith("CONFIGURATION")
    assert groups[1].startswith("  tab   ")
    assert groups[2].startswith("  reloading")
    # a blank line ends nothing: it only ever comes between two groups
    assert not banner.endswith("\n")
    assert "\n\n\n" not in banner


def test_banner_puts_a_blank_line_between_tabs(tmp_path):
    # each tab is its own block, divided like the sections are, so two tabs'
    # reports do not run together
    a = write_stub(tmp_path, "alpha_tab", "Alpha", "alpha")
    b = write_stub(tmp_path, "beta_tab", "Beta", "nested/beta")
    tabs = load([{"plugin": a}, {"plugin": b}])
    banner = app_module.startup_banner(tabs, 5090, "hobserver.toml")
    alpha_at = banner.index("Alpha (alpha_tab)")
    beta_at = banner.index("Beta (beta_tab)")
    between = banner[alpha_at:beta_at]
    assert "\n\n  tab" in between        # a blank line, then the next tab
    assert "\n\n\n" not in banner        # exactly one, never a doubled gap


def test_banner_takes_no_blank_line_with_an_empty_group():
    banner = app_module.startup_banner([], 5090, "hobserver.toml", serving=False)
    assert not banner.endswith("\n")
    assert len(banner.split("\n\n")) == 2


def test_banner_does_not_claim_to_listen_when_nothing_loaded():
    banner = app_module.startup_banner([], 5090, "hobserver.toml", serving=False)
    assert "none configured" in banner
    assert "listening" not in banner


# --- /_status ----------------------------------------------------------------


def test_status_endpoint_reports_traffic(client):
    client.get("/alpha/")
    client.get("/nested/beta/")
    body = client.get("/_status").get_data(as_text=True)
    assert "/alpha/" in body and "/nested/beta/" in body
    assert "200x1" in body


def test_status_endpoint_excludes_itself(client):
    # checking the tally must not register as traffic, or its own last-seen
    # time would always look fresh
    client.get("/_status")
    body = client.get("/_status").get_data(as_text=True)
    assert "/_status" not in body


def test_status_endpoint_is_plain_text(client):
    resp = client.get("/_status")
    assert resp.headers["Content-Type"].startswith("text/plain")


def test_status_endpoint_refreshes_itself(client):
    resp = client.get("/_status")
    assert resp.headers["Refresh"] == "3"
    # still plain text, so curl and a browser see the same thing
    assert resp.headers["Content-Type"].startswith("text/plain")


def test_status_link_in_the_tab_bar_opens_a_new_tab(client):
    # the tally is for checking while a page sits polling, so following it
    # in place would defeat the purpose
    page = client.get("/alpha/").get_data(as_text=True)
    assert 'href="/_status"' in page
    assert 'target="_blank"' in page
    assert 'class="status-link"' in page
    # named so it cannot be read as agent/LLM requests, which is what every
    # other view on this app shows
    assert ">Hobserver status</a>" in page
    assert ">requests</a>" not in page
    # a diagnostic, not a view: it never takes the active-tab styling
    assert '/_status" class="active"' not in page


def test_status_link_is_on_every_page(client):
    for url in ("/alpha/", "/nested/beta/"):
        assert 'href="/_status"' in client.get(url).get_data(as_text=True)


def test_errors_reach_the_console_and_successes_do_not(client, caplog):
    """The console rule, exercised through the app rather than through a
    server's access log — which is the point of moving it here: waitress logs
    no requests, and never sees a 404 at all, since Flask answers it."""
    with caplog.at_level(logging.WARNING):
        client.get("/alpha/")
        assert caplog.records == []
        client.get("/no/such/page")
    assert [r.getMessage() for r in caplog.records] == \
        ["GET /no/such/page -> 404"]


def test_a_failing_poll_is_logged_with_its_query_string(client, caplog):
    # a page left open across a URL rename polls the old address forever; the
    # `since=` is what says which poll it is, so the log line has to carry it
    with caplog.at_level(logging.WARNING):
        client.get("/alpha/fragment/renamed?since=42")
    assert [r.getMessage() for r in caplog.records] == \
        ["GET /alpha/fragment/renamed?since=42 -> 404"]


def test_dev_flag_is_not_mistaken_for_a_config_file():
    # --dev may be written on either side of the config path
    assert app_module.parse_args([]) == (None, False)
    assert app_module.parse_args(["--dev"]) == (None, True)
    assert app_module.parse_args(["other.toml"]) == ("other.toml", False)
    assert app_module.parse_args(["--dev", "other.toml"]) == ("other.toml", True)
    assert app_module.parse_args(["other.toml", "--dev"]) == ("other.toml", True)
