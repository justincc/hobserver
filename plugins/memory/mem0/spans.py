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
    The Turns tab has its own defensive read for this (`spans._as_dict`);
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
    return [_hit(item) for item in raw if isinstance(item, dict)]


def _hit(item):
    """One search hit, `{id, memory, score}`, each None when absent or the
    wrong type."""
    memory = item.get("memory")
    score = item.get("score")
    return {
        "id": item.get("id") if isinstance(item.get("id"), str) else None,
        "memory": memory if isinstance(memory, str) else None,
        # ints are valid JSON numbers; bool is an int subclass
        "score": score if isinstance(score, (int, float))
                 and not isinstance(score, bool) else None,
    }


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


def _result_dict(text):
    """A result's JSON object as fed back to the model, or None. The whole
    text first, then the first object in it, for a result that arrives with
    framing around it."""
    try:
        data = json.loads(text.strip())
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def read_search_result(text):
    """mem0_search's result on the prompt page (ADR 26): the reading the
    Turns tab draws as the formatted tab, or None to leave the raw body.

    The tool returns `{"results": [{id, memory, score}], "count": n}`, `{"result":
    "No relevant memories found."}` when nothing matched, or `{"error":
    "..."}`. Each hit is a row: the memory as its text, its score and id as the
    row's facts; the count, or the no-match sentence, is the whole result's."""
    data = _result_dict(text)
    if data is None:
        return None
    error = data.get("error")
    if isinstance(error, str) and error:
        return {"error": error, "results": []}
    raw = data.get("results")
    if not isinstance(raw, list):
        said = data.get("result")
        if isinstance(said, str) and said:
            return {"fields": [{"key": "result", "value": said}],
                    "results": []}
        return None
    hits = [_hit(item) for item in raw if isinstance(item, dict)]
    count = data.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = len(hits)
    return {
        "fields": [{"key": "count", "value": str(count)}],
        "results": [{"description": h["memory"],
                     "fields": [{"key": k, "value": str(h[k])}
                                for k in ("score", "id") if h[k] is not None]}
                    for h in hits],
    }


# The table this plugin contributes, keyed by the name a spec uses as a
# source. `__init__` re-exports it as SPAN_READERS, which is what the shell
# picks up — beside SCOPES, and gone with it when this tab is disabled.
SPAN_READERS = {
    "mem0_results": mem0_results,
    "mem0_result_count": mem0_result_count,
}

# The prompt page's reading of this plugin's tool results, keyed by tool name.
# `__init__` re-exports it as RESULT_READERS, beside SPAN_READERS (ADR 26).
RESULT_READERS = {
    "mem0_search": read_search_result,
}
