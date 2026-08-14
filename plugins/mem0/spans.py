"""How mem0's span payloads are read (ADR 17).

`scopes.py` says how mem0's spans *show*; this says how they are *read*. The
two are the halves of one contribution and travel together: the Turns tab
paints the rows, but nothing there knows mem0's payload shape, and nothing
there has to.

A reader is `fn(span) -> value`, named from a spec the way a `Span` property
is (`Each("mem0_results", …)`). It gets the whole span, so it can read either
payload, the metadata or the timings — the underlying values, not this app's
curated accessors, which is what design principle 1 requires of an extension
point.

**Payloads are opaque per the ATOF spec.** These shapes have been uniform in
the log so far and are still type-guarded at every step: a reader that raises
costs its own value, but one that trusts a shape produces a wrong one, which
is worse. Checked against the tool definitions in
`$HERMES_SOURCE/plugins/memory/mem0/__init__.py` — the four mem0 tools live in
hermes' memory *plugin*, not in `$HERMES_SOURCE/tools/` with the rest.
"""

import json


def _end_dict(span):
    """A span's end payload as a dict, or None.

    The nemo_relay plugin emits hermes tool results as raw JSON strings, so
    the payload arrives as text about as often as it arrives as an object.
    The Turns tab has its own defensive read for this (`assembler._as_dict`);
    this is a plugin, and a plugin importing another tab's private helper
    would be exactly the coupling ADR 4 rules out — so mem0 keeps its own.
    """
    data = getattr(span, "end_data", None)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def mem0_results(span):
    """What a mem0_search actually retrieved.

    `{"count": n, "results": [{"id", "memory", "score"}, …]}`, ranked by score
    descending, so the first entries are the top hits. Rendering a tool's
    *output* at all is the exception rather than the rule — the query alone
    never says whether the search was any good, which is what earns it here.
    """
    if getattr(span, "name", None) != "mem0_search":
        return []
    end = _end_dict(span)
    if end is None:
        return []
    raw = end.get("results")
    if not isinstance(raw, list):
        return []
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        memory = item.get("memory")
        score = item.get("score")
        results.append({
            "id": item.get("id") if isinstance(item.get("id"), str) else None,
            "memory": memory if isinstance(memory, str) else None,
            # ints are valid JSON numbers; bool is an int subclass
            "score": score if isinstance(score, (int, float))
                     and not isinstance(score, bool) else None,
        })
    return results


def mem0_result_count(span):
    """How many memories came back. The payload's own count is authoritative
    (it is what mem0 reported); fall back to the list length when absent."""
    if getattr(span, "name", None) != "mem0_search":
        return None
    end = _end_dict(span)
    if end is not None:
        count = end.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    results = mem0_results(span)
    return len(results) if results else None


# The table this plugin contributes, keyed by the name a spec uses as a
# source. `__init__` re-exports it as SPAN_READERS, which is what the shell
# picks up — beside SCOPES, and gone with it when this tab is disabled.
SPAN_READERS = {
    "mem0_results": mem0_results,
    "mem0_result_count": mem0_result_count,
}
