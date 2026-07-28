"""Where hermes-agent keeps the files this app reads.

Used by the in-tree plugins to default their own source paths, and by the
startup banner to show which config directory those defaults came from. A
plugin is not obliged to use any of it — an out-of-tree tab reads whatever it
likes from its own settings.
"""

import os

# The fallback for a shell that never exported HERMES_HOME. Keeping the
# machine-specific absolute path in one place keeps it out of the plugins.
FALLBACK_CONFIG_DIR = os.path.expanduser(
    "~/jc/knowledge/data/processing/analysis/reasoning/artificial/agents/"
    "autonomous/resident/hermes-agent/product/src/config"
)


def hermes_config_dir():
    """The hermes-agent config directory: $HERMES_HOME, else the fallback.

    HERMES_HOME is conventionally set to <checkout>/hermes-agent/../config, so
    it is normalized rather than used raw.
    """
    home = os.environ.get("HERMES_HOME")
    return os.path.normpath(home) if home else FALLBACK_CONFIG_DIR


def config_dir_origin():
    """How `hermes_config_dir` decided, for the banner."""
    return "HERMES_HOME" if os.environ.get("HERMES_HOME") else "built-in fallback"
