"""Line reader tests — offsets, partial lines, missing file, re-reads."""

import json

import pytest

from plugins.prompts.tailer import (file_size, read_at, read_bytes_at,
                                    read_lines)


def event_line(uuid, us):
    return json.dumps({
        "kind": "mark", "uuid": uuid, "name": "m", "timestamp": us,
    })


def test_yields_offset_and_length_per_line(tmp_path):
    path = tmp_path / "events.jsonl"
    first, second = event_line("a", 1), event_line("b", 2)
    path.write_text(first + "\n" + second + "\n")

    lines = list(read_lines(str(path)))
    assert [text for _, _, text in lines] == [first, second]
    assert [offset for offset, _, _ in lines] == [0, len(first) + 1]
    assert [length for _, length, _ in lines] == [len(first) + 1,
                                                  len(second) + 1]


def test_offsets_round_trip_through_read_at(tmp_path):
    """The whole of ADR 11 rests on this: what the index records is enough
    to get the line back without reading the file."""
    path = tmp_path / "events.jsonl"
    path.write_text(event_line("a", 1) + "\n" + event_line("b", 2) + "\n")
    for offset, length, text in read_lines(str(path)):
        assert read_at(str(path), offset, length) == text


def test_reads_only_what_was_appended(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(event_line("a", 1) + "\n")
    consumed = sum(length for _, length, _ in read_lines(str(path)))
    with open(path, "a") as handle:
        handle.write(event_line("b", 2) + "\n")
    assert [json.loads(t)["uuid"]
            for _, _, t in read_lines(str(path), consumed)] == ["b"]


def test_partial_trailing_line_waits_for_its_newline(tmp_path):
    path = tmp_path / "events.jsonl"
    complete = event_line("a", 1) + "\n"
    partial = event_line("b", 2)
    path.write_text(complete + partial[:20])       # exporter mid-write
    assert [t for _, _, t in read_lines(str(path))] == [complete[:-1]]
    path.write_text(complete + partial + "\n")
    assert [json.loads(t)["uuid"]
            for _, _, t in read_lines(str(path))] == ["a", "b"]


def test_a_line_longer_than_one_read_chunk_is_still_one_line(tmp_path):
    """Turn marks average 570 KB and llm requests 129 KB, so records
    routinely straddle a read boundary."""
    path = tmp_path / "events.jsonl"
    big = json.dumps({"kind": "mark", "uuid": "a", "name": "m",
                      "timestamp": 1, "data": {"text": "x" * 5000}})
    path.write_text(big + "\n" + event_line("b", 2) + "\n")
    lines = list(read_lines(str(path), 0, chunk_bytes=64))
    assert [json.loads(t)["uuid"] for _, _, t in lines] == ["a", "b"]
    assert lines[0][1] == len(big) + 1


def test_missing_file_reads_as_empty(tmp_path):
    path = tmp_path / "events.jsonl"
    assert list(read_lines(str(path))) == []
    assert file_size(str(path)) == 0


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
    lines = list(read_lines(str(path)))
    assert [json.loads(t)["uuid"] for _, _, t in lines] == ["a", "b"]


def test_read_bytes_at_refuses_a_short_read(tmp_path):
    """A truncated log must raise rather than hand back a fragment — the
    index treats it as "the bytes moved" and the page says so."""
    path = tmp_path / "events.jsonl"
    path.write_text("abcdef\n")
    assert read_bytes_at(str(path), 0, 7) == b"abcdef\n"
    with pytest.raises(OSError):
        read_bytes_at(str(path), 0, 100)
