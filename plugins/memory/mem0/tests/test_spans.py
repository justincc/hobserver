"""mem0's span readers — how this plugin reads its own payloads (ADR 17).

They are mem0's knowledge, so they live and are tested with this plugin's
code.

A reader is `fn(span) -> value`, so these test against a stub carrying the
attributes a reader reads. That is the contract, and holding to it here is
what proves a reader needs nothing from the `Span` class — which is the
whole point of the extension point: someone with their own tool and no
foothold in this tree can write one.
"""

from types import SimpleNamespace

from plugins.memory import mem0
from plugins.memory.mem0.spans import (RESULT_READERS, SPAN_READERS,
                                       mem0_result_count, mem0_results,
                                       read_search_result)

RANKED = ('{"count": 3, "results":'
          ' [{"id": "b760576d", "memory": "top fact", "score": 0.8042},'
          '  {"id": "f9c1f7ee", "memory": "next fact", "score": 0.5339},'
          '  {"id": "fb54073a", "memory": "third fact", "score": 0.4229}]}')


def span(name="mem0_search", end_data=None):
    return SimpleNamespace(name=name, end_data=end_data)


def test_results_come_back_ranked_from_the_end_payload():
    # the log carries the tool result as a JSON *string*, ranked by score
    found = mem0_results(span(end_data=RANKED))
    assert [r["id"] for r in found] == ["b760576d", "f9c1f7ee", "fb54073a"]
    assert found[0]["memory"] == "top fact"
    assert found[0]["score"] == 0.8042
    assert mem0_result_count(span(end_data=RANKED)) == 3


def test_results_are_read_only_on_mem0_search_scopes():
    # "results" is far too generic a key to trust on another scope
    other = span(name="session_search",
                 end_data='{"count": 1, "results": [{"memory": "not mem0"}]}')
    assert mem0_results(other) == []
    assert mem0_result_count(other) is None


def test_results_are_read_defensively():
    # payloads are opaque per the ATOF spec: a missing key drops to None and
    # a non-dict entry is skipped, rather than 500ing the turn page
    odd = span(end_data='{"results": [{"memory": "no id, no score"},'
                        ' "not a dict", {"id": "abc"}]}')
    assert mem0_results(odd) == [
        {"id": None, "memory": "no id, no score", "score": None},
        {"id": "abc", "memory": None, "score": None},
    ]
    # no count in the payload, so it falls back to the list length
    assert mem0_result_count(odd) == 2


def test_a_payload_that_is_already_a_dict_reads_the_same():
    # the shape depends on which hermes wrote it, so both are handled
    assert mem0_results(span(end_data={"results": [{"id": "x"}]}))[0]["id"] == "x"
    assert mem0_results(span(end_data="not json at all")) == []
    assert mem0_results(span(end_data=["results"])) == []


def test_a_search_still_open_has_no_results():
    assert mem0_results(span(end_data=None)) == []
    assert mem0_result_count(span(end_data=None)) is None


def test_the_tab_exposes_its_readers_beside_its_scopes():
    """Both halves ride the same contribution, so disabling this tab takes
    the readings with the rows they feed."""
    assert mem0.SPAN_READERS is SPAN_READERS
    assert set(SPAN_READERS) == {"mem0_results", "mem0_result_count"}
    assert all(callable(r) for r in SPAN_READERS.values())


# --- the prompt page's reading of a mem0_search result (ADR 26) -------------

def test_a_search_result_reads_as_one_row_per_memory():
    reading = read_search_result(RANKED)
    assert reading["fields"] == [{"key": "count", "value": "3"}]
    first = reading["results"][0]
    assert first["description"] == "top fact"
    assert first["fields"] == [{"key": "score", "value": "0.8042"},
                               {"key": "id", "value": "b760576d"}]
    assert [r["description"] for r in reading["results"]] == [
        "top fact", "next fact", "third fact"]


def test_a_search_that_matched_nothing_says_so():
    reading = read_search_result('{"result": "No relevant memories found."}')
    assert reading == {"fields": [{"key": "result",
                                   "value": "No relevant memories found."}],
                       "results": []}


def test_a_failed_search_reads_as_its_error():
    reading = read_search_result('{"error": "Search failed: timeout"}')
    assert reading["error"] == "Search failed: timeout"


def test_a_result_that_is_not_the_search_shape_stays_raw():
    assert read_search_result("not json") is None
    assert read_search_result('{"other": 1}') is None
    assert read_search_result("[1, 2]") is None
    # a hit's missing fields simply do not draw
    reading = read_search_result('{"results": [{"memory": "m"}]}')
    assert reading["results"] == [{"description": "m", "fields": []}]
    assert reading["fields"] == [{"key": "count", "value": "1"}]


def test_the_tab_exposes_its_result_readers():
    assert mem0.RESULT_READERS is RESULT_READERS
    assert RESULT_READERS == {"mem0_search": read_search_result}
