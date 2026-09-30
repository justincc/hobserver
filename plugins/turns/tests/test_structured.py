"""structured.py: a command's printed output split into text and blocks."""

import json
import time

import plugins.turns.structured as structured
from plugins.turns.structured import structure


def _shape(reading):
    """Each segment as ("text", text) or (format, root kind), in order."""
    return [("text", s["text"]) if "text" in s else (s["format"], s["tree"]["kind"])
            for s in reading["segments"]]


def test_one_json_value_is_read_as_json():
    reading = structure('  {"a": [1, "x", null, true], "b": {}}\n')
    assert reading["summary"] == "JSON"
    root = reading["segments"][0]["tree"]
    assert root["kind"] == "object"
    assert [c["key"] for c in root["children"]] == ["a", "b"]
    arr = root["children"][0]["node"]
    assert [(c["node"]["type"], c["node"]["value"]) for c in arr["children"]] == [
        ("number", "1"), ("string", '"x"'), ("null", "null"), ("bool", "true")]


def test_a_run_of_json_values_is_each_its_own_tree():
    pretty = json.dumps({"a": 1}, indent=2)
    reading = structure(f'{pretty}\n{{"b": 2}}\n[3]\n')
    assert reading["summary"] == "3 JSON values"
    assert _shape(reading) == [("JSON", "object"), ("JSON", "object"),
                               ("JSON", "array")]


def test_a_python_literal_is_read_in_python_spelling():
    reading = structure("{'ok': True, 'n': None, 'pair': (1, 2), 's': {3}}")
    assert reading["summary"] == "a Python literal"
    root = reading["segments"][0]["tree"]
    kids = {c["key"]: c["node"] for c in root["children"]}
    assert kids["ok"]["value"] == "True" and kids["ok"]["type"] == "bool"
    assert kids["n"]["value"] == "None"
    assert kids["pair"]["kind"] == "tuple" and kids["s"]["kind"] == "set"


def test_text_between_blocks_stays_text_in_order():
    out = ('{"reference": 1}\nsome page text\nmore text\n'
           '{"reference": 2}\ntrailing text')
    reading = structure(out)
    assert reading["summary"] == "text with 2 JSON values"
    assert _shape(reading) == [("JSON", "object"),
                               ("text", "some page text\nmore text"),
                               ("JSON", "object"), ("text", "trailing text")]


def test_a_label_before_a_value_on_its_line_is_text():
    reading = structure("dailyRevenue 2026-09-08 {'Base': {'V1': 74296}}\n"
                        "OLD {\"title\": \"x\", \"n\": 1}\n"
                        "[INFO] got {'a': 1, 'b': 2}")
    assert _shape(reading) == [
        ("text", "dailyRevenue 2026-09-08"), ("Python literal", "object"),
        ("text", "OLD"), ("JSON", "object"),
        ("text", "[INFO] got"), ("Python literal", "object")]
    assert reading["summary"] == \
        "text with 2 Python literals and a JSON value"


def test_output_holding_no_block_stays_text():
    assert structure("hello") is None
    assert structure('{"a": 1} and more') is None      # not to the line end
    assert structure("42") is None                     # a scalar is no tree
    assert structure("[]") is None                     # nor is an empty one
    assert structure("[INFO] starting") is None
    assert structure("see [1] above") is None
    assert structure('<r id="1"/>') is None            # XML is not read
    assert structure(None) is None


def test_a_value_cut_off_part_way_stays_text():
    """hermes caps what it captures; a value it cut short is left as text,
    and a whole value before it is still read."""
    reading = structure('{"a": 1}\n{"b": [1, 2,')
    assert _shape(reading) == [("JSON", "object"), ("text", '{"b": [1, 2,')]


def test_python_code_in_the_output_is_never_run():
    """literal_eval builds literals only: a call is a failed parse, text."""
    marker = []
    structured.__dict__["_probe"] = marker.append
    try:
        assert structure("[__import__('os').getcwd()]") is None
        assert structure("[_probe(1)]") is None
        assert structure("{'a': open('/etc/passwd')}") is None
    finally:
        del structured.__dict__["_probe"]
    assert marker == []


def test_nesting_past_the_limit_is_text_without_reaching_the_parser():
    deep = "[" * (structured.MAX_NEST + 1) + "'x'" + "]" * (structured.MAX_NEST + 1)
    assert structure(deep) is None
    shallow = "[" * 10 + "'x'" + "]" * 10
    assert structure(shallow)["summary"] == "a Python literal"


def test_a_structure_deeper_than_the_tree_is_cut_to_one_leaf():
    depth = structured.TREE_DEPTH + 5
    node = structure("[" * depth + "1" + "]" * depth)["segments"][0]["tree"]
    for _ in range(structured.TREE_DEPTH):
        node = node["children"][0]["node"]
    assert node["kind"] == "scalar" and node["type"] == "deep"


def test_an_output_over_the_size_cap_is_not_scanned():
    big = json.dumps(["x" * 100] * (structured.MAX_CHARS // 100))
    assert len(big) > structured.MAX_CHARS
    assert structure(big) is None


def test_overlapping_unclosed_candidates_stay_within_the_budget():
    """Every line opens a JSON array nested in the one before, never closed:
    each candidate is parsed a thousand lines deep before it fails. Unbounded
    that is seconds per output; the budget stops retrying once it is spent."""
    text = "\n".join("[1," for _ in range(60_000))[:structured.MAX_CHARS]
    started = time.monotonic()
    assert structure(text) is None
    assert time.monotonic() - started < 0.5


def test_a_citation_ending_a_line_of_prose_is_text():
    assert structure("fell ~2.4% on the news. [16]") is None
    assert structure("google paths ['/a']") is None
    # alone on its line it is still a value
    assert structure("[16]")["summary"] == "JSON"


def test_the_inner_lines_of_a_cut_off_document_are_not_picked_apart():
    cut = '{\n  "rows": [\n    {"bullet_fields": ["a", "b"],\n     "x": 1'
    assert structure(cut) is None
