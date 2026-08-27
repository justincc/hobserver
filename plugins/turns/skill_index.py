"""Effective skill description — what hermes' router actually sees.

hermes lists every skill in the system prompt as a one-line index entry, and
truncates a long description there (agent/skill_utils.py, currently ~60 chars).
The model routes on that truncated line unless it opens the full skill, so a
description whose trigger falls past the cut is invisible to routing.

This surfaces the truncated entry hermes actually wrote, read from a recent
system prompt in the log — never a reconstruction, so the cut length is not
encoded here and cannot drift out of step with hermes:

- The system prompt is `annotated_request.instructions` (or a `system`
  message) on a recent llm call. hobserver already reads these payloads.
- An entry is recognised as *this* skill's by its name, and confirmed as a
  genuine truncation by the entry text (minus hermes' trailing `...`) being a
  proper prefix of the skill's full SKILL.md description.

If hermes changes the cut length, the prefix check still holds. If it changes
the index line shape past recognition, no entry is found and the row is simply
omitted — fail-open, like the rest of the app. An untruncated entry also
yields nothing: the frontmatter below already shows the full text.
"""

from __future__ import annotations

import os
import re
from typing import Any, List, Optional

from plugins.turns.atof_index import hydrate_span
from plugins.turns.spans import LLM_CATEGORY

MANIFEST = "SKILL.md"
# How many recent llm calls to try before giving up: the newest may be an
# in-flight call whose request has not been written, so fall back a few.
_RECENT_LLM_TRIES = 5


def effective_description_for_skill(skill_dir: str, name: str, assembly,
                                    atof_path: Optional[str],
                                    usage_shapes=None) -> Optional[str]:
    """The truncated index entry hermes routes on for `name`, or None.

    None when the log is unavailable, no recent system prompt can be read, the
    skill is not in the index, or its description is not truncated there.
    """
    if assembly is None:
        return None
    full = _full_description(skill_dir)
    if not full:
        return None
    system_text = _recent_system_text(assembly, atof_path, usage_shapes)
    if not system_text:
        return None
    return effective_description(name, full, system_text)


def effective_description(name: str, full_desc: str,
                          system_text: str) -> Optional[str]:
    """The entry for `name` in `system_text` when it is a truncation of
    `full_desc`, else None. Pure — the drift-proof core."""
    entry = _index_entry(name, system_text)
    if not entry or not full_desc:
        return None
    if not entry.endswith("..."):
        return None                       # not truncated; frontmatter has it
    core = entry[:-3].rstrip()
    full = full_desc.strip()
    # A genuine truncation: the shown text (minus the ellipsis) is a proper
    # prefix of the full description. This both confirms the line is really
    # this skill's index entry and rules out a description an author ended
    # with "..." of their own.
    if core and len(core) < len(full) and full.startswith(core):
        return entry
    return None


def _index_entry(name: str, system_text: str) -> Optional[str]:
    """The `- <name>: <text>` line's text in the system prompt, or None."""
    pattern = re.compile(r"(?m)^[ \t]*-[ \t]+" + re.escape(name)
                         + r"[ \t]*:[ \t]*(.+?)[ \t]*$")
    match = pattern.search(system_text)
    return match.group(1) if match else None


def _full_description(skill_dir: str) -> Optional[str]:
    """A skill's `description:` from its SKILL.md frontmatter, read shallowly
    and forgivingly (mirrors `skills._identity`)."""
    try:
        with open(os.path.join(skill_dir, MANIFEST), encoding="utf-8") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("\n---", 3)
    block = head[3:end] if end != -1 else head[3:]
    match = re.search(r"(?m)^\s*description:\s*(.+?)\s*$", block)
    if not match:
        return None
    return match.group(1).strip().strip("'\"") or None


def _recent_system_text(assembly, atof_path: Optional[str],
                        usage_shapes) -> Optional[str]:
    """A recent system prompt's text, hydrating an llm call to read it.

    The skill index is in every system prompt, so any recent llm call carries
    it. Hydration mutates the cached span in place, so a second read is free.
    """
    for span in _recent_llm_spans(assembly):
        if atof_path:
            try:
                hydrate_span(span, atof_path, usage_shapes)
            except Exception:
                continue
        text = _system_text(getattr(span, "category_profile", None))
        if text:
            return text
    return None


def _recent_llm_spans(assembly, limit: int = _RECENT_LLM_TRIES) -> List[Any]:
    spans: List[Any] = []
    for session in assembly.sessions:
        for turn in session.turns:
            spans.extend(s for s in turn.spans if s.category == LLM_CATEGORY)
        spans.extend(s for s in session.unassigned_spans
                     if s.category == LLM_CATEGORY)
    spans.sort(key=lambda s: s.start_us, reverse=True)
    return spans[:limit]


def _system_text(category_profile) -> Optional[str]:
    """The system-prompt text of a request: `instructions` (openai_responses)
    and any `system`-role message, concatenated."""
    if not isinstance(category_profile, dict):
        return None
    request = category_profile.get("annotated_request")
    if not isinstance(request, dict):
        return None
    parts: List[str] = []
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        parts.append(instructions)
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(block["text"] for block in content
                             if isinstance(block, dict)
                             and isinstance(block.get("text"), str))
    return "\n".join(parts) or None
