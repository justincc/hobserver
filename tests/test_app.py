"""Shell tests: plugin registration, tab bar, root and legacy redirects."""

import app as app_module


def test_root_redirects_to_memory_tab(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/memory/")


def test_tab_bar_lists_all_plugins(client):
    page = client.get("/memory/").get_data(as_text=True)
    assert "Memory" in page
    assert "Prompt timing" in page
    assert "/timing/" in page


def test_tab_bar_marks_active_tab(client):
    memory_page = client.get("/memory/").get_data(as_text=True)
    assert '/memory/" class="active"' in memory_page
    timing_page = client.get("/timing/").get_data(as_text=True)
    assert '/timing/" class="active"' in timing_page


def test_legacy_event_url_redirects(client):
    resp = client.get("/event/3")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/memory/event/3")


def test_legacy_fragment_url_redirects_with_query(client):
    resp = client.get("/fragment/events?since=3")
    assert resp.status_code == 302
    assert "/memory/fragment/events" in resp.headers["Location"]
    assert "since=3" in resp.headers["Location"]


def test_templates_reload_without_restart(client):
    # template edits must show up on the next request/poll, no restart
    app = client.application
    assert app.config["TEMPLATES_AUTO_RELOAD"] is True
    assert app.jinja_env.auto_reload is True


def test_sources_derive_from_hermes_home(monkeypatch):
    # both defaults hang off the hermes config dir, so a correctly set
    # HERMES_HOME is all that is needed to start with no arguments
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    assert app_module.hermes_config_dir() == "/srv/hermes/config"
    assert app_module.default_db_path() == "/srv/hermes/config/jmem0_logged.db"
    assert app_module.default_atof_path() == \
        "/srv/hermes/config/nemo-relay/atof/hermes-atof.jsonl"


def test_hermes_home_is_normalized(monkeypatch):
    # the agent conventionally exports <checkout>/hermes-agent/../config
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/hermes-agent/../config")
    assert app_module.hermes_config_dir() == "/srv/hermes/config"


def test_sources_fall_back_without_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert app_module.hermes_config_dir() == app_module.FALLBACK_CONFIG_DIR
    assert app_module.default_db_path().endswith("/config/jmem0_logged.db")


def test_env_vars_override_the_defaults(monkeypatch):
    monkeypatch.setattr(app_module.sys, "argv", ["app.py"])
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    monkeypatch.setenv("JMEM0_DB", "/tmp/other.db")
    monkeypatch.setenv("ATOF_LOG", "/tmp/other.jsonl")
    (db_path, db_from), (atof_path, atof_from) = app_module.resolve_sources()
    assert (db_path, db_from) == ("/tmp/other.db", "JMEM0_DB")
    assert (atof_path, atof_from) == ("/tmp/other.jsonl", "ATOF_LOG")


def test_first_argument_wins_for_the_database(monkeypatch):
    monkeypatch.setattr(app_module.sys, "argv", ["app.py", "/tmp/argv.db"])
    monkeypatch.setenv("JMEM0_DB", "/tmp/env.db")
    (db_path, db_from), _ = app_module.resolve_sources()
    assert (db_path, db_from) == ("/tmp/argv.db", "command line")


def test_banner_reports_paths_and_whether_they_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes/config")
    present = tmp_path / "there.db"
    present.write_text("")
    banner = app_module.startup_banner(
        (str(present), "default"), ("/nope/absent.jsonl", "ATOF_LOG"), 5090)
    assert f"{present}  [ok] (from default)" in banner
    assert "/nope/absent.jsonl  [MISSING] (from ATOF_LOG)" in banner
    assert "/srv/hermes/config" in banner
    assert "http://0.0.0.0:5090/" in banner


def test_banner_flags_an_unset_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    banner = app_module.startup_banner(("/a.db", "default"), ("/b.jsonl", "default"), 5090)
    assert "unset" in banner and "fallback" in banner


def test_status_endpoint_reports_traffic(client):
    client.get("/memory/")
    client.get("/timing/")
    body = client.get("/_status").get_data(as_text=True)
    assert "/memory/" in body and "/timing/" in body
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
    page = client.get("/memory/").get_data(as_text=True)
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
    for url in ("/memory/", "/timing/"):
        assert 'href="/_status"' in client.get(url).get_data(as_text=True)
