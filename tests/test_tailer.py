"""Tailer tests — byte-offset cursor, partial lines, truncation, missing file."""

import json

from plugins.timing.tailer import AtofTailer


def event_line(uuid, us):
    return json.dumps({
        "kind": "mark", "uuid": uuid, "name": "m", "timestamp": us,
    })


def test_reads_appended_lines_incrementally(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(event_line("a", 1) + "\n" + event_line("b", 2) + "\n")
    tailer = AtofTailer(str(path))
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["a", "b"]
    with open(path, "a") as handle:
        handle.write(event_line("c", 3) + "\n")
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["a", "b", "c"]


def test_partial_trailing_line_waits_for_newline(tmp_path):
    path = tmp_path / "events.jsonl"
    complete = event_line("a", 1) + "\n"
    partial = event_line("b", 2)
    path.write_text(complete + partial[:20])     # exporter mid-write
    tailer = AtofTailer(str(path))
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["a"]
    assert tailer.errors == []                   # the fragment is not "malformed"
    path.write_text(complete + partial + "\n")
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["a", "b"]


def test_truncated_file_resets_and_rereads(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(event_line("a", 1) + "\n" + event_line("b", 2) + "\n")
    tailer = AtofTailer(str(path))
    tailer.refresh()
    assert len(tailer.events) == 2
    path.write_text(event_line("z", 9) + "\n")   # overwrite mode / rotation
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["z"]


def test_missing_file_is_empty_then_reads_once_created(tmp_path):
    path = tmp_path / "events.jsonl"
    tailer = AtofTailer(str(path))
    tailer.refresh()
    assert tailer.events == []
    path.write_text(event_line("a", 1) + "\n")
    tailer.refresh()
    assert [e.uuid for e in tailer.events] == ["a"]


def test_parse_errors_accumulate_with_line_numbers(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(event_line("a", 1) + "\nnot json\n")
    tailer = AtofTailer(str(path))
    tailer.refresh()
    with open(path, "a") as handle:
        handle.write("also not json\n")
    tailer.refresh()
    assert [e.line_no for e in tailer.errors] == [2, 3]
    assert [e.uuid for e in tailer.events] == ["a"]


def test_unicode_line_separators_inside_a_record_do_not_split_it(tmp_path):
    """JSON leaves U+0085/U+2028/U+2029 unescaped and hermes' assistant text
    contains them verbatim; str.splitlines() would break such a record into
    fragments that then fail to parse, silently losing marks — a lost
    hermes.turn.end leaves its turn open forever."""
    path = tmp_path / "events.jsonl"
    payload = json.dumps({
        "kind": "mark", "uuid": "a", "name": "hermes.turn.end",
        "timestamp": 1, "data": {"text": "one\u0085two\u2028three\u2029four"},
    }, ensure_ascii=False)
    assert len(payload.splitlines()) == 4        # the trap this guards
    path.write_text(payload + "\n" + event_line("b", 2) + "\n",
                    encoding="utf-8")
    tailer = AtofTailer(str(path))
    tailer.refresh()
    assert tailer.errors == []
    assert [e.uuid for e in tailer.events] == ["a", "b"]
    assert [e.line_no for e in tailer.events] == [1, 2]
