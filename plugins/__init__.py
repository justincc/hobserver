"""In-tree view plugins.

A plugin is any importable module exposing the tab contract (ADR 5) — the
ones here are simply the plugins that ship with the app. Which of them are
served, in what order, is decided by hobserver.toml, not by this package; there
is no registry to add a tab to.

The contract, in full:

    PLUGIN_API = 1                  # version the plugin was written against
    bp                              # Flask blueprint, registered under the prefix
    TAB_LABEL                       # what the tab reads
    URL_PREFIX                      # address, may be multi-segment

    def init_app(app, settings)     # optional: stash config, publish accessors
    def sources(settings)           # optional: what this tab reads, for the banner

A plugin imports nothing from the app: the contract is module attributes and
Flask, so an out-of-tree tab does not depend on a hobserver package or
track its version. The in-tree plugins do import `hermes_paths`, but only to
default their paths — nothing in the contract requires it.

`bp.name` is the code identifier (`url_for`, `templates/<name>/`) and does not
move; TAB_LABEL is UI copy and URL_PREFIX is the address, both free to change
without touching a single `url_for` call. Do not collapse them back together.

Every plugin is a read-only view over a data source produced by another
process (ADR 2): turns reads the NeMo Relay ATOF JSONL, mem0 reads
jmem0_logged.db.
"""
