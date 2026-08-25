"""Reading a skill off disk for the skill view (ADR 22).

Everything here is confined to the configured skill roots — the standard
hermes skill directories plus the `skills.external_dirs` from hermes'
`config.yaml`. A path that comes from the log is only ever a *candidate*: it is
admitted only when its `realpath` (symlinks resolved first) falls inside a
root, so an untrusted payload cannot point the reader at an arbitrary file.
That containment is the whole security guarantee — see SECURITY.md.

The file list a skill page shows is built by scanning the skill's own
directory, not from anything the log said, which is why the sidebar adds no
read surface the SKILL.md view does not already have.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# YAML has no standard-library reader (unlike the TOML of hobserver.toml), so
# reading external_dirs from hermes' config.yaml needs a parser. It is a soft
# dependency: without it the external dirs are simply skipped and only the
# standard roots are used (ADR 22's degradation path).
try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised by the degradation test
    yaml = None

import hermes_paths

MANIFEST = "SKILL.md"

# Files a skill page will render. Everything else in a skill (images, wheels,
# compiled helpers) is listed but not opened — rendering it as text would be
# noise at best and a decode error at worst.
TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".sh", ".bash", ".zsh", ".fish",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".env",
    ".js", ".ts", ".jsx", ".tsx", ".css", ".html", ".xml", ".csv", ".sql",
})
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# Names never worth listing or descending into.
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules"})

# A skill directory can hold a lot; a listing is a navigation aid, not an
# archive index, so it stops well before it could bury the page.
_MAX_LISTING = 500
_MAX_TEXT_BYTES = 1_000_000
# How deep to hunt for a skill when only its name is known. external_dirs
# point at individual skill trees, so the manifest is never far down.
_FIND_MAXDEPTH = 4


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _within(path: str, root: str) -> bool:
    """Whether `path` is `root` or sits beneath it — both already realpath'd,
    compared on a separator boundary so `/a/skills-secret` is not read as
    inside `/a/skills`."""
    return path == root or path.startswith(root + os.sep)


def _external_dirs() -> list[str]:
    """`skills.external_dirs` from hermes' config.yaml, or [] if it cannot be
    read. Never raises: a missing parser, an absent file or malformed YAML all
    mean "no external dirs", not a broken page."""
    if yaml is None:
        return []
    cfg = os.path.join(hermes_paths.hermes_config_dir(), "config.yaml")
    try:
        with open(cfg, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    skills = data.get("skills")
    dirs = skills.get("external_dirs") if isinstance(skills, dict) else None
    if not isinstance(dirs, list):
        return []
    return [d for d in dirs if isinstance(d, str) and d]


def _standard_roots() -> list[str]:
    """The hermes skill directories derived from HERMES_HOME, mirroring the
    agent's own `hermes_constants` — the user skills dir, optional skills, and
    a bundled dir when a wrapper names one. Kept in step with the agent for the
    same reason hermes_paths mirrors its config-dir fallback."""
    home = hermes_paths.hermes_config_dir()
    roots = [os.path.join(home, "skills"),
             os.path.join(home, "optional-skills")]
    for env in ("HERMES_BUNDLED_SKILLS", "HERMES_OPTIONAL_SKILLS"):
        override = os.environ.get(env, "").strip()
        if override:
            roots.append(override)
    return roots


def skill_roots(settings=None) -> list[str]:
    """The allow-list of skill roots: realpath'd, de-duplicated, order kept.

    An explicit `skill_roots` in the tab's settings replaces the derivation
    entirely (the override ADR 22 keeps for an operator who would rather not
    have hobserver read config.yaml at all) — and an explicit empty list turns
    skill viewing off. Otherwise it is the standard hermes roots plus the
    external dirs.
    """
    if settings is not None and "skill_roots" in settings:
        raw = list(settings["skill_roots"] or [])
    else:
        raw = _standard_roots() + _external_dirs()
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        real = _real(entry)
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


def containing_root(path: str, roots) -> Optional[str]:
    """The root that holds `path`, or None. Resolves symlinks first, so a link
    that points out of a root is caught by the check rather than followed."""
    real = _real(path)
    for root in roots:
        if _within(real, root):
            return root
    return None


def _manifest_dir(start: str, root: str) -> Optional[str]:
    """Walk from `start` up to `root`, returning the nearest directory that
    holds a SKILL.md. Skills are not nested, so there is at most one."""
    cur = start if os.path.isdir(start) else os.path.dirname(start)
    while _within(cur, root):
        if os.path.isfile(os.path.join(cur, MANIFEST)):
            return cur
        if cur == root:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _child_of_root(start: str, root: str) -> str:
    """The directory immediately under `root` on the way to `start` — the best
    guess at a skill's own directory when no SKILL.md marks it."""
    base = start if os.path.isdir(start) else os.path.dirname(start)
    rel = os.path.relpath(base, root)
    if rel in (".", "") or rel.startswith(".."):
        return root
    return os.path.join(root, rel.split(os.sep)[0])


def _skill_dirs(root: str):
    """Directories under `root` (root included) that hold a SKILL.md, to a
    bounded depth — every skill's own directory, for a name lookup."""
    root = root.rstrip(os.sep)
    base_depth = root.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        if dirpath.count(os.sep) - base_depth >= _FIND_MAXDEPTH:
            dirnames[:] = []
        if MANIFEST in filenames:
            yield dirpath


def _identity(skill_dir: str) -> Optional[str]:
    """A skill's declared name, from the `name:` of its SKILL.md frontmatter.
    Read shallowly and forgivingly — a skill with no frontmatter just has no
    identity to match on, and falls back to its directory name."""
    try:
        with open(os.path.join(skill_dir, MANIFEST), encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("\n---", 3)
    block = head[3:end] if end != -1 else head[3:]
    match = re.search(r"(?m)^\s*name:\s*(.+?)\s*$", block)
    if not match:
        return None
    return match.group(1).strip().strip("'\"") or None


def _find_by_name(roots, name: str) -> Optional[str]:
    """A skill directory matched to `name`, by one of three keys in order of
    reliability: its declared identity, its path relative to the root it sits
    under, or its directory basename.

    Identity wins so a namespaced skill is found even where its directory is
    called `src` or `skill`. The relative path is what hermes writes when a
    skill lives under a category dir — `skill_view(name='finance/crypto-analysis')`
    for `<root>/finance/crypto-analysis`, whose declared name is only
    `crypto-analysis` — and it is preferred over the basename so that form
    reaches the skill under `finance/` rather than a same-named skill
    elsewhere."""
    by_relpath = None
    by_basename = None
    for root in roots:
        for skill_dir in _skill_dirs(root):
            if _identity(skill_dir) == name:
                return skill_dir
            if by_relpath is None and os.path.relpath(skill_dir, root) == name:
                by_relpath = skill_dir
            if by_basename is None and os.path.basename(skill_dir) == name:
                by_basename = skill_dir
    return by_relpath or by_basename


def _dir_for_contained_path(cand: str, roots) -> Optional[str]:
    """The skill directory holding `cand`, or None if it is inside no root.
    `cand` names a skill's tree (or a file within it) directly, so it resolves
    by containment: its manifest dir, or the child of the root on the way to
    it."""
    root = containing_root(cand, roots)
    if root is None:
        return None
    real = _real(cand)
    return _manifest_dir(real, root) or _child_of_root(real, root)


def resolve_skill_dir(roots, name: Optional[str],
                      path: Optional[str]) -> Optional[str]:
    """The skill directory a scope points at, or None if it is not inside any
    root.

    A contained `path` is the reliable key — it names the skill's tree
    directly. An absolute `name` is really a path too: the model sometimes puts
    the skill's location where its name belongs, so it is resolved by
    containment as well, and thus never lands on a same-named skill elsewhere
    that a bare-name scan would match by basename. A relative `name` is the
    fallback, resolved by scanning the roots."""
    for cand in (path, name if name and os.path.isabs(name) else None):
        if cand:
            found = _dir_for_contained_path(cand, roots)
            if found is not None:
                return found
    if name and not os.path.isabs(name):
        return _find_by_name(roots, name)
    return None


def safe_target(skill_dir: str, rel: str) -> Optional[str]:
    """The absolute path of a file chosen from the sidebar, or None if `rel`
    escapes the skill directory. Symlinks are resolved before the check, so a
    link inside the skill cannot reach out of it."""
    candidate = _real(os.path.join(skill_dir, rel))
    if _within(candidate, skill_dir) and os.path.isfile(candidate):
        return candidate
    return None


def is_markdown(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MARKDOWN_SUFFIXES


# A leading YAML frontmatter block: `---`, content, a closing `---` line.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)",
                          re.DOTALL)


def split_frontmatter(text: str):
    """`(frontmatter, body)` if `text` opens with a `--- … ---` block, else
    `(None, text)`. The fences are kept on the frontmatter so it can be shown
    verbatim: CommonMark reads a `key:` line closed by `---` as a setext
    heading, so a skill's whole frontmatter renders as one bold heading unless
    it is held out of the markdown and shown as the text it is."""
    match = _FRONTMATTER.match(text)
    if not match:
        return None, text
    return text[:match.end()].rstrip("\n"), text[match.end():]


def _viewable(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TEXT_SUFFIXES


def list_skill_files(skill_dir: str) -> list[dict]:
    """Every file in the skill, as `{rel, viewable}`, for the sidebar. Built by
    scanning the directory on disk — never from the log — and confined to it,
    so it opens no read surface the manifest view does not already have."""
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            # A symlink out of the skill is not part of it; skip rather than
            # follow, the same rule the served file is held to.
            if not _within(_real(full), skill_dir):
                continue
            out.append({"rel": os.path.relpath(full, skill_dir),
                        "viewable": _viewable(full)})
            if len(out) >= _MAX_LISTING:
                return out
    return out


def read_text(path: str):
    """A skill file's text for rendering, capped, with a note when the cap bit.
    Returns (text, problem); text is None when the file is not one this view
    opens (a binary or an unknown type)."""
    if not _viewable(path):
        return None, "not a text file — listed but not shown"
    try:
        with open(path, "rb") as fh:
            blob = fh.read(_MAX_TEXT_BYTES + 1)
    except OSError as exc:
        return None, f"could not be read ({exc.__class__.__name__})"
    problem = None
    if len(blob) > _MAX_TEXT_BYTES:
        blob = blob[:_MAX_TEXT_BYTES]
        problem = f"shown to the first {_MAX_TEXT_BYTES:,} bytes"
    return blob.decode("utf-8", "replace"), problem
