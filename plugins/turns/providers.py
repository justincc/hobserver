"""How a provider's LLM response reads — the only module that knows (ADR 13).

hermes talks to several provider APIs and they disagree about where the
assistant's words, its tool calls and its token counts live. Everything in
this app that depends on *which* provider answered is here; `atof_reader`
beside it knows the ATOF envelope and nothing about OpenAI or Anthropic.

Two halves, and they are extensible to different degrees on purpose:

- **Token counts are declarative** — a `UsageShape` is a probe plus a table
  of where each count sits — and are therefore the published extension
  point. A deployment names its own shapes in `provider_specs` and this
  app reads its router without a fork. See
  docs/extending/writing-a-provider-spec.md.
- **Assistant text and tool calls are code**, because reading them is a walk
  over typed content parts rather than a lookup. They are not published, and
  the reason is evidence rather than effort: hermes' own
  `annotated_response` carries both, uniformly across every route in the
  log, and leads over the raw payload — so a new provider's text usually
  works already. Token counts are where providers actually diverge, which is
  why that is the half worth publishing. If a router turns up whose text
  needs its own reader, `UsageShape` is a dataclass and gaining an optional
  callable is a backwards-compatible change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

# --- the two input conventions -------------------------------------------
# A provider's own "input" figure is the one count whose *meaning* varies,
# and reading one convention as the other is a wrong number rather than a
# missing one.
#
#   WHOLE  the entire prompt, cache included; the fresh part is the
#          remainder. OpenAI Chat Completions and Responses, and every
#          OpenAI-compatible proxy — openai-codex, openrouter.
#   PARTS  only what was sent fresh, with the cache read and write counted
#          alongside it, so the whole prompt is their sum. Anthropic
#          Messages.
#
# hermes forks on exactly this when it prices a call (the
# `anthropic_messages` arm of `agent/usage_pricing.py` adds where the others
# subtract), which is the corroboration for treating it as a real fork and
# not a quirk of one payload.
#
# The convention is not carried as a flag on the count. It is *which
# canonical key the table maps the figure to*, and `complete_the_prompt`
# fills in whichever of the two the producer did not report.
WHOLE = "whole"
PARTS = "parts"
CONVENTIONS = (WHOLE, PARTS)

# The canonical names, which are the ones `spans.TOKEN_TREE` renders. A
# shape may map any subset; `request_count` has no counterpart in any
# provider payload and is never mapped.
CANONICAL_COUNTS = ("prompt_tokens", "input_tokens", "cache_read_tokens",
                    "cache_write_tokens", "output_tokens", "reasoning_tokens",
                    "total_tokens")

# Sources for the counts that mean the same thing wherever they appear, so
# only the spelling varies. One list serves every provider; a contributed
# shape is welcome to reuse it rather than restate it.
#
# A canonical key may list several sources — the first that yields a count
# wins — so a producer that flattens a nested detail costs a row here rather
# than a branch anywhere.
COMMON_COUNTS = (
    ("cache_read_tokens", ("input_tokens_details", "cached_tokens")),
    ("cache_read_tokens", ("prompt_tokens_details", "cached_tokens")),
    ("cache_read_tokens", ("cache_read_input_tokens",)),    # Anthropic's names
    ("cache_read_tokens", ("cache_read_tokens",)),          # hermes' flattening
    ("cache_write_tokens", ("input_tokens_details", "cache_write_tokens")),
    ("cache_write_tokens", ("prompt_tokens_details", "cache_write_tokens")),
    ("cache_write_tokens", ("cache_creation_input_tokens",)),
    ("cache_write_tokens", ("cache_write_tokens",)),
    ("output_tokens", ("output_tokens",)),
    ("output_tokens", ("completion_tokens",)),
    ("reasoning_tokens", ("output_tokens_details", "reasoning_tokens")),
    ("reasoning_tokens", ("completion_tokens_details", "reasoning_tokens")),
    ("total_tokens", ("total_tokens",)),
)

WHOLE_PROMPT_COUNTS = (
    # `prompt_tokens` leads: it only ever means the whole prompt, where
    # `input_tokens` means different things to different producers.
    ("prompt_tokens", ("prompt_tokens",)),
    ("prompt_tokens", ("input_tokens",)),
)

FRESH_INPUT_COUNTS = (
    ("input_tokens", ("input_tokens",)),
)

# Anthropic's cache counts travel without Anthropic's input convention:
# OpenAI-compatible proxies routing Claude models expose these names beside
# an OpenAI-shaped `prompt_tokens` that is still the whole prompt (hermes
# carries the same fallback, citing OpenRouter, the Vercel AI Gateway and
# Cline). So they identify the cache *names*, never the convention alone.
ANTHROPIC_CACHE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens")

# A payload saying none of these is not a usage payload this app can read.
# `total_tokens` is deliberately absent: a grand total alone breaks down into
# no rows, so claiming to have recognised the shape would gain nothing.
OPENAI_USAGE_KEYS = ("prompt_tokens", "prompt_tokens_details",
                     "completion_tokens", "completion_tokens_details",
                     "input_tokens", "input_tokens_details", "output_tokens",
                     "output_tokens_details")


@dataclass(frozen=True)
class UsageShape:
    """One provider's token-count payload: how to recognise it, how to read it.

    `matches(usage)` decides whether a usage dict is this shape. It is
    probed on the payload's own keys rather than on the route name beside
    it: `data` is opaque per the ATOF spec and the route in `metadata` is a
    label, not a schema declaration. The payload is the thing that knows.

    `counts` maps canonical names to paths within the usage dict, as
    `(canonical_key, (step, ...))`. Earlier entries win, so listing a nested
    source before a flattened one expresses a preference rather than a
    branch.

    `convention` says what this provider's input figure means — see WHOLE
    and PARTS above. It must agree with `counts`: a WHOLE shape maps the
    figure to `prompt_tokens`, a PARTS shape to `input_tokens`. `check`
    enforces that rather than trusting it.
    """

    name: str
    convention: str
    counts: Sequence
    matches: Callable[[dict], bool]
    # Where this shape came from, for the startup banner. Filled in by the
    # loader; a shape declares its own name and nothing about its origin.
    origin: str = field(default="", compare=False)


def _openai_compatible(usage: dict) -> bool:
    return any(key in usage for key in OPENAI_USAGE_KEYS)


def _anthropic_messages(usage: dict) -> bool:
    """Both halves of the signature, and neither alone.

    A bare `input_tokens` with nothing said about caching is *not* claimed
    here: the two conventions coincide when nothing was cached, and leaving
    it to the whole-prompt shape is the reading that invents no cache rows.
    """
    return ("input_tokens" in usage
            and "input_tokens_details" not in usage
            and "prompt_tokens" not in usage
            and any(key in usage for key in ANTHROPIC_CACHE_KEYS))


ANTHROPIC_MESSAGES = UsageShape(
    name="anthropic_messages",
    convention=PARTS,
    counts=FRESH_INPUT_COUNTS + COMMON_COUNTS,
    matches=_anthropic_messages,
    origin="built in",
)

# Last, and deliberately the broadest: it claims any payload carrying a
# recognisable OpenAI-ish key, so a shape that must win has to be more
# specific *and* be tried first. Contributed shapes are prepended for
# exactly that reason.
OPENAI_COMPATIBLE = UsageShape(
    name="openai_compatible",
    convention=WHOLE,
    counts=WHOLE_PROMPT_COUNTS + COMMON_COUNTS,
    matches=_openai_compatible,
    origin="built in",
)

USAGE_SHAPES = (ANTHROPIC_MESSAGES, OPENAI_COMPATIBLE)


def check_shapes(shapes: Any) -> list:
    """Everything wrong with a contributed shape table, as plain sentences.

    Runs at startup so a malformed third-party shape is named in the banner
    rather than raising from inside the parser on some later line of the
    log. Mirrors `scopes.check_table` — same idea, same reporting.
    """
    faults = []
    if not isinstance(shapes, (list, tuple)) or not shapes:
        return ["USAGE_SHAPES must be a non-empty list of UsageShape"]
    for i, shape in enumerate(shapes):
        where = f"shape {i}"
        if not isinstance(shape, UsageShape):
            faults.append(f"{where} is {type(shape).__name__}, not a UsageShape")
            continue
        where = f"shape {shape.name!r}"
        if not shape.name or not isinstance(shape.name, str):
            faults.append(f"{where} has no name")
        if shape.convention not in CONVENTIONS:
            faults.append(f"{where} has convention {shape.convention!r}, "
                          f"not one of {', '.join(CONVENTIONS)}")
        if not callable(shape.matches):
            faults.append(f"{where} has a non-callable `matches`")
        if not isinstance(shape.counts, (list, tuple)) or not shape.counts:
            faults.append(f"{where} maps no counts")
            continue
        mapped = set()
        for entry in shape.counts:
            if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                    or not isinstance(entry[1], (list, tuple))):
                faults.append(f"{where} has a count that is not "
                              f"(canonical_key, (step, ...)): {entry!r}")
                continue
            key, path = entry
            if key not in CANONICAL_COUNTS:
                faults.append(f"{where} maps {key!r}, which is not a canonical "
                              f"count ({', '.join(CANONICAL_COUNTS)})")
            if not all(isinstance(step, str) for step in path):
                faults.append(f"{where} has a non-string path step in {path!r}")
            mapped.add(key)
        # The convention has to match what the table actually does, or
        # `complete_the_prompt` derives in the wrong direction — the exact
        # failure this fork exists to prevent, and silent if unchecked.
        expected = "prompt_tokens" if shape.convention == WHOLE else "input_tokens"
        if shape.convention in CONVENTIONS and expected not in mapped:
            faults.append(
                f"{where} declares the {shape.convention} convention but maps "
                f"no {expected!r}; a {shape.convention} shape must map the "
                f"provider's input figure to {expected!r}")
    return faults


def shape_modules(shapes: Sequence) -> list:
    """The modules a shape table's behaviour comes from.

    The index hashes these into its fingerprint (ADR 11): a contributed
    shape decides what the stored token counts *mean*, so editing one has to
    invalidate an index built before the edit, exactly as editing this tree
    does.
    """
    names = []
    for shape in shapes:
        module = getattr(shape.matches, "__module__", None)
        if module and module not in names:
            names.append(module)
    return names


# --- reading a usage payload ---------------------------------------------

def _count(value: Any) -> Optional[int]:
    # bool is an int subclass and is never a token count
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def shape_for(usage: dict, shapes: Optional[Sequence] = None) -> Optional[UsageShape]:
    """The first shape claiming this payload, or None."""
    for shape in (USAGE_SHAPES if shapes is None else shapes):
        try:
            if shape.matches(usage):
                return shape
        except Exception:       # noqa: BLE001 - third-party probe
            # A probe that raises has disqualified itself, not the payload.
            # Falling through to the next shape keeps one bad contributed
            # module from blanking the token counts of every call.
            continue
    return None


def canonical_usage(usage: Any, shapes: Optional[Sequence] = None) -> dict:
    """Any provider's token counts under the canonical names.

    Only what the payload actually reported: an absent count stays absent,
    because absent and zero mean different things to every row that reads
    these (see `spans.TOKEN_TREE`). Nothing is derived here — see
    `complete_the_prompt`.
    """
    if not isinstance(usage, dict):
        return {}
    shape = shape_for(usage, shapes)
    if shape is None:
        return {}
    canonical: dict = {}
    for key, path in shape.counts:
        if key in canonical:
            continue                    # an earlier source already spoke
        value: Any = usage
        for step in path:
            value = value.get(step) if isinstance(value, dict) else None
        count = _count(value)
        if count is not None:
            canonical[key] = count
    return canonical


def complete_the_prompt(canonical: dict) -> dict:
    """Fill in whichever of `prompt` / `in` the provider did not report.

    `cache read + in + cache write == prompt` either way; which side of it
    the payload gave decides the direction:

    - the WHOLE convention gave `prompt`, so `in` is the remainder;
    - the PARTS convention gave `in`, so `prompt` is the sum.

    Derived only where the cache read was actually reported. Absent is not
    zero, and both directions would otherwise state something about caching
    that the payload did not: subtracting nothing claims the whole prompt
    was fresh, and adding nothing claims none of it was cached.

    Omitted too when the parts do not partition the prompt — a negative
    remainder is not a count this app can vouch for.
    """
    cache_read = canonical.get("cache_read_tokens")
    if cache_read is None:
        return canonical
    cache_write = canonical.get("cache_write_tokens", 0)
    prompt = canonical.get("prompt_tokens")
    fresh = canonical.get("input_tokens")
    if prompt is not None and fresh is None:
        remainder = prompt - cache_read - cache_write
        if remainder >= 0:
            canonical["input_tokens"] = remainder
    elif fresh is not None and prompt is None:
        canonical["prompt_tokens"] = fresh + cache_read + cache_write
    return canonical


def chunk_usage(usage: Any, shapes: Optional[Sequence] = None) -> dict:
    """A streaming chunk's token counts, ready to stand in for an end payload's.

    Providers that report usage only on the final chunk of a stream — the
    openrouter route among them — leave the span's own end payload with a
    null `usage`. The counts are the same counts, so they are read the same
    way and the assembler falls back to them (docs/design/adr/0011).
    """
    return complete_the_prompt(canonical_usage(usage, shapes))


def end_usage(usage: Any, annotated: dict,
              shapes: Optional[Sequence] = None) -> dict:
    """An end payload's token counts, filled out from hermes' annotation.

    The raw usage wins where both speak: on the responses route it is the
    more detailed of the two, reporting the cache write and the reasoning
    split that the annotation does not carry.
    """
    canonical = canonical_usage(usage, shapes)
    for key, value in canonical_usage(annotated.get("usage"), shapes).items():
        canonical.setdefault(key, value)
    return complete_the_prompt(canonical)


# --- reading a response payload ------------------------------------------
# Not published: see the module docstring. hermes' `annotated_response`
# leads on both of these and is uniform across every route in the log, so
# the raw readers below are the fallback for a response the annotation
# missed rather than the usual path.

def _output_items(data: Any, item_type: str) -> list:
    """Items of one type from a provider response's `output` array."""
    if not isinstance(data, dict):
        return []
    output = data.get("output")
    if not isinstance(output, list):
        return []
    return [i for i in output
            if isinstance(i, dict) and i.get("type") == item_type]


def _choices(data: Any) -> list:
    """A chat-completions response's `choices`, defensively."""
    if not isinstance(data, dict):
        return []
    choices = data.get("choices")
    if not isinstance(choices, list):
        return []
    return [c for c in choices
            if isinstance(c, dict) and isinstance(c.get("message"), dict)]


def assistant_text(data: Any, annotated: dict) -> str:
    """The assistant's words.

    `annotated_response.message` is hermes' own normalization and is
    uniform across the provider APIs it speaks — it matched the raw payload
    text exactly on every llm call in the log, `openai_responses` and
    `openai_chat` alike. So it leads, and the raw payloads are the fallback
    for a response the annotation missed.

    Reasoning items are deliberately not read on either route: that is the
    hidden reasoning, counted in the token tree but never shown.
    """
    message = annotated.get("message")
    if isinstance(message, str) and message:
        return message

    parts = []
    # openai_responses: a list of typed output items, whose message items
    # hold a list of typed content parts.
    for item in _output_items(data, "message"):
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    if parts:
        return "".join(parts)

    # openai_chat: choices, each with one message whose content is a string.
    for choice in _choices(data):
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content:
            parts.append(content)
    return "".join(parts)


def tool_calls(data: Any, annotated: Any) -> list:
    """The tools the model asked for, as the canonical
    [{name, id, arguments}].

    `annotated_response.tool_calls` is preferred: hermes has already decoded
    the arguments there, where the raw `output` array carries them as a JSON
    string. The raw items are the fallback for a response the annotation
    missed.
    """
    if isinstance(annotated, dict):
        calls = annotated.get("tool_calls")
        if isinstance(calls, list) and calls:
            return [c for c in calls if isinstance(c, dict)]
    calls = []
    # openai_responses: function_call items in the output array.
    for item in _output_items(data, "function_call"):
        calls.append({"name": item.get("name"),
                      "id": item.get("call_id") or item.get("id"),
                      "arguments": _decoded_arguments(item.get("arguments"))})
    if calls:
        return calls
    # openai_chat: tool_calls on each choice's message, where the name and
    # arguments sit one level down under "function".
    for choice in _choices(data):
        raw = choice["message"].get("tool_calls")
        if not isinstance(raw, list):
            continue
        for call in raw:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            calls.append({
                "name": function.get("name") or call.get("name"),
                "id": call.get("id"),
                "arguments": _decoded_arguments(
                    function.get("arguments", call.get("arguments"))),
            })
    return calls


def _decoded_arguments(arguments: Any) -> Any:
    """Tool-call arguments, decoded when the provider sent them as JSON text.
    An undecodable string is kept as it arrived — still readable."""
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def normalize_llm_end(data: Any, category_profile: dict,
                      shapes: Optional[Sequence] = None) -> Any:
    """A provider response object, rewritten as hermes' canonical llm end.

    Keeps the provider payload's own keys alongside the canonical ones, so
    nothing is lost to a reader who goes looking for what the provider
    actually said.
    """
    if not isinstance(data, dict) or "assistant_message" in data:
        # Already canonical. An event that declared no schema but speaks the
        # old envelope is left alone rather than rewritten — the mapping is
        # for provider-shaped payloads, and running it over a canonical one
        # would be this reader arguing with itself.
        return data
    annotated = category_profile.get("annotated_response")
    annotated = annotated if isinstance(annotated, dict) else {}
    if (not isinstance(data.get("output"), list)
            and not isinstance(data.get("choices"), list)
            and not annotated):
        # Not a provider response either. The ATOF spec makes `data` opaque
        # and other producers put their own shapes here; inventing an empty
        # assistant_message over one would be noise, not normalization.
        return data

    normalized = dict(data)
    normalized["assistant_message"] = {
        "role": "assistant",
        "content": assistant_text(data, annotated),
        "tool_calls": tool_calls(data, annotated),
    }
    finish_reason = annotated.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        # passed through as the exporter reports it. The old envelope's
        # "tool_calls" / "stop" split is not reconstructed here: it would be
        # this app inferring a provider's verdict from the presence of tool
        # calls, which the row beside it already shows.
        normalized["finish_reason"] = finish_reason
    usage = end_usage(data.get("usage"), annotated, shapes)
    if usage:
        normalized["usage"] = usage
    return normalized
