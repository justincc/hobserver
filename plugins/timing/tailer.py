"""ATOF tailer — the bottom layer of the reader (docs/adr/0002).

Incrementally reads the ATOF JSONL file, keeping a byte-offset cursor so
each refresh parses only what the exporter appended since last time.
Parsed events and errors accumulate in memory; the assembler runs over the
accumulated events per request.

Cursor rules (ADR 2):
- only complete lines are consumed — a partial trailing line (the exporter
  mid-write) stays unread until its newline arrives;
- if the file is shorter than the cursor (overwrite mode, rotation), reset
  to zero and re-read from scratch;
- a missing file resets the state — the view shows the loud no-source
  message, and a recreated file is read fresh.
"""

from __future__ import annotations

import os
import threading
from typing import List

from plugins.timing.atof_reader import AtofEvent, ParseError, parse_lines


class AtofTailer:
    def __init__(self, path: str):
        self.path = path
        self.events: List[AtofEvent] = []
        self.errors: List[ParseError] = []
        self._offset = 0
        self._next_line_no = 1
        self._lock = threading.Lock()

    def _reset(self) -> None:
        self.events = []
        self.errors = []
        self._offset = 0
        self._next_line_no = 1

    def refresh(self) -> None:
        """Read and parse whatever the exporter appended since last call."""
        with self._lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                self._reset()
                return
            if size < self._offset:
                self._reset()
            if size == self._offset:
                return
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
            cut = chunk.rfind(b"\n")
            if cut == -1:      # nothing but a partial line so far
                return
            complete = chunk[: cut + 1]
            self._offset += len(complete)
            lines = complete.decode("utf-8", errors="replace").splitlines()
            events, errors = parse_lines(lines, first_line_no=self._next_line_no)
            self._next_line_no += len(lines)
            self.events.extend(events)
            self.errors.extend(errors)
