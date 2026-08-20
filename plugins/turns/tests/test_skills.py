"""Skill resolution and — the part that matters — confinement to the roots.

The paths a skill scope carries come from the log, which is untrusted, so the
point of these tests is that nothing outside a configured root is ever served,
however the path is dressed up (traversal, an absolute path, a symlink out)."""

import os

import pytest

from testkit import make_app
from plugins.turns import skills


def skill_client(tmp_path, roots):
    """A Turns-only app confined to `roots`, no ATOF log needed — the skill
    route does not read one."""
    app = make_app([{"plugin": "plugins.turns",
                     "settings": {"atof_log": str(tmp_path / "none.jsonl"),
                                  "skill_roots": [str(r) for r in roots]}}])
    return app.test_client()


def make_skill(root, name, *, identity=None, files=None):
    """A skill directory under `root` with a SKILL.md and any extra files."""
    d = root / name
    d.mkdir(parents=True)
    front = f"---\nname: {identity}\n---\n" if identity else ""
    (d / "SKILL.md").write_text(front + f"# {name}\n\nbody\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


# --- roots ------------------------------------------------------------------

def test_explicit_roots_override_and_are_realpathed_and_deduped(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    roots = skills.skill_roots({"skill_roots": [str(a), str(a), str(a) + "/."]})
    assert roots == [os.path.realpath(a)]


def test_external_dirs_are_skipped_without_a_yaml_reader(tmp_path, monkeypatch):
    # The degradation path: no pyyaml means the config.yaml external_dirs are
    # simply not read, and only the standard roots remain.
    monkeypatch.setattr(skills, "yaml", None)
    assert skills._external_dirs() == []


# --- containment ------------------------------------------------------------

def test_a_path_inside_a_root_finds_it_and_outside_does_not(tmp_path):
    root = tmp_path / "root"
    (root / "skill").mkdir(parents=True)
    roots = [os.path.realpath(root)]
    assert skills.containing_root(str(root / "skill" / "x.md"), roots) == roots[0]
    assert skills.containing_root(str(tmp_path / "elsewhere" / "x"), roots) is None


def test_a_sibling_with_a_shared_prefix_is_not_inside(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    sibling = tmp_path / "skills-secret"
    sibling.mkdir()
    assert skills.containing_root(str(sibling / "x"), [os.path.realpath(root)]) is None


def test_a_symlink_pointing_out_of_a_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    link = root / "escape"
    os.symlink(secret, link)
    # realpath resolves the link to outside the root, so containment fails
    assert skills.containing_root(str(link), [os.path.realpath(root)]) is None


# --- resolving a skill directory -------------------------------------------

def test_resolve_by_a_nested_path_finds_the_manifest_dir(tmp_path):
    root = tmp_path / "root"
    d = make_skill(root, "alpha", files={"references/deep.md": "x"})
    roots = [os.path.realpath(root)]
    got = skills.resolve_skill_dir(roots, None, str(d / "references" / "deep.md"))
    assert got == os.path.realpath(d)


def test_resolve_by_name_prefers_declared_identity_over_basename(tmp_path):
    root = tmp_path / "root"
    # directory is "src", but the skill's declared name is "blog-topic"
    d = root / "blog" / "src"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: blog-topic\n---\n# x\n", encoding="utf-8")
    roots = [os.path.realpath(root)]
    assert skills.resolve_skill_dir(roots, "blog-topic", None) == os.path.realpath(d)


def test_resolve_by_name_falls_back_to_basename(tmp_path):
    root = tmp_path / "root"
    d = make_skill(root, "events-hunter")
    roots = [os.path.realpath(root)]
    assert skills.resolve_skill_dir(roots, "events-hunter", None) == os.path.realpath(d)


def test_resolve_returns_none_for_a_path_outside_every_root(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha")
    outside = tmp_path / "outside" / "secret"
    assert skills.resolve_skill_dir([os.path.realpath(root)], None, str(outside)) is None


# --- picking a file within the skill ---------------------------------------

def test_safe_target_accepts_a_file_inside_and_rejects_traversal(tmp_path):
    d = make_skill(tmp_path, "alpha", files={"notes.md": "hi"})
    d = str(os.path.realpath(d))
    assert skills.safe_target(d, "notes.md") == os.path.join(d, "notes.md")
    assert skills.safe_target(d, "../../etc/passwd") is None
    assert skills.safe_target(d, "SKILL.md/..") is None       # not a file


def test_safe_target_rejects_a_symlink_out_of_the_skill(tmp_path):
    d = make_skill(tmp_path, "alpha")
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    os.symlink(secret, d / "escape.md")
    assert skills.safe_target(str(os.path.realpath(d)), "escape.md") is None


# --- listing ----------------------------------------------------------------

def test_listing_marks_viewable_and_skips_hidden_and_git(tmp_path):
    d = make_skill(tmp_path, "alpha", files={
        "helper.py": "x", "logo.png": "binary", ".secret": "s",
        ".git/config": "g"})
    rels = {f["rel"]: f["viewable"] for f in skills.list_skill_files(str(os.path.realpath(d)))}
    assert rels["SKILL.md"] is True
    assert rels["helper.py"] is True
    assert rels["logo.png"] is False        # listed, not opened
    assert ".secret" not in rels
    assert not any(r.startswith(".git") for r in rels)


# --- reading ----------------------------------------------------------------

def test_read_text_returns_text_for_markdown_and_none_for_binary(tmp_path):
    d = make_skill(tmp_path, "alpha", files={"logo.png": "\x00\x01"})
    d = str(os.path.realpath(d))
    text, problem = skills.read_text(os.path.join(d, "SKILL.md"))
    assert "body" in text and problem is None
    text, problem = skills.read_text(os.path.join(d, "logo.png"))
    assert text is None and "not a text file" in problem


def test_is_markdown(tmp_path):
    assert skills.is_markdown("a/SKILL.md")
    assert not skills.is_markdown("a/helper.py")


# --- the route --------------------------------------------------------------

def test_skill_page_renders_the_manifest_and_lists_files(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha", identity="alpha",
               files={"helper.py": "print('hi')", "logo.png": "\x00"})
    page = skill_client(tmp_path, [root]).get(
        "/turns/skill?name=alpha").get_data(as_text=True)
    assert "body" in page                      # SKILL.md rendered
    assert "helper.py" in page and "logo.png" in page   # sidebar
    assert "/turns/skill?" in page and "sel=helper.py" in page  # viewable link


def test_skill_page_opens_a_chosen_file(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha", files={"helper.py": "MARKER_TEXT = 1"})
    page = skill_client(tmp_path, [root]).get(
        "/turns/skill?name=alpha&sel=helper.py").get_data(as_text=True)
    assert "MARKER_TEXT = 1" in page


def test_a_traversal_sel_is_refused(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha")
    (tmp_path / "secret.txt").write_text("token", encoding="utf-8")
    resp = skill_client(tmp_path, [root]).get(
        "/turns/skill?name=alpha&sel=../../secret.txt")
    assert resp.status_code == 404


def test_a_path_outside_every_root_is_refused(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha")
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    resp = skill_client(tmp_path, [root]).get(f"/turns/skill?path={secret}")
    assert resp.status_code == 404
    assert "token" not in resp.get_data(as_text=True)


def test_an_unknown_skill_is_a_404(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha")
    assert skill_client(tmp_path, [root]).get(
        "/turns/skill?name=nope").status_code == 404


def test_the_raw_view_shows_characters_not_rendered_markdown(tmp_path):
    root = tmp_path / "root"
    make_skill(root, "alpha")
    client = skill_client(tmp_path, [root])
    rendered = client.get("/turns/skill?name=alpha").get_data(as_text=True)
    raw = client.get("/turns/skill?name=alpha&raw=1").get_data(as_text=True)
    assert "<h1" in rendered            # markdown became a heading
    assert "<pre" in raw                # raw is the characters in a pre block


def test_skill_viewing_off_when_no_roots_configured(tmp_path):
    resp = skill_client(tmp_path, []).get("/turns/skill?name=alpha")
    assert resp.status_code == 404
    assert "not available" in resp.get_data(as_text=True)


# --- frontmatter ------------------------------------------------------------

def test_split_frontmatter_keeps_the_fences_and_returns_the_body():
    fm, body = skills.split_frontmatter(
        "---\nname: maps\nversion: 1.2.0\n---\n\n# Maps\n\nbody\n")
    assert fm == "---\nname: maps\nversion: 1.2.0\n---"
    assert body.lstrip().startswith("# Maps")


def test_split_frontmatter_is_none_when_there_is_none():
    fm, body = skills.split_frontmatter("# Maps\n\nno frontmatter here\n")
    assert fm is None and body.startswith("# Maps")


def test_frontmatter_is_shown_verbatim_not_as_a_heading(tmp_path):
    # The bug this fixes: CommonMark reads `name: maps` closed by `---` as a
    # setext heading, so the whole block rendered as one bold heading.
    root = tmp_path / "root"
    d = root / "maps"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: maps\nversion: 1.2.0\n---\n\n# Maps\n\nGeocoding.\n",
        encoding="utf-8")
    page = skill_client(tmp_path, [root]).get(
        "/turns/skill?name=maps").get_data(as_text=True)
    assert 'class="blob skill-frontmatter"' in page   # shown as text
    assert "version: 1.2.0" in page
    assert "<h1" in page and "Geocoding." in page      # body still markdown
    assert "<h2>name: maps" not in page                # not swallowed into one
