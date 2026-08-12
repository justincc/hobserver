"""Where hermes-agent keeps the files this app reads.

Used by the in-tree plugins to default their own source paths, and by the
startup banner to show which config directory those defaults came from. A
plugin is not obliged to use any of it — an out-of-tree tab reads whatever it
likes from its own settings.
"""

import os

# Where hermes-agent keeps its config when nobody has said otherwise. This
# must match what the agent itself falls back to with HERMES_HOME unset —
# hermes_constants._get_platform_default_hermes_home() — or an installation
# that configured nothing gets hobserve looking in a directory hermes
# never writes to, which is exactly the installation the fallback is for.
FALLBACK_CONFIG_DIR = os.path.expanduser("~/.hermes")


def hermes_config_dir():
    """The hermes-agent config directory: $HERMES_HOME, else the fallback.

    HERMES_HOME is conventionally set to <checkout>/hermes-agent/../config, so
    it is normalized rather than used raw.
    """
    home = os.environ.get("HERMES_HOME")
    return os.path.normpath(home) if home else FALLBACK_CONFIG_DIR
