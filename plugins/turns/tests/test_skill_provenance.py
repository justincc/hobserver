"""Skill provenance classification (ADR 23).

The origin is read from hermes' sidecar files, never inferred. These tests pin
each bucket, the precedence between them, and the two honest fallbacks: an
unmarked skill is "user-added" (with the caveat that it may be agent-authored
at the user's request), and a root with no sidecars at all is "unknown"."""

import json
import os

from plugins.turns import skills, skill_provenance as prov
from plugins.turns.tests.test_skills import make_skill, skill_client


def _roots(*dirs):
    return skills.skill_roots({"skill_roots": [str(d) for d in dirs]})


def _write_sidecars(root, *, bundled=(), hub=(), usage=None):
    if bundled:
        (root / ".bundled_manifest").write_text(
            "".join(f"{n}:deadbeef\n" for n in bundled), encoding="utf-8")
    if hub:
        (root / ".hub").mkdir(parents=True, exist_ok=True)
        (root / ".hub" / "lock.json").write_text(
            json.dumps({"installed": {n: {"identifier": f"official/{n}"}
                                      for n in hub}}), encoding="utf-8")
    if usage is not None:
        (root / ".usage.json").write_text(json.dumps(usage), encoding="utf-8")


def test_bundled_skill_is_read_from_the_manifest(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "diagramming")
    _write_sidecars(root, bundled=["diagramming"], usage={})
    assert prov.classify(str(d), _roots(root)).origin_key == "bundled"


def test_hub_installed_skill_is_read_from_the_lock(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "baoyu-illustrator")
    _write_sidecars(root, hub=["baoyu-illustrator"], usage={})
    p = prov.classify(str(d), _roots(root))
    assert p.origin_key == "hub"
    assert ("Hub source", "official/baoyu-illustrator") in p.rows


def test_hub_wins_over_a_bundled_seed(tmp_path):
    # hermes treats a hub install as authoritative over a bundled seed
    # (tools/skill_usage.py checks hub first); we mirror that precedence.
    root = tmp_path / "skills"
    d = make_skill(root, "overlap")
    _write_sidecars(root, bundled=["overlap"], hub=["overlap"], usage={})
    assert prov.classify(str(d), _roots(root)).origin_key == "hub"


def test_agent_created_skill_is_read_from_usage(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "self-authored")
    _write_sidecars(root, usage={"self-authored": {
        "created_by": "agent",
        "created_at": "2026-08-01T12:00:00+00:00",
        "use_count": 4, "last_used_at": "2026-08-20T09:30:00+00:00"}})
    p = prov.classify(str(d), _roots(root))
    assert p.origin_key == "agent"
    assert p.created == "2026-08-01 12:00 UTC"
    assert p.last_used == "2026-08-20 09:30 UTC"
    assert ("Times used", "4") in p.rows


def test_unmarked_skill_is_user_added_with_the_agent_caveat(tmp_path):
    # A record exists but created_by is null: manually authored, OR authored by
    # hermes at the user's request (hermes leaves those unmarked).
    root = tmp_path / "skills"
    d = make_skill(root, "hand-written")
    _write_sidecars(root, usage={"hand-written": {"created_by": None}})
    p = prov.classify(str(d), _roots(root))
    assert p.origin_key == "user"
    assert p.note and "agent-created" in p.note


def test_a_skill_with_no_record_but_present_sidecars_is_user_added(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "dropped-in")
    _write_sidecars(root, bundled=["something-else"], usage={})
    assert prov.classify(str(d), _roots(root)).origin_key == "user"


def test_no_sidecars_at_all_is_unknown(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "orphan")
    assert prov.classify(str(d), _roots(root)).origin_key == "unknown"


def test_external_dir_skill_is_labelled_external(tmp_path, monkeypatch):
    ext = tmp_path / "my-skills"
    d = make_skill(ext, "personal")
    monkeypatch.setattr(skills, "_external_dirs", lambda: [str(ext)])
    # Even with a bundled manifest sitting in the external root, external wins:
    # the origin is the configuration, decided before any sidecar is read.
    _write_sidecars(ext, bundled=["personal"])
    assert prov.classify(str(d), _roots(ext)).origin_key == "external"


def test_the_declared_name_not_the_dir_basename_keys_the_lookup(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "some-dir", identity="declared-name")
    _write_sidecars(root, bundled=["declared-name"], usage={})
    assert prov.classify(str(d), _roots(root)).origin_key == "bundled"


def test_last_modified_reflects_the_skill_md(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "dated")
    _write_sidecars(root, usage={})
    p = prov.classify(str(d), _roots(root))
    assert p.modified and p.modified.endswith("UTC")


def test_times_modified_reads_patch_count(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "edited")
    _write_sidecars(root, usage={"edited": {"created_by": None, "patch_count": 6}})
    assert prov.classify(str(d), _roots(root)).modified_count == 6


def test_times_modified_is_none_without_a_usage_record(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "no-record")
    _write_sidecars(root, bundled=["something-else"], usage={})
    assert prov.classify(str(d), _roots(root)).modified_count is None


def test_times_modified_renders_below_last_modified(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "edited")
    _write_sidecars(root, usage={"edited": {"created_by": None, "patch_count": 3}})
    body = skill_client(tmp_path, [root]).get(
        "/turns/skill?name=edited").get_data(as_text=True)
    assert "Times modified" in body
    assert body.index("Last modified") < body.index("Times modified")


def test_unreadable_usage_json_degrades_to_user_not_crash(tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "broken")
    (root / ".usage.json").write_text("{ not json", encoding="utf-8")
    # A sidecar exists (so not "unknown") but cannot be parsed: still classifies,
    # falling through to user-added rather than raising.
    assert prov.classify(str(d), _roots(root)).origin_key == "user"


def test_the_summary_box_renders_the_origin_on_the_skill_page(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "diagramming")
    _write_sidecars(root, bundled=["diagramming"], usage={})
    page = skill_client(tmp_path, [root]).get("/turns/skill?name=diagramming")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Bundled with hermes" in body
    assert "Last modified" in body
