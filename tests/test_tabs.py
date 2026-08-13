"""Tab configuration and loading (ADR 5).

The out-of-tree cases are exercised by writing a plugin into tmp_path and
putting it on sys.path — the same route an installed package takes, and the
point of loading by module name rather than from a registry.
"""

import sys
import textwrap

import pytest

import tabs as tabs_module
from app import create_app
from conftest import make_app

PLUGIN = '''\
from flask import Blueprint

PLUGIN_API = {api}
bp = Blueprint("{name}", __name__)
TAB_LABEL = "{label}"
URL_PREFIX = "{prefix}"

SEEN = {{}}

def init_app(app, settings):
    SEEN["settings"] = settings

def sources(settings):
    return [{{"label": "thing", "path": settings.get("path", "/nowhere"),
             "from": "settings", "required": {required},
             "problem": settings.get("problem")}}]

@bp.route("/")
def index():
    return "hello from {name}"
'''


def write_plugin(tmp_path, name, label="Extra", prefix=None, api=1,
                 required="False", body=None):
    """An out-of-tree plugin package, importable as `name`."""
    path = tmp_path / f"{name}.py"
    path.write_text(body if body is not None else PLUGIN.format(
        name=name, label=label, prefix=prefix or name, api=api,
        required=required))
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    sys.modules.pop(name, None)
    return name


def load(entries):
    return tabs_module.load_tabs(tabs_module.parse_config({"tabs": entries}))


def test_a_plugin_from_outside_the_tree_loads_and_serves(tmp_path, memory_db):
    # the whole point of module paths: nothing in this repo mentions it
    name = write_plugin(tmp_path, "outside_tab", label="Outside")
    client = make_app(entries=[
        {"module": name},
        {"module": "plugins.mem0", "settings": {"db": memory_db}},
    ]).test_client()
    assert client.get("/outside_tab/").get_data(as_text=True) == \
        "hello from outside_tab"
    # and it takes its place in the bar, ahead of the in-tree tab
    bar = client.get("/memory/mem0/").get_data(as_text=True)
    assert bar.index("Outside") < bar.index("Mem0")


def test_settings_reach_the_plugin_untouched(tmp_path):
    name = write_plugin(tmp_path, "settings_tab")
    tabs = load([{"module": name, "settings": {"path": "/x", "n": 3}}])
    create_app(tabs)
    assert sys.modules[name].SEEN["settings"] == {"path": "/x", "n": 3}


def test_settings_expand_user_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_DIR", "/srv/data")
    specs = tabs_module.parse_config({"tabs": [
        {"module": "plugins.turns", "settings": {"atof_log": "$SOME_DIR/a.jsonl"}}]})
    assert specs[0].settings["atof_log"] == "/srv/data/a.jsonl"


def test_disabled_tab_is_not_loaded(tmp_path):
    name = write_plugin(tmp_path, "off_tab")
    assert load([{"module": name, "enabled": False}]) == []


def test_config_order_is_tab_order(memory_db):
    app = make_app(entries=[
        {"module": "plugins.mem0", "settings": {"db": memory_db}},
        {"module": "plugins.turns", "settings": {"atof_log": "/nope.jsonl"}},
    ])
    page = app.test_client().get("/memory/mem0/").get_data(as_text=True)
    assert page.index("Mem0") < page.index("Turns")
    # and the first tab is what / lands on
    assert app.test_client().get("/").headers["Location"].endswith("/memory/mem0/")


def test_a_broken_plugin_does_not_stop_the_others(tmp_path, memory_db):
    write_plugin(tmp_path, "exploding_tab",
                 body="raise RuntimeError('boom')\n")
    app = make_app(entries=[
        {"module": "exploding_tab"},
        {"module": "plugins.mem0", "settings": {"db": memory_db}},
    ])
    client = app.test_client()
    assert client.get("/memory/mem0/").status_code == 200
    page = client.get("/memory/mem0/").get_data(as_text=True)
    assert "exploding_tab" in page          # still in the bar, marked
    assert "⚠" in page


def test_a_broken_plugin_serves_its_reason(tmp_path):
    write_plugin(tmp_path, "broken_tab", body="raise RuntimeError('boom')\n")
    app = make_app(entries=[{"module": "broken_tab"}])
    resp = app.test_client().get("/unavailable/broken_tab/")
    assert resp.status_code == 503
    page = resp.get_data(as_text=True)
    assert "boom" in page and "RuntimeError" in page
    assert "broken_tab" in page


def test_a_module_that_is_not_a_plugin_is_reported(tmp_path):
    write_plugin(tmp_path, "not_a_tab", body="ANSWER = 42\n")
    tab = load([{"module": "not_a_tab"}])[0]
    assert tab.bp is None
    assert "not a tab plugin" in tab.problem
    assert "bp" in tab.problem


def test_a_plugin_for_another_api_version_is_refused(tmp_path):
    name = write_plugin(tmp_path, "future_tab", api=99)
    tab = load([{"module": name}])[0]
    assert tab.bp is None
    assert "99" in tab.problem and str(tabs_module.PLUGIN_API) in tab.problem


def test_an_unusable_required_source_takes_the_tab_out_of_service(tmp_path):
    name = write_plugin(tmp_path, "needy_tab", required="True")
    tab = load([{"module": name, "settings": {"problem": "no such file"}}])[0]
    assert tab.bp is None
    assert tab.problem == "thing: no such file"


def test_an_optional_source_problem_leaves_the_tab_serving(tmp_path):
    # the Turns tab's case: a missing log is the tab's own story to tell
    name = write_plugin(tmp_path, "relaxed_tab", required="False")
    tab = load([{"module": name, "settings": {"problem": "no such file"}}])[0]
    assert tab.bp is not None
    assert tab.problem is None


def test_the_mem0_tab_survives_an_unusable_database(tmp_path):
    # was a process exit before ADR 5; now one tab's problem, not the app's
    app = make_app(entries=[
        {"module": "plugins.turns", "settings": {"atof_log": "/nope.jsonl"}},
        {"module": "plugins.mem0", "settings": {"db": str(tmp_path)}},
    ])
    client = app.test_client()
    assert client.get("/turns/").status_code == 200
    unavailable = client.get("/memory/mem0/")
    assert unavailable.status_code == 503
    assert "not a regular file" in unavailable.get_data(as_text=True)


def test_two_tabs_at_one_url_prefix_is_fatal(tmp_path):
    write_plugin(tmp_path, "first_tab", prefix="same")
    write_plugin(tmp_path, "second_tab", prefix="same")
    with pytest.raises(tabs_module.ConfigError) as exc:
        load([{"module": "first_tab"}, {"module": "second_tab"}])
    assert "same" in str(exc.value)
    assert "first_tab" in str(exc.value) and "second_tab" in str(exc.value)


def test_two_tabs_with_one_blueprint_name_is_fatal(tmp_path):
    # different addresses, same blueprint name: url_for would be ambiguous
    (tmp_path / "clash_a.py").write_text(
        PLUGIN.format(name="shared", label="A", prefix="a", api=1,
                      required="False"))
    (tmp_path / "clash_b.py").write_text(
        PLUGIN.format(name="shared", label="B", prefix="b", api=1,
                      required="False"))
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    for name in ("clash_a", "clash_b"):
        sys.modules.pop(name, None)
    with pytest.raises(tabs_module.ConfigError) as exc:
        load([{"module": "clash_a"}, {"module": "clash_b"}])
    assert "blueprint name" in str(exc.value)


def test_host_is_read_from_the_top_level_key(tmp_path):
    path = tmp_path / "hobserver.toml"
    path.write_text(textwrap.dedent("""
        host = "0.0.0.0"

        [[tabs]]
        module = "plugins.turns"
    """))
    _, _, host = tabs_module.read_config(str(path))
    assert host == "0.0.0.0"


def test_host_defaults_to_none_when_unset():
    # None, not a bind address: the serving default lives with the server, so
    # the config layer only reports what the file said, which here is nothing
    assert tabs_module.parse_host({"tabs": []}) is None


def test_a_non_string_host_is_fatal():
    with pytest.raises(tabs_module.ConfigError) as exc:
        tabs_module.parse_host({"host": 5090})
    assert "host must be a string" in str(exc.value)


def test_config_errors_name_the_entry(tmp_path):
    with pytest.raises(tabs_module.ConfigError) as exc:
        tabs_module.parse_config({"tabs": [{"module": "a"}, {"label": "b"}]})
    assert "entry 2" in str(exc.value)

    with pytest.raises(tabs_module.ConfigError) as exc:
        tabs_module.parse_config({})
    assert "no [[tabs]]" in str(exc.value)


def test_a_missing_config_file_falls_back_to_the_built_in_tabs(tmp_path):
    specs, origin, host = tabs_module.read_config(str(tmp_path / "absent.toml"))
    assert origin == "built-in defaults"
    assert [s.module for s in specs] == ["plugins.turns", "plugins.mem0"]
    assert host is None                      # no file, so no host — app defaults it


def test_a_config_file_is_read_in_order(tmp_path):
    path = tmp_path / "hobserver.toml"
    path.write_text(textwrap.dedent("""
        [[tabs]]
        module = "plugins.mem0"
        settings = { db = "/tmp/x.db" }

        [[tabs]]
        module = "plugins.turns"
        enabled = false
    """))
    specs, origin, host = tabs_module.read_config(str(path))
    assert origin == str(path)
    assert [(s.module, s.enabled) for s in specs] == [
        ("plugins.mem0", True), ("plugins.turns", False)]
    assert specs[0].settings == {"db": "/tmp/x.db"}
    assert host is None                      # this file sets no host


def test_malformed_toml_is_fatal(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[[tabs]\nmodule =\n")
    with pytest.raises(tabs_module.ConfigError) as exc:
        tabs_module.read_config(str(path))
    assert str(path) in str(exc.value)


def test_the_example_config_matches_the_built_in_default():
    # hobserver.example.toml is the template a user copies; the built-in list
    # is what runs when they have not. They must show the same tabs, or the
    # example teaches a layout the zero-config default does not deliver.
    specs, _, _ = tabs_module.read_config("hobserver.example.toml")
    assert [s.module for s in specs] == \
        [entry["module"] for entry in tabs_module.BUILTIN_TABS]
