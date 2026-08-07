"""One whole value, made into something a browser can show (ADR 12).

The turn page deals in excerpts — an assistant message cut at 400
characters, a prompt reduced to its last user turn. This module takes the
value behind one of those and prepares the page that shows all of it:
markdown to HTML where the value is prose a model wrote or was sent, JSON
where it is a structure, escaped text otherwise.

Two things it is careful about, both because the value is someone else's
log text and not ours:

- **Nothing is trusted as markup.** The renderer is configured with raw
  HTML disabled, so a `<script>` in a prompt renders as the characters it
  is. Text and JSON are escaped by Jinja like any other value.
- **The renderer is optional.** `markdown-it-py` is a dependency, but a
  missing or broken one degrades this page to its text form and says so,
  rather than failing (design principle 1, degrade per component).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

# Rendered up front, once per process: a page under a live poll should not
# pay an import on first sight.
try:                                  # pragma: no cover - import shape
    from markdown_it import MarkdownIt
    # `html: False` explicitly, and it is the whole of the safety here: the
    # "commonmark" preset turns raw HTML *on* — full CommonMark includes it —
    # so taking the preset as it comes would put a `<script>` from someone's
    # prompt into this page as a script. Off, it renders as the characters it
    # is. `linkify: False` for the same reason in miniature: text that merely
    # looks like a URL is text.
    _MD = MarkdownIt("commonmark", {"html": False, "linkify": False})
    _MD_PROBLEM = None
except Exception as exc:              # noqa: BLE001 - a missing dependency is
    _MD = None                        # a state this page renders, not an error
    _MD_PROBLEM = f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class Section:
    """One labelled part of a value made of several — a message of a request.

    The label is this app's; the text is not. They are kept in separate
    fields all the way to the template, which draws the label as its own
    chrome, so nothing this app wrote ends up inside what it is showing.

    `nested` marks a part shown *under* the one before it, and `nests` the
    part it belongs to — the request page uses the pair for a tool result
    moved to sit with the call it answers, which are drawn as one card. Both
    are the source's word, carried through rather than inferred here: this
    module renders parts and does not know what any of them mean.
    """

    label: str
    text: str
    html: Optional[str] = None
    nested: bool = False
    nests: bool = False


@dataclass(frozen=True)
class Rendered:
    """What the full page shows, and what it should say about it.

    kind      "markdown" (html is set), "sections", "json", or "text".
    text      the value as characters — the raw view and the copy button.
              None for "sections", which has no single text: the page shows
              each section's own, under the same labels.
    html      rendered markdown, or None.
    sections  for "sections", the parts in order; empty otherwise.
    chars     how much wire text this holds — the figure the excerpt
              promised, and across every section when there are several.
    problem   why this is not what the scope asked for, when it is not:
              a missing renderer, or a value that was not there at all.
    """

    kind: str
    text: Optional[str]
    html: Optional[str] = None
    sections: tuple = ()
    chars: int = 0
    problem: Optional[str] = None


def as_text(value: Any) -> Optional[str]:
    """A resolved value as characters.

    A string is itself. Anything structured is JSON, indented — an ATOF
    payload key can hold a whole request, and printing it as a `repr` would
    be printing Python at someone reading a log.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _markdown(text: str):
    """(html, problem) for one string. Nothing here raises."""
    if _MD is None:
        return None, f"markdown not rendered — {_MD_PROBLEM}"
    try:
        return _MD.render(text), None
    except Exception as exc:  # noqa: BLE001 - third-party renderer, any text
        return None, f"markdown not rendered — {type(exc).__name__}: {exc}"


def _sections(value: Any) -> Optional[Rendered]:
    """A `[{label, text}]` value as labelled, separately rendered parts.

    Returns None when the value is not that shape, so the caller falls back
    to rendering it whole — a scope declaring `render="sections"` over
    something else gets the plainest true thing rather than an error.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    parts, problem = [], None
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            return None
        label = entry.get("label")
        html, fault = _markdown(entry["text"])
        problem = problem or fault
        parts.append(Section(label=str(label) if label else "(unlabelled)",
                             text=entry["text"], html=html,
                             nested=bool(entry.get("nested")),
                             nests=bool(entry.get("nests"))))
    return Rendered(kind="sections", text=None, sections=tuple(parts),
                    chars=sum(len(p.text) for p in parts), problem=problem)


def render(value: Any, how: str) -> Rendered:
    """`value` prepared for the page, in the form the scope asked for.

    A structure is JSON whatever `how` says: markdown of a dict would be
    markdown of its punctuation. That is a fallback and not a failure, so it
    carries no `problem` — the page names the form it is showing either way.
    `sections` falls back the same way when the value is not a list of them.
    """
    if how == "sections":
        sections = _sections(value)
        if sections is not None:
            return sections
    text = as_text(value)
    if text is None:
        return Rendered(kind="text", text=None,
                        problem="this value is not in the payload")
    if not isinstance(value, str):
        return Rendered(kind="json", text=text, chars=len(text))
    if how == "text":
        return Rendered(kind="text", text=text, chars=len(text))
    html, problem = _markdown(text)
    if html is None:
        return Rendered(kind="text", text=text, chars=len(text),
                        problem=problem)
    return Rendered(kind="markdown", text=text, html=html, chars=len(text))
