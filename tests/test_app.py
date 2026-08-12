"""Shell tests: plugin registration, tab bar, the root redirect."""

import errno
import logging
import os

import pytest

import app as app_module
import hermes_paths
import tabs as tabs_module
from plugins import mem0, turns


def load(entries):
    """Config entries → loaded tabs, the way main() does it."""
    return tabs_module.load_tabs(tabs_module.parse_config({"tabs": entries}))


def test_root_redirects_to_the_first_tab(client):
    # the leftmost tab is the landing page, so tab order decides this
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/turns/")


def test_tab_bar_lists_all_plugins_in_order(client):
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert "/turns/" in page
    assert page.index("Turns") < page.index("Mem0")


def test_tab_bar_marks_active_tab(client):
    memory_page = client.get("/memory/mem0/").get_data(as_text=True)
    assert '/memory/mem0/" class="active"' in memory_page
    turns_page = client.get("/turns/").get_data(as_text=True)
    assert '/turns/" class="active"' in turns_page


def test_a_plugin_is_served_under_its_own_url_prefix(client):
    # URL_PREFIX is independent of the blueprint name, and may be a path
    assert client.get("/memory/mem0/event/1").status_code == 200
    assert client.get("/turns/").status_code == 200


def test_templates_reload_under_dev_and_not_otherwise(client):
    """One switch for every kind of edit (ADR 15). Both directions are
    asserted because each is a promise: --dev must pick a template edit up on
    the next request without a restart, and a plain run must not pick it up
    at all, however long it serves for."""
    tabs = load([{"module": "plugins.turns"}])
    dev = app_module.create_app(tabs, dev=True)
    assert dev.config["TEMPLATES_AUTO_RELOAD"] is True
    assert dev.jinja_env.auto_reload is True

    plain = app_module.create_app(tabs)
    assert plain.config["TEMPLATES_AUTO_RELOAD"] is False
    assert plain.jinja_env.auto_reload is False

    # …and the default is the plain one, so nothing gets it by accident
    assert client.application.jinja_env.auto_reload is False


def test_sources_derive_from_hermes_home(monkeypatch):
    # both in-tree plugins default off the hermes config dir, so a correctly
    # set HERMES_HOME is all that is needed to start with no config at all
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    monkeypatch.delenv("JMEM0_DB", raising=False)
    monkeypatch.delenv("ATOF_LOG", raising=False)
    assert hermes_paths.hermes_config_dir() == "/srv/hermes/config"
    assert mem0.db_path({}) == "/srv/hermes/config/jmem0_logged.db"
    assert turns.atof_path({}) == \
        "/srv/hermes/config/nemo-relay/atof/hermes-atof.jsonl"


def test_hermes_home_is_normalized(monkeypatch):
    # the agent conventionally exports <checkout>/hermes-agent/../config
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/hermes-agent/../config")
    assert hermes_paths.hermes_config_dir() == "/srv/hermes/config"


def test_sources_fall_back_without_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("JMEM0_DB", raising=False)
    assert hermes_paths.hermes_config_dir() == hermes_paths.FALLBACK_CONFIG_DIR
    assert mem0.db_path({}).endswith("/.hermes/jmem0_logged.db")


def test_the_fallback_matches_the_agents_own_default(monkeypatch):
    """The fallback is what hermes-agent itself uses with HERMES_HOME unset —
    `~/.hermes`, not a `config` subdirectory of it. An installation that
    configured nothing has to find the files the agent actually wrote; anyone
    whose hermes lives elsewhere exports HERMES_HOME and never meets this."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fallback = hermes_paths.FALLBACK_CONFIG_DIR
    assert fallback == os.path.join(os.path.expanduser("~"), ".hermes")


def test_env_vars_override_the_defaults(monkeypatch):
    # a plugin resolves its own source: setting, then env var, then default
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    monkeypatch.setenv("JMEM0_DB", "/tmp/other.db")
    monkeypatch.setenv("ATOF_LOG", "/tmp/other.jsonl")
    assert mem0.db_path({}) == "/tmp/other.db"
    assert turns.atof_path({}) == "/tmp/other.jsonl"


def test_settings_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("JMEM0_DB", "/tmp/env.db")
    monkeypatch.setenv("ATOF_LOG", "/tmp/env.jsonl")
    assert mem0.db_path({"db": "/tmp/set.db"}) == "/tmp/set.db"
    assert turns.atof_path({"atof_log": "/tmp/set.jsonl"}) == "/tmp/set.jsonl"


def test_banner_reports_each_tabs_sources(memory_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    tabs = load([
        {"module": "plugins.turns", "settings": {"atof_log": "/nope/absent.jsonl"}},
        {"module": "plugins.mem0", "settings": {"db": memory_db}},
    ])
    banner = app_module.startup_banner(tabs, 5090, "observer.toml")
    assert f"{memory_db}  [ok]" in banner
    assert "/nope/absent.jsonl  [MISSING" in banner   # allowed to be absent
    # the mark ends the line, as it does on the source lines below it
    assert "Turns (plugins.turns)  [ok]" in banner
    assert "Mem0 (plugins.mem0)  [ok]" in banner
    assert "/srv/hermes/config" in banner
    assert "observer.toml" in banner
    assert "http://0.0.0.0:5090/" in banner


def test_banner_lists_a_path_without_saying_what_supplied_it(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    monkeypatch.delenv("ATOF_LOG", raising=False)
    for entry in ({"module": "plugins.turns"},
                  {"module": "plugins.turns",
                   "settings": {"atof_log": "/tmp/a.jsonl"}}):
        banner = app_module.startup_banner(load([entry]), 5090, "observer.toml")
        source_lines = [line for line in banner.splitlines()
                        if line.startswith("    ")]
        assert source_lines
        assert all("(from" not in line for line in source_lines)


def test_banner_reports_an_unusable_database(monkeypatch):
    """A path that exists but is not an event log must not read [ok] — that
    was the stray `app.py .` failure, which only showed up as a 500. It no
    longer exits the app, so the banner is where it has to be obvious."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    tabs = load([{"module": "plugins.mem0", "settings": {"db": "."}}])
    banner = app_module.startup_banner(tabs, 5090, "observer.toml")
    assert "UNAVAILABLE (event db: not a regular file)" in banner
    assert ".  [UNUSABLE (not a regular file)]" in banner


def test_banner_flags_an_unset_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    banner = app_module.startup_banner([], 5090, "built-in defaults")
    assert "HERMES_HOME not set" in banner
    assert "/.hermes (default location" in banner   # …and what stood in for it


def test_banner_names_where_the_hermes_dir_came_from(monkeypatch):
    # one line, not the variable and the path it resolves to
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/hermes-agent/../config")
    banner = app_module.startup_banner([], 5090, "observer.toml")
    assert "hermes dir   /srv/hermes/config (from HERMES_HOME)" in banner


def test_banner_separates_what_resolved_from_tabs_from_serving(memory_db):
    tabs = load([{"module": "plugins.mem0", "settings": {"db": memory_db}}])
    banner = app_module.startup_banner(tabs, 5090, "observer.toml")
    groups = banner.split("\n\n")
    assert len(groups) == 3
    assert groups[0].startswith("hermes-observer")
    assert groups[1].startswith("  tab   ")
    assert groups[2].startswith("  reloading")
    # a blank line ends nothing: it only ever comes between two groups
    assert not banner.endswith("\n")
    assert "\n\n\n" not in banner


def test_banner_takes_no_blank_line_with_an_empty_group():
    banner = app_module.startup_banner([], 5090, "observer.toml", serving=False)
    assert not banner.endswith("\n")
    assert len(banner.split("\n\n")) == 2


def test_banner_does_not_claim_to_listen_when_nothing_loaded():
    banner = app_module.startup_banner([], 5090, "observer.toml", serving=False)
    assert "none configured" in banner
    assert "listening" not in banner


def test_status_endpoint_reports_traffic(client):
    client.get("/memory/mem0/")
    client.get("/turns/")
    body = client.get("/_status").get_data(as_text=True)
    assert "/memory/mem0/" in body and "/turns/" in body
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
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert 'href="/_status"' in page
    assert 'target="_blank"' in page
    assert 'class="status-link"' in page
    # named so it cannot be read as agent/LLM requests, which is what every
    # other view on this app shows
    assert ">observer status</a>" in page
    assert ">requests</a>" not in page
    # a diagnostic, not a view: it never takes the active-tab styling
    assert '/_status" class="active"' not in page


def test_status_link_is_on_every_page(client):
    for url in ("/memory/mem0/", "/turns/"):
        assert 'href="/_status"' in client.get(url).get_data(as_text=True)


def test_errors_reach_the_console_and_successes_do_not(client, caplog):
    """The console rule, exercised through the app rather than through a
    server's access log — which is the point of moving it here: waitress logs
    no requests, and never sees a 404 at all, since Flask answers it."""
    with caplog.at_level(logging.WARNING):
        client.get("/turns/")
        assert caplog.records == []
        client.get("/no/such/page")
    assert [r.getMessage() for r in caplog.records] == \
        ["GET /no/such/page -> 404"]


def test_a_failing_poll_is_logged_with_its_query_string(client, caplog):
    # a page left open across a URL rename polls the old address forever; the
    # `since=` is what says which poll it is, so the log line has to carry it
    with caplog.at_level(logging.WARNING):
        client.get("/memory/mem0/fragment/renamed?since=42")
    assert [r.getMessage() for r in caplog.records] == \
        ["GET /memory/mem0/fragment/renamed?since=42 -> 404"]


def test_dev_flag_is_not_mistaken_for_a_config_file():
    # --dev may be written on either side of the config path
    assert app_module.parse_args([]) == (None, False)
    assert app_module.parse_args(["--dev"]) == (None, True)
    assert app_module.parse_args(["other.toml"]) == ("other.toml", False)
    assert app_module.parse_args(["--dev", "other.toml"]) == ("other.toml", True)
    assert app_module.parse_args(["other.toml", "--dev"]) == ("other.toml", True)


def test_banner_reports_reloading_in_both_states():
    # off is worth a line too: it answers why an edit changed nothing, and
    # names the switch where the reader is already looking
    off = app_module.startup_banner([], 5090, "x.toml")
    assert "reloading    no (specify --dev to pick up .py and template " \
           "edits)" in off
    on = app_module.startup_banner([], 5090, "x.toml", dev=True)
    assert "reloading    yes — .py edits restart, template edits land on " \
           "the next request" in on


def test_a_taken_port_says_so_instead_of_a_traceback(monkeypatch, capsys):
    def busy(app, host, port):
        raise OSError(errno.EADDRINUSE, "Address already in use")
    monkeypatch.setattr(app_module, "serve", busy)
    exits = []
    monkeypatch.setattr(app_module.os, "_exit", exits.append)

    app_module.serve_forever(object(), port=5090)

    message = capsys.readouterr().err
    assert "port 5090 is already in use" in message
    assert "Traceback" not in message
    assert exits == [1]


def test_other_serving_errors_are_not_swallowed(monkeypatch):
    # only the one diagnosis is claimed; anything else keeps its traceback
    def refused(app, host, port):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(app_module, "serve", refused)
    with pytest.raises(OSError, match="Permission denied"):
        app_module.serve_forever(object(), port=5090)
