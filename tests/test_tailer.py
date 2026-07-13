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
