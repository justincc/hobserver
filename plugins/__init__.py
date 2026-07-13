"""View plugins for the log browser.

Each plugin is a module exposing a Flask blueprint named ``bp`` and a
``TAB_LABEL`` string. The app shell registers every entry in PLUGINS under
``/<bp.name>/`` and renders one tab per plugin in base.html.

Every plugin is a read-only view over a data source produced by another
process (see docs/adr/0002): memory reads jmem0_logged.db, timing reads the
NeMo Relay ATOF JSONL stream. Registration is a static tuple — discovery can
come later if plugins multiply.
"""

from plugins import memory, timing

PLUGINS = (memory, timing)
