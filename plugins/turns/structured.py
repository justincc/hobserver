"""What a command printed, read into text and structured blocks (spans.py
`_read_command_result` supplies the text; fulltext.py hands this reading to
the page).

A script the agent wrote prints JSON, a Python value (`print(result)`), a run
of either, or a label with one on the same line (`print(name, result)`), with
text around them. This module scans the output once and splits it into text
segments and blocks, each block drawn as a tree. Nothing is guessed from how
the output looks: a block is a span that one of `BLOCK_READERS` parses in
full, and everything else stays text.

The output is someone else's log text, so the scan is bounded (SECURITY.md):

- **Only data is built.** `json` and `ast.literal_eval` construct literals
  and nothing else; no name, call or attribute in the text is evaluated.
- **Work is capped.** An output over `MAX_CHARS` is not scanned; every parse
  attempt spends from a budget proportional to the output; a Python block is
  measured by a bracket matcher that refuses nesting deeper than `MAX_NEST`
  before `literal_eval` (whose parser can exhaust the C stack) sees it.
- **Any failure is text.** A reader that raises, for any reason, leaves the
  span as text.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

# An output longer than this is shown as text without being scanned.
MAX_CHARS = 256 * 1024

# Bracket nesting a Python block may have before it is left as text.
MAX_NEST = 100

# Characters the readers may scan in total, as a multiple of the output's
# length. A scan that keeps retrying overlapping candidates (a line opening a
# bracket that closes only at the end) stops trying once this is spent.
BUDGET_FACTOR = 8

# How many candidate starts on one line are tried: the line's first bracket
# may be part of a label (`[INFO] got {...}`).
CANDIDATES_PER_LINE = 3

# How deep a tree is drawn as nested nodes. A subtree below this is one leaf
# holding its spelling, so a pathological nesting cannot recurse the template
# out of stack.
TREE_DEPTH = 40


class _Budget:
    def __init__(self, chars: int):
        self.remaining = chars

    def spend(self, chars: int) -> bool:
        """Take `chars` from the budget; False once it is exhausted."""
        self.remaining -= max(chars, 1)
        return self.remaining >= 0


@dataclass(frozen=True)
class BlockReader:
    """One format a block can be in.

    `read(text, pos, budget)` returns `(value, end)` for a value starting at
    `pos`, or None. `one` and `plural` name blocks of it in the page's
    heading. `spell` is how a scalar of that format is written, for
    the tree's leaves. `opens` are the characters a block of it can start
    with."""

    name: str
    one: str
    plural: str
    opens: str
    read: Callable[[str, int, _Budget], Optional[tuple]]
    spell: Callable[[Any], str]


_DECODER = json.JSONDecoder()


def _read_json(text: str, pos: int, budget: _Budget) -> Optional[tuple]:
    try:
        value, end = _DECODER.raw_decode(text, pos)
    except json.JSONDecodeError as exc:
        budget.spend(exc.pos - pos)
        return None
    except (ValueError, RecursionError, MemoryError):
        budget.spend(len(text) - pos)
        return None
    budget.spend(end - pos)
    return value, end


def _python_extent(text: str, pos: int, budget: _Budget) -> Optional[int]:
    """Where the bracketed value starting at `pos` ends, or None.

    Tracks `([{` against `)]}` by depth only (literal_eval checks pairing)
    and skips quoted strings with their backslash escapes. A newline inside
    a string, nesting past `MAX_NEST`, or running out of budget is None."""
    depth, quote, i, n = 0, None, pos, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
            elif c == "\n":
                break
        elif c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
            if depth > MAX_NEST:
                break
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1 if budget.spend(i + 1 - pos) else None
        i += 1
    budget.spend(i - pos)
    return None


def _read_python(text: str, pos: int, budget: _Budget) -> Optional[tuple]:
    end = _python_extent(text, pos, budget)
    if end is None or not budget.spend(end - pos):
        return None
    try:
        return ast.literal_eval(text[pos:end]), end
    except Exception:  # noqa: BLE001 - any failure, RecursionError and
        return None      # MemoryError included, leaves the span as text


def _spell_python(value: Any) -> str:
    return repr(value)


def _spell_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# Tried in order at each candidate start; the first that reads a block wins,
# so JSON keeps what both would parse. A format is one entry here.
BLOCK_READERS = (
    BlockReader("JSON", "a JSON value", "JSON values", "{[", _read_json,
                _spell_json),
    BlockReader("Python literal", "a Python literal", "Python literals",
                "{[(", _read_python, _spell_python),
)


def _candidates(text: str, start: int, stop: int, opens: str):
    """Positions in `text[start:stop]` (one line) where a block may start: a
    bracket at the line's first non-blank, or after a space, tab, `:` or `=`
    — a label's end. A label ending in a quoted key (`"key":`) is a line
    inside a larger value, often one cut off, so its bracket is none. At
    most `CANDIDATES_PER_LINE` of them."""
    found = 0
    for i in range(start, stop):
        if text[i] not in opens:
            continue
        label = text[start:i].rstrip()
        if not label or (text[i - 1] in " \t:="
                         and not label.endswith(('":', "':"))):
            yield i
            found += 1
            if found == CANDIDATES_PER_LINE:
                return


def _ends_its_line(text: str, end: int) -> bool:
    newline = text.find("\n", end)
    return not text[end:newline if newline >= 0 else len(text)].strip()


def _is_container(value: Any) -> bool:
    return isinstance(value, (dict, list, tuple, set, frozenset)) and \
        len(value) > 0


def _one_scalar(value: Any) -> bool:
    """A container of a single scalar — after a label, as often a citation
    (`… revenue. [8]`) as a value, and no easier to read as a tree."""
    if len(value) != 1:
        return False
    only = next(iter(value.values() if isinstance(value, dict) else value))
    return not isinstance(only, (dict, list, tuple, set, frozenset))


def structure(text: str) -> Optional[dict]:
    """`text` split into text segments and structured blocks, or None when it
    holds no block and is shown as the text it is.

    A block is a non-empty object/dict, array/list, tuple or set that one of
    `BLOCK_READERS` parses from a candidate start (`_candidates`) to the end
    of a line; it may span lines. After a label it must hold more than one
    scalar (`_one_scalar`). Returns `{summary, segments}`: `segments`
    in output order, each `{text}` or `{format, tree}` (`tree` a node, see
    `_node`), and `summary` what the output was read as, for the heading —
    "JSON" for one JSON value alone."""
    if not isinstance(text, str) or len(text) > MAX_CHARS:
        return None
    opens = "".join(sorted({c for r in BLOCK_READERS for c in r.opens}))
    budget = _Budget(BUDGET_FACTOR * len(text) + 1024)
    segments, counts, last, pos, n = [], {}, 0, 0, len(text)
    while pos < n and budget.remaining > 0:
        line_end = text.find("\n", pos)
        line_end = n if line_end < 0 else line_end
        block = None
        for start in _candidates(text, pos, line_end, opens):
            for reader in BLOCK_READERS:
                if text[start] not in reader.opens:
                    continue
                read = reader.read(text, start, budget)
                labelled = bool(text[pos:start].strip())
                if read and _is_container(read[0]) \
                        and _ends_its_line(text, read[1]) \
                        and not (labelled and _one_scalar(read[0])):
                    block = (reader, start, read[0], read[1])
                    break
                if read:
                    break       # a value read but not to the line end
            if block:
                break
        if block is None:
            pos = line_end + 1
            continue
        reader, start, value, end = block
        _add_text(segments, text[last:start])
        segments.append({"format": reader.name,
                         "tree": _node(value, reader.spell, 0)})
        counts[reader] = counts.get(reader, 0) + 1
        last = end
        pos = end
    if not counts:
        return None
    _add_text(segments, text[last:])
    return {"summary": _summary(counts, any("text" in s for s in segments)),
            "segments": segments}


def _add_text(segments: list, text: str) -> None:
    """A text segment, with the line break that ended the block before it
    and the blanks before the block after it trimmed; nothing when blank."""
    text = text.strip("\n").rstrip()
    if text.strip():
        segments.append({"text": text})


def _summary(counts: dict, has_text: bool) -> str:
    if not has_text and list(counts.values()) == [1]:
        only = next(iter(counts))
        if only.name == "JSON":
            return "JSON"
    said = " and ".join(f"{n} {r.plural}" if n > 1 else r.one
                        for r, n in counts.items())
    return f"text with {said}" if has_text else said


def json_tree(value: Any) -> dict:
    """An already-parsed JSON value as a tree node (`_node`), for a value
    that arrived as structure rather than printed text."""
    return _node(value, _spell_json, 0)


def _node(value: Any, spell: Callable[[Any], str], depth: int) -> dict:
    """A value as a tree node: `{kind: "object" | "array" | "tuple" | "set",
    children: [{key, node}]}` (`key` None but in an object) or `{kind:
    "scalar", type, value}`, `value` spelled as the format writes it and
    `type` one of string, number, bool, null or deep (a subtree below
    `TREE_DEPTH`, spelled whole)."""
    if isinstance(value, (dict, list, tuple, set, frozenset)) \
            and depth >= TREE_DEPTH:
        try:
            spelled = spell(value)
        except (ValueError, RecursionError):
            spelled = "…"
        return {"kind": "scalar", "type": "deep", "value": spelled}
    if isinstance(value, dict):
        return {"kind": "object", "children": [
            {"key": k if isinstance(k, str) else spell(k),
             "node": _node(v, spell, depth + 1)} for k, v in value.items()]}
    if isinstance(value, (list, tuple, set, frozenset)):
        kind = ("array" if isinstance(value, list) else
                "tuple" if isinstance(value, tuple) else "set")
        return {"kind": kind, "children": [
            {"key": None, "node": _node(v, spell, depth + 1)} for v in value]}
    kind = ("null" if value is None else "bool" if isinstance(value, bool)
            else "string" if isinstance(value, (str, bytes)) else "number")
    return {"kind": "scalar", "type": kind, "value": spell(value)}
