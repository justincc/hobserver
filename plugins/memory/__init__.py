"""The memory-plugin family.

A namespace, not a plugin: each tab under it (`plugins.memory.mem0`, and any
memory system added later) is an independent view with the full tab contract,
grouped here only so one family of tools shares a prefix. Nothing loads because
it lives here — which tabs are served is still hobserver.toml's to say.
"""
