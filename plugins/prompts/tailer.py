"""ATOF line reader — the bottom layer of the reader (docs/design/adr/0002).

Yields complete lines of the JSONL file from a byte offset, each with the
offset and length it occupies. Those two numbers are what
[ADR 11](../../docs/design/adr/0011-index-the-atof-log-rather-than-hold-it-in-memory.md)
stores in place of the payload, so a page can read one span's payload back
out of the log without holding the file in memory.

This layer parses nothing and remembers nothing: the index owns the cursor,
because the cursor and the rows built from it have to advance together or
not at all.

Cursor rules (ADR 2):
- only complete lines are read — a partial trailing line (the exporter
  mid-write) stays unread until its newline arrives;
- callers compare the file's size against their stored offset before
  reading: a shorter file is overwrite mode or rotation, and means start
  again from zero;
- a missing file is not an error here, it is an empty read.
"""

from __future__ import annotations

import os
from typing import Iterator, Tuple

# Bytes pulled from the file at a time. Large enough that a full rebuild is
# not syscall-bound, small enough that rebuilding a multi-gigabyte log never
# holds more than this much of it at once.
READ_CHUNK_BYTES = 4 * 1024 * 1024


def file_size(path: str) -> int:
    """Size of the log, or 0 when it is not there."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_lines(path: str, offset: int = 0,
               chunk_bytes: int = READ_CHUNK_BYTES) -> Iterator[Tuple[int, int, str]]:
    """Yield ``(offset, length, text)`` per complete line from ``offset``.

    ``offset`` and ``length`` are byte positions in the file and include the
    trailing newline, so the pair round-trips through ``seek``/``read``.
    ``text`` is the decoded line without its newline.

    **Split the chunk on b"\\n" only — never str.splitlines().** splitlines
    also breaks on U+0085, U+2028 and U+2029, which JSON does not require
    escaping and which hermes' assistant text contains verbatim. That
    shredded whole records into unparseable fragments; when the shredded
    record was a `hermes.turn.end`, its turn stayed open forever, so a
    long-finished turn kept polling at 2 s.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        handle.seek(offset)
        position = offset
        pending = b""
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                return
            pending += chunk
            start = 0
            while True:
                cut = pending.find(b"\n", start)
                if cut == -1:
                    break
                raw = pending[start:cut + 1]
                yield position, len(raw), raw[:-1].decode("utf-8", errors="replace")
                position += len(raw)
                start = cut + 1
            pending = pending[start:]


def read_bytes_at(path: str, offset: int, length: int) -> bytes:
    """Exactly the bytes at ``offset``, raising if they are not all there.

    Raises OSError if the log has gone or is now shorter than the region it
    is supposed to hold. Both callers treat a short read as "the log moved
    under us": the index rebuilds, and a page says so rather than rendering
    a blank payload (ADR 2's loud-failure rule).
    """
    with open(path, "rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    if len(raw) != length:
        raise OSError(f"{path}: only {len(raw)} of {length} bytes at {offset}")
    return raw


def read_at(path: str, offset: int, length: int) -> str:
    """One line, decoded, without its trailing newline."""
    return read_bytes_at(path, offset, length).rstrip(b"\n").decode(
        "utf-8", errors="replace")
