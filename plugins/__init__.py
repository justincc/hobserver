"""View plugins for the log browser.

Each plugin is a module exposing a Flask blueprint named ``bp`` and a
``TAB_LABEL`` string. The app shell registers every entry in PLUGINS under
``/<bp.name>/`` and renders one tab per plugin in base.html.

Every plugin is a read-only view over a data source produced by another
process (see docs/adr/0002): timing reads the NeMo Relay ATOF JSONL stream,
memory reads jmem0_logged.db. Registration is a static tuple — discovery can
come later if plugins multiply.

Tuple order is tab order, left to right, and its first entry is where `/`
lands. Prompts leads because a turn is the unit of activity: it shows what
hermes was asked and everything it did about it, memory calls included,
while the mem0 log covers one tool. The memory stores hermes keeps for
itself, and any other external provider tried later, are expected to arrive
as their own plugins beside mem0 rather than inside it.
"""

from plugins import memory, timing

PLUGINS = (timing, memory)
