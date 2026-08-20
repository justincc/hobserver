"""Provider spec tests (ADR 13) — how each router's token counts read.

The shapes here are copied from live payloads: openai-codex on OpenAI's
Responses API, openrouter on Chat Completions, and Anthropic's Messages
shape, whose input convention is the opposite of the other two.
"""

from providers import (COMMON_COUNTS, PARTS, USAGE_SHAPES,
                                       WHOLE, FRESH_INPUT_COUNTS, UsageShape,
                                       canonical_usage, check_shapes,
                                       chunk_usage, shape_for, shape_modules)


# --- the chat-completions shape ------------------------------------------
# Copied from a live openrouter call (moonshotai/kimi-k3, 2026-08-07). The
# route reports the same counts as the responses API under entirely
# different names, and nests the cache read under the prompt rather than
# under an "input".

OPENROUTER_USAGE = {
    "prompt_tokens": 29786,
    "prompt_tokens_details": {"audio_tokens": 0, "cache_write_tokens": 0,
                              "cached_tokens": 29248, "video_tokens": 0},
    "completion_tokens": 465,
    "completion_tokens_details": {"accepted_prediction_tokens": None,
                                  "audio_tokens": 0, "image_tokens": 0,
                                  "reasoning_tokens": 12,
                                  "rejected_prediction_tokens": None},
    "total_tokens": 30251,
}


def test_chat_completions_usage_is_remapped_to_canonical_names():
    assert canonical_usage(OPENROUTER_USAGE) == {
        "prompt_tokens": 29786,
        "cache_read_tokens": 29248,
        "cache_write_tokens": 0,
        "output_tokens": 465,
        "reasoning_tokens": 12,
        "total_tokens": 30251,
    }


def test_hermes_own_annotation_flattens_the_cache_read():
    """Same names as the chat route, minus the nesting — one table covers
    both, which is why `cache_read_tokens` lists two sources."""
    assert canonical_usage({"prompt_tokens": 18782, "cache_read_tokens": 1792,
                            "completion_tokens": 2248,
                            "total_tokens": 21030}) == {
        "prompt_tokens": 18782, "cache_read_tokens": 1792,
        "output_tokens": 2248, "total_tokens": 21030,
    }


def test_the_nested_cache_read_wins_over_the_flattened_one():
    """Both sources present is not a shape seen in the log, but the table
    lists the nested one first and the order has to mean something."""
    assert canonical_usage({"prompt_tokens": 100, "cache_read_tokens": 1,
                            "prompt_tokens_details": {"cached_tokens": 2},
                            })["cache_read_tokens"] == 2


def test_usage_in_no_known_shape_yields_nothing():
    """Rather than half-reading it: `data` is opaque, and a count under a
    name this app does not recognise is not a count it can name on screen."""
    assert canonical_usage({"tokens_used": 500, "total_tokens": 500}) == {}
    assert canonical_usage(None) == {}
    assert canonical_usage("1,338/1,375") == {}


def test_chunk_usage_derives_fresh_input_like_an_end_payload_would():
    assert chunk_usage({"prompt_tokens": 18782, "cache_read_tokens": 1792,
                        "completion_tokens": 2248}) == {
        "prompt_tokens": 18782, "cache_read_tokens": 1792,
        "output_tokens": 2248, "input_tokens": 18782 - 1792,
    }


# --- the two input conventions -------------------------------------------
# Anthropic's `input_tokens` is what was sent *fresh*, with the cache read
# and write counted alongside it; OpenAI's is the whole prompt with the
# cache inside it. Reading one as the other is a wrong number, not a missing
# one. hermes forks on the same distinction in `agent/usage_pricing.py`.

ANTHROPIC_USAGE = {"input_tokens": 1200, "output_tokens": 300,
                   "cache_read_input_tokens": 18000,
                   "cache_creation_input_tokens": 900}


def test_the_parts_convention_sums_to_the_prompt_rather_than_subtracting():
    assert chunk_usage(ANTHROPIC_USAGE) == {
        "input_tokens": 1200,           # reported, not derived
        "cache_read_tokens": 18000,
        "cache_write_tokens": 900,
        "output_tokens": 300,
        "prompt_tokens": 1200 + 18000 + 900,    # derived the other way
    }


def test_both_conventions_agree_on_the_same_call():
    """The regression this fork exists to prevent: the same 20,100-token
    call read under either spelling gives one prompt and one fresh input."""
    openai_shaped = {"prompt_tokens": 20100, "completion_tokens": 300,
                     "prompt_tokens_details": {"cached_tokens": 18000,
                                               "cache_write_tokens": 900}}
    parts = chunk_usage(ANTHROPIC_USAGE)
    whole = chunk_usage(openai_shaped)
    for key in ("prompt_tokens", "input_tokens", "cache_read_tokens",
                "cache_write_tokens", "output_tokens"):
        assert parts[key] == whole[key], key


def test_anthropic_cache_names_do_not_by_themselves_mean_the_parts_convention():
    """OpenAI-compatible proxies routing Claude expose Anthropic's cache key
    names beside an OpenAI-shaped `prompt_tokens`, which is still the whole
    prompt. Summing there would double-count the cache."""
    proxied = {"prompt_tokens": 20100, "completion_tokens": 300,
               "cache_read_input_tokens": 18000,
               "cache_creation_input_tokens": 900}
    assert chunk_usage(proxied)["prompt_tokens"] == 20100     # not 39,000
    assert chunk_usage(proxied)["input_tokens"] == 1200


def test_a_bare_input_tokens_is_read_as_the_whole_prompt():
    """Nothing said about caching: the two conventions coincide, and this is
    the reading that invents no cache rows."""
    assert chunk_usage({"input_tokens": 1200, "output_tokens": 300}) == {
        "prompt_tokens": 1200, "output_tokens": 300}


def test_neither_direction_is_derived_from_a_payload_silent_on_caching():
    """Subtracting nothing would claim the whole prompt was fresh; adding
    nothing would claim none of it was cached. Both are claims about
    caching from a payload that made none."""
    assert "input_tokens" not in chunk_usage({"prompt_tokens": 20100})
    assert "prompt_tokens" not in canonical_usage(
        {"input_tokens": 1200, "cache_read_input_tokens": 18000})


def test_a_token_count_that_is_not_a_number_is_not_a_count():
    assert canonical_usage({"prompt_tokens": "many",
                            "completion_tokens": True,
                            "total_tokens": 5}) == {"total_tokens": 5}



# --- the extension point --------------------------------------------------
# A deployment names its own shapes in `provider_specs` and this app reads
# its router without a fork (ADR 13). The shape below is the worked example
# from docs/extending/writing-a-provider-spec.md: a fictional router that
# reports the cache split under names nothing else uses.

ACME_USAGE = {"acme_prompt_total": 4000, "acme_cached": 3200,
              "acme_generated": 120}

ACME = UsageShape(
    name="acme_router",
    convention=WHOLE,
    counts=(("prompt_tokens", ("acme_prompt_total",)),
            ("cache_read_tokens", ("acme_cached",)),
            ("output_tokens", ("acme_generated",))),
    matches=lambda usage: "acme_prompt_total" in usage,
)


def test_a_contributed_shape_reads_a_payload_the_built_ins_cannot():
    assert canonical_usage(ACME_USAGE) == {}        # unrecognised today
    assert chunk_usage(ACME_USAGE, (ACME,) + USAGE_SHAPES) == {
        "prompt_tokens": 4000, "cache_read_tokens": 3200,
        "output_tokens": 120, "input_tokens": 800,
    }


def test_a_contributed_shape_is_asked_before_the_built_ins():
    """`openai_compatible` claims any payload with a recognisable key, so a
    more specific shape can only win by being tried first."""
    overlapping = UsageShape(
        name="acme_openai_flavoured", convention=WHOLE,
        counts=(("prompt_tokens", ("prompt_tokens",)),
                ("output_tokens", ("completion_tokens",))),
        matches=lambda usage: "acme_trace_id" in usage)
    payload = {"acme_trace_id": "x", "prompt_tokens": 10, "completion_tokens": 2}
    assert shape_for(payload, (overlapping,) + USAGE_SHAPES) is overlapping
    assert shape_for(payload, USAGE_SHAPES).name == "openai_compatible"


def test_a_probe_that_raises_does_not_blank_every_other_call():
    """One bad third-party module must cost its own provider's rows and no
    one else's."""
    def explode(usage):
        raise RuntimeError("bad probe")

    broken = UsageShape(name="broken", convention=WHOLE,
                        counts=(("prompt_tokens", ("prompt_tokens",)),),
                        matches=explode)
    openrouter = {"prompt_tokens": 100, "completion_tokens": 5}
    assert chunk_usage(openrouter, (broken,) + USAGE_SHAPES)["prompt_tokens"] == 100


def test_check_shapes_accepts_the_built_ins():
    assert check_shapes(USAGE_SHAPES) == []
    assert check_shapes(ACME) != []              # not a sequence
    assert check_shapes([]) != []


def test_a_shape_whose_convention_contradicts_its_table_is_rejected():
    """The exact fault this fork exists to prevent, and silent if unchecked:
    declaring PARTS while mapping the figure to `prompt_tokens` would derive
    in the wrong direction."""
    wrong = UsageShape(name="wrong", convention=PARTS,
                       counts=(("prompt_tokens", ("prompt_tokens",)),),
                       matches=lambda usage: True)
    faults = check_shapes([wrong])
    assert any("input_tokens" in f and "parts" in f for f in faults)

    right = UsageShape(name="right", convention=PARTS,
                       counts=FRESH_INPUT_COUNTS + COMMON_COUNTS,
                       matches=lambda usage: True)
    assert check_shapes([right]) == []


def test_check_shapes_names_the_other_ways_a_shape_can_be_wrong():
    faults = check_shapes([UsageShape(name="odd", convention="sideways",
                                      counts=(("not_a_count", ("x",)),
                                              "not-a-pair"),
                                      matches="not callable")])
    joined = " | ".join(faults)
    assert "convention" in joined
    assert "non-callable" in joined
    assert "not a canonical count" in joined
    assert "(canonical_key, (step, ...))" in joined


def test_shape_modules_names_where_a_shape_came_from():
    """The index hashes these so editing a contributed shape invalidates an
    index built before the edit (ADR 11's cache rule)."""
    assert shape_modules(USAGE_SHAPES) == ["providers"]
    assert __name__ in shape_modules((ACME,))


# --- loading contributed shapes from settings -----------------------------

def write_provider_module(tmp_path, monkeypatch, name, body):
    """A provider spec module on sys.path, as an installed one would be."""
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


ACME_MODULE = '''
from providers import COMMON_COUNTS, WHOLE, UsageShape

def _is_acme(usage):
    return "acme_prompt_total" in usage

USAGE_SHAPES = (UsageShape(
    name="acme_router", convention=WHOLE,
    counts=(("prompt_tokens", ("acme_prompt_total",)),
            ("cache_read_tokens", ("acme_cached",))) + COMMON_COUNTS,
    matches=_is_acme),)
'''


def test_a_contributed_module_is_loaded_and_tried_first(tmp_path, monkeypatch):
    from plugins.turns import usage_shape_table

    name = write_provider_module(tmp_path, monkeypatch, "acme_providers",
                                 ACME_MODULE)
    shapes, notes = usage_shape_table({"provider_specs": [name]})
    assert shapes[0].name == "acme_router"
    assert shapes[1:] == USAGE_SHAPES              # built-ins kept behind it
    assert notes[0]["problem"] is None
    assert "acme_router" in notes[0]["from"]
    assert chunk_usage({"acme_prompt_total": 4000, "acme_cached": 3200},
                       shapes)["input_tokens"] == 800


def test_a_module_that_cannot_be_imported_is_reported_and_skipped():
    """The tab still serves on this tree's own shapes — a broken contribution
    costs its provider's token rows, not the log (ADR 5's failure model)."""
    from plugins.turns import usage_shape_table

    shapes, notes = usage_shape_table({"provider_specs": ["no_such_module"]})
    assert notes[0]["problem"].startswith("ModuleNotFoundError")
    assert shapes == USAGE_SHAPES


def test_a_module_with_no_shapes_is_reported(tmp_path, monkeypatch):
    from plugins.turns import usage_shape_table

    name = write_provider_module(tmp_path, monkeypatch, "empty_providers",
                                 "X = 1\n")
    _, notes = usage_shape_table({"provider_specs": [name]})
    assert "no USAGE_SHAPES" in notes[0]["problem"]


def test_a_malformed_shape_is_reported_rather_than_reaching_the_parser(
        tmp_path, monkeypatch):
    from plugins.turns import usage_shape_table

    name = write_provider_module(tmp_path, monkeypatch, "bad_providers", '''
from providers import PARTS, UsageShape
USAGE_SHAPES = (UsageShape(name="bad", convention=PARTS,
                           counts=(("prompt_tokens", ("prompt_tokens",)),),
                           matches=lambda u: True),)
''')
    shapes, notes = usage_shape_table({"provider_specs": [name]})
    assert "input_tokens" in notes[0]["problem"]
    assert shapes == USAGE_SHAPES              # skipped, not half-loaded


def test_provider_modules_accepts_one_path_without_brackets():
    from plugins.turns import provider_modules

    assert provider_modules({"provider_specs": "one.module"}) == ["one.module"]
    assert provider_modules({"provider_specs": ["a", "b"]}) == ["a", "b"]
    assert provider_modules({}) == []


def test_contributed_modules_appear_in_the_startup_sources(tmp_path, monkeypatch):
    """Same hook as the log and the scope specs, so a failed import is
    visible in the banner beside what it would have read."""
    from plugins.turns import sources

    # Explicit skill_roots so the banner's skill-root entries (ADR 22) are one
    # known row rather than whatever this machine's config.yaml resolves to.
    entries = sources({"atof_log": str(tmp_path / "nope.jsonl"),
                       "index_db": str(tmp_path / "index.sqlite3"),
                       "provider_specs": ["no_such_module"],
                       "skill_roots": [str(tmp_path)]})
    assert [e["label"] for e in entries] == [
        "ATOF log", "ATOF index", "provider spec", "skill root"]
    spec = next(e for e in entries if e["label"] == "provider spec")
    assert spec["problem"].startswith("ModuleNotFoundError")
