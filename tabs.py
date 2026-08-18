"""Reading the tab configuration and loading the plugins it names.

Per ADR 5: tabs are declared in a TOML file, in tab order, and loaded by
module path — `plugins.turns` from this tree and `hobserver_zep` from
site-packages arrive the same way, so a tab needs no fork to be added.

    [[plugins]]
    plugin = "plugins.turns"
    settings = { atof_log = "…" }

    [[plugins]]
    plugin = "plugins.memory.mem0"
    enabled = false

Everything here is deliberately tolerant except one case. A tab that will not
import, is missing part of the contract, or cannot reach a source it must have
is reported and skipped — one broken tab, especially somebody else's, must not
stop the others from serving. Two tabs claiming the same URL prefix or
blueprint name is fatal, because either could then be the one serving a
request and no output would say which.
"""

from __future__ import annotations

import importlib
import os
import tomllib
from dataclasses import dataclass, field

# The plugin contract version this shell implements. A plugin declares the
# version it was written against as PLUGIN_API; a mismatch is refused rather
# than half-loaded.
PLUGIN_API = 1

CONFIG_ENV = "HOBSERVER_CONFIG"
DEFAULT_CONFIG_FILE = "hobserver.toml"
EXAMPLE_CONFIG_FILE = "hobserver.example.toml"

# The `origin` read_config reports when no file was found and BUILTIN_TABS is
# serving. A named constant, not a bare string, because the banner keys its
# "you are running the defaults" hint off it — the two must agree.
BUILTIN_ORIGIN = "built-in defaults"

# Used when no config file is found at all, so a fresh checkout runs with no
# setup. Just the Turns tab — the source every install has. Mem0 reads a log
# most users do not produce, so it is opt-in: named in hobserver.example.toml
# for a user to uncomment, not served by default. Keep this in step with the
# active entries in that template.
BUILTIN_TABS = ({"plugin": "plugins.turns"},)

# What a module must expose to be a tab: `bp` the Flask blueprint (its name
# keys the template directory and every url_for), `TAB_LABEL` the text in the
# tab bar, `URL_PREFIX` the address it is mounted at. All three are checked
# before anything is registered, so a module that is not a tab is reported
# rather than half-loaded. `init_app` and `sources` are optional and looked up
# separately. docs/extending/writing-a-plugin.md is the contract in full.
REQUIRED_ATTRS = ("bp", "TAB_LABEL", "URL_PREFIX")

# Optional, and carried untouched: a tab may describe how its own spans show
# on a tab that paints spans (ADR 10), and how their payloads are read
# (`SPAN_READERS`, ADR 17). The shell never looks inside these — it does not
# know what a scope spec is, the same way it does not know what a setting
# means — it only hands them to the tabs that want them, which is what ties
# their lifetime to this tab being enabled.
SPEC_ATTRS = ("SCOPES", "SCOPES_BY_CATEGORY", "SPAN_READERS")


class ConfigError(Exception):
    """The configuration cannot be served as written. Fatal by design."""


@dataclass(frozen=True)
class TabSpec:
    """One `[[plugins]]` entry, before the plugin it names is imported."""

    plugin: str
    enabled: bool = True
    settings: dict = field(default_factory=dict)


@dataclass
class Tab:
    """A configured tab, loaded or not.

    `bp` is None when the tab could not be loaded; `problem` then says why and
    the shell serves an explanation in its place.
    """

    module_name: str
    settings: dict = field(default_factory=dict)
    module: object | None = None
    label: str = ""
    url_prefix: str = ""
    bp: object | None = None
    problem: str | None = None
    sources: tuple = ()
    # {attribute name: table} from SPEC_ATTRS — opaque here, meaningful only
    # to a tab that paints spans. Empty for a tab that contributes none.
    scopes: dict = field(default_factory=dict)
    # CSS the plugin ships in its `STYLES` string, injected once into every
    # page's <head> by the shell. It lets a plugin carry the styling for what
    # it renders — including its spans on another tab — instead of the shell
    # holding every plugin's classes. Empty for a plugin that ships none.
    styles: str = ""

    @property
    def name(self):
        """Blueprint name when loaded, else something stable to key on."""
        if self.bp is not None:
            return self.bp.name
        return self.module_name.replace(".", "_")


def parse_config(data):
    """`[[plugins]]` entries → TabSpecs. Raises ConfigError on anything unusable.

    Errors name the entry by position, since a config file has no other handle
    on an entry that is missing its plugin.
    """
    entries = data.get("plugins")
    if entries is None:
        raise ConfigError("no [[plugins]] entries — nothing to serve")
    if not isinstance(entries, list):
        raise ConfigError("plugins must be a list of [[plugins]] entries")

    specs = []
    for i, entry in enumerate(entries, start=1):
        where = f"[[plugins]] entry {i}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} is not a table")
        plugin = entry.get("plugin")
        if not plugin or not isinstance(plugin, str):
            raise ConfigError(f"{where} has no plugin")
        settings = entry.get("settings", {})
        if not isinstance(settings, dict):
            raise ConfigError(f"{where} ({plugin}): settings must be a table")
        specs.append(TabSpec(plugin=plugin,
                             enabled=bool(entry.get("enabled", True)),
                             settings=_expand(settings)))
    return specs


def parse_host(data):
    """The bind address from the top-level `host` key, or None if unset.

    None means "use the serving default" — what that default is (loopback) is
    the server's decision, not the config file's, so it is applied in `app`
    next to the port rather than here.
    """
    host = data.get("host")
    if host is not None and not isinstance(host, str):
        raise ConfigError("host must be a string")
    return host


def _expand(settings):
    """Expand ~ and $VARS in string settings, so paths can be written once.

    Applied by the shell rather than per plugin: every tab wants it, and a
    plugin that did its own would be the one place it worked differently.
    """
    out = {}
    for key, value in settings.items():
        if isinstance(value, str):
            out[key] = os.path.expanduser(os.path.expandvars(value))
        elif isinstance(value, dict):
            out[key] = _expand(value)
        else:
            out[key] = value
    return out


def config_path(argv_path=None):
    """Where to read the config from: argument, env, then the default file."""
    if argv_path:
        return argv_path
    return os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE


def read_config(path):
    """(specs, origin, host) from a TOML file, falling back to the built-in
    tabs. `host` is None when the file does not set one (or is absent).

    A missing file is not an error: the app should run from a fresh checkout
    with no setup. A malformed one is, since it says what was meant and cannot
    be honoured.
    """
    if not os.path.exists(path):
        return ([TabSpec(**entry) for entry in BUILTIN_TABS],
                BUILTIN_ORIGIN, None)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_config(data), path, parse_host(data)


def load_tabs(specs):
    """Import each enabled tab and check what it needs. Never raises for one
    bad tab; raises ConfigError only when two tabs would collide."""
    tabs = [_load(spec) for spec in specs if spec.enabled]
    _check_collisions(tabs)
    return tabs


def _load(spec):
    tab = Tab(module_name=spec.plugin, settings=spec.settings,
              label=spec.plugin.rsplit(".", 1)[-1])
    try:
        module = importlib.import_module(spec.plugin)
    except Exception as exc:                      # any import-time failure
        tab.problem = f"cannot be imported ({exc.__class__.__name__}: {exc})"
        return tab

    missing = [a for a in REQUIRED_ATTRS if not getattr(module, a, None)]
    if missing:
        tab.problem = f"not a tab plugin: no {', '.join(missing)}"
        return tab

    api = getattr(module, "PLUGIN_API", None)
    if api != PLUGIN_API:
        tab.problem = (f"needs plugin API {api!r}, this app implements "
                       f"{PLUGIN_API}")
        return tab

    tab.module = module
    tab.label = module.TAB_LABEL
    tab.url_prefix = module.URL_PREFIX.strip("/")
    tab.sources = _sources(module, spec.settings)

    blocking = [s for s in tab.sources if s.get("required") and s.get("problem")]
    if blocking:
        first = blocking[0]
        tab.problem = f"{first.get('label', 'source')}: {first['problem']}"
        return tab

    tab.scopes = {a: getattr(module, a) for a in SPEC_ATTRS
                  if isinstance(getattr(module, a, None), dict)}
    # Optional `STYLES` — CSS the plugin contributes. A non-string is ignored
    # rather than fatal, the same tolerance the rest of the contract shows.
    styles = getattr(module, "STYLES", "")
    tab.styles = styles if isinstance(styles, str) else ""
    tab.bp = module.bp
    return tab


def _sources(module, settings):
    """What the tab reads, as reported by its optional `sources` hook.

    Plain dicts rather than a class, so a plugin needs no import from this
    app: {label, path, from, required, problem}.
    """
    hook = getattr(module, "sources", None)
    if hook is None:
        return ()
    try:
        return tuple(hook(settings))
    except Exception as exc:                      # a hook must not be fatal
        return ({"label": "sources", "path": "", "required": True,
                 "problem": f"could not be determined ({exc})"},)


def _check_collisions(tabs):
    """Two tabs at one address is the one configuration error worth exiting
    for: both would be reachable, and nothing in the output would say which
    one answered."""
    for attr, what in (("url_prefix", "URL prefix"), ("name", "blueprint name")):
        seen = {}
        for tab in tabs:
            if tab.bp is None:
                continue                          # not serving anything
            key = getattr(tab, attr)
            if key in seen:
                raise ConfigError(
                    f"{what} {key!r} claimed by both {seen[key]} and "
                    f"{tab.module_name}")
            seen[key] = tab.module_name
