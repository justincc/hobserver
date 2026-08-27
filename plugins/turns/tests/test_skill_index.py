"""Effective skill description (ADR 24).

The truncated routing entry is read from the prompt, never reconstructed, so
the tests pin the drift-proof recognition: an entry is this skill's only when
its text (minus hermes' `...`) is a proper prefix of the full description, and
an untruncated entry yields nothing."""

import types

from plugins.turns import skill_index as si
from plugins.turns.assembler import Assembly, Session, Turn


SYSTEM = (
    "You are hermes. Guidance follows.\n"
    "- note: this hyphen line is not a skill index entry\n"
    "  finance:\n"
    "    - crypto-analysis: Record and maintain crypto protocol/token investment argu...\n"
    "    - events-hunter: Find and track AI/technology events that match the user's...\n"
    "    - blog-topic: a skill to help the user choose a blog topic\n"
)

EVENTS_FULL = ("Find and track AI/technology events that match the user's "
               "evolving interests, especially London/Cambridge AI-agent events.")


def test_a_truncated_entry_is_returned_verbatim():
    got = si.effective_description("events-hunter", EVENTS_FULL, SYSTEM)
    assert got == "Find and track AI/technology events that match the user's..."


def test_an_untruncated_entry_yields_nothing():
    # blog-topic's index entry is the whole description — no "...", so there is
    # nothing the frontmatter below does not already show.
    full = "a skill to help the user choose a blog topic"
    assert si.effective_description("blog-topic", full, SYSTEM) is None


def test_a_name_absent_from_the_index_yields_nothing():
    assert si.effective_description("no-such-skill", "whatever", SYSTEM) is None


def test_an_entry_that_is_not_a_prefix_of_the_full_desc_is_rejected():
    # Guards a coincidental "- name: ..." line: only a genuine prefix of THIS
    # skill's description counts, so a mismatched full description returns None.
    assert si.effective_description(
        "events-hunter", "Something completely unrelated to events.", SYSTEM) is None


def test_the_cut_length_is_not_encoded():
    # A different cut point still works — the check is prefix, not a number.
    system = "    - foo: Alpha bravo charlie delta...\n"
    assert si.effective_description("foo", "Alpha bravo charlie delta echo foxtrot",
                                    system) == "Alpha bravo charlie delta..."


def test_index_entry_matches_the_named_line_only():
    assert si._index_entry("events-hunter", SYSTEM).startswith("Find and track")
    assert si._index_entry("finance", SYSTEM) is None      # a header, not an entry


def test_full_description_reads_the_frontmatter(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: s\ndescription: The full sentence here.\n---\n# s\n",
        encoding="utf-8")
    assert si._full_description(str(d)) == "The full sentence here."


def test_system_text_reads_instructions_and_system_message():
    prof = {"annotated_request": {"instructions": "sys A",
                                  "messages": [{"role": "system", "content": "sys B"},
                                               {"role": "user", "content": "hi"}]}}
    assert si._system_text(prof) == "sys A\nsys B"


def _assembly_with_llm(profile):
    span = types.SimpleNamespace(category="llm", start_us=100,
                                 category_profile=profile, payload_elided=False)
    turn = Turn(session_id="s1", turn_id="t1", start_us=100, spans=[span])
    return Assembly(sessions=[Session(session_id="s1", turns=[turn])], anomalies=[])


def _skill(tmp_path, name, description):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8")
    return str(d)


def test_end_to_end_surfaces_the_truncated_entry(tmp_path):
    d = _skill(tmp_path, "events-hunter", EVENTS_FULL)
    asm = _assembly_with_llm({"annotated_request": {"instructions": SYSTEM}})
    got = si.effective_description_for_skill(d, "events-hunter", asm, None)
    assert got == "Find and track AI/technology events that match the user's..."


def test_end_to_end_is_none_for_an_untruncated_skill(tmp_path):
    d = _skill(tmp_path, "blog-topic", "a skill to help the user choose a blog topic")
    asm = _assembly_with_llm({"annotated_request": {"instructions": SYSTEM}})
    assert si.effective_description_for_skill(d, "blog-topic", asm, None) is None


def test_end_to_end_is_none_without_an_assembly(tmp_path):
    d = _skill(tmp_path, "events-hunter", EVENTS_FULL)
    assert si.effective_description_for_skill(d, "events-hunter", None, None) is None


def test_the_skill_page_shows_the_effective_description(tmp_path):
    # The whole path: an llm call in the log carries the skill index in its
    # system prompt, the skill is on disk with the full description, and the
    # route hydrates the call to read what hermes routes on.
    from testkit import make_app
    from plugins.turns.tests.test_turns import (write_atof, mark_line,
                                                scope_lines, _assistant)
    root = tmp_path / "skills"
    _skill(root, "events-hunter", EVENTS_FULL)  # _skill mkdirs under root
    system = ("You are Hermes.\n"
              "    - events-hunter: Find and track AI/technology events that "
              "match the user's...\n")
    lines = [
        mark_line("hermes.turn.start", 1_000_000, session="s1", turn="t1"),
        *scope_lines("L1", "llm", 1_100_000, 1_600_000, name="openai-codex",
                     session="s1", turn="t1",
                     profile={"annotated_request": {"instructions": system}},
                     start_data={"headers": {}}, end_data=_assistant("done")),
        mark_line("hermes.turn.end", 2_000_000, session="s1", turn="t1"),
    ]
    atof = write_atof(tmp_path, lines)
    app = make_app([{"plugin": "plugins.turns",
                     "settings": {"atof_log": str(atof),
                                  "skill_roots": [str(root)]}}])
    page = app.test_client().get(
        "/turns/skill?name=events-hunter").get_data(as_text=True)
    assert "Effective description" in page
    # The apostrophe in "user's" is HTML-escaped in the page, so assert the
    # part before it plus the truncation ellipsis.
    assert "Find and track AI/technology events that match the" in page
    assert "..." in page
