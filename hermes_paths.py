"""Where hermes-agent keeps the files this app reads.

Used by the in-tree plugins to default their own source paths, and by the
startup banner to show which config directory those defaults came from. A
plugin is not obliged to use any of it — an out-of-tree tab reads whatever it
likes from its own settings.
"""

import os

# Where hermes-agent keeps its config when nobody has said otherwise: the
# conventional location, a dotdir under the home directory. A default has to
# be the one that suits an installation nobody has configured, not the one
# that suits whoever wrote the line — anyone whose hermes lives elsewhere
# exports HERMES_HOME and never meets it.
FALLBACK_CONFIG_DIR = os.path.expanduser("~/.hermes/config")


def hermes_config_dir():
    """The hermes-agent config directory: $HERMES_HOME, else the fallback.

    HERMES_HOME is conventionally set to <checkout>/hermes-agent/../config, so
    it is normalized rather than used raw.
    """
    home = os.environ.get("HERMES_HOME")
    return os.path.normpath(home) if home else FALLBACK_CONFIG_DIR


def config_dir_origin():
    """How `hermes_config_dir` decided, for the banner.

    Names the fallback rather than calling it one: a tab reporting a missing
    file "from default (~/.hermes/config)" has told the reader where to look
    and what to set, where "from default (built-in fallback)" only says the
    path came from somewhere they did not choose.
    """
    return "HERMES_HOME" if os.environ.get("HERMES_HOME") else "~/.hermes/config"
