"""Skill provenance — where a skill on disk came from (the summary-box read).

The skill view (ADR 22) shows a skill's files. This adds the one thing those
files do not carry: whether the skill was bundled with hermes, installed from
the Skills Hub, created by hermes' own curator, configured by the user as an
external directory, or added by hand.

hermes records this in three sidecar files at the root of its runtime skills
directory (`$HERMES_HOME/skills`), never in the SKILL.md:

  `.bundled_manifest`   `name:hash` per line — skills seeded from the hermes repo
  `.hub/lock.json`      `{"installed": {name: {...}}}` — Skills-Hub installs
  `.usage.json`         `{name: {created_by, created_at, ...}}` — curator telemetry

These are hermes-internal formats, not a contract (ADR 23). Every read here is
best-effort: a missing, renamed or reformatted sidecar yields an "unknown"
origin and the page still renders. What we never do is guess — an origin is a
record we found, not an inference from a name or a path.

One honesty limit is hermes', not ours: hermes stamps `created_by: "agent"`
only on skills its background curator created on its own. A skill hermes wrote
because the user asked it to (a foreground `skill_manage` create) is left
unmarked, so here it is indistinguishable from a hand-authored skill and lands
in "user-added". The box says so rather than overclaim.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from plugins.turns import skills

# (stable slug for tests/styling, shown label). The slug never changes; the
# label may.
BUNDLED = ("bundled", "Bundled with hermes")
HUB = ("hub", "Hub / URL-installed")
AGENT = ("agent", "Agent-created")
EXTERNAL = ("external", "External dir (user-configured)")
USER = ("user", "User-added / manually authored")
UNKNOWN = ("unknown", "Unknown")

# Shown beside the "user-added" label: the bucket is wider than it looks,
# because hermes deliberately does not mark user-requested agent authorship.
_AGENT_CAVEAT = (
    "hermes records a skill as agent-created only when its curator made one on "
    "its own. A skill you asked hermes to write is left unmarked, so it appears "
    "here as user-added."
)
_UNKNOWN_NOTE = (
    "no provenance sidecars were found in this skill's root, so its origin "
    "cannot be determined."
)


@dataclass
class Provenance:
    """A skill's origin and the dates around it, ready for the summary box."""

    origin_key: str
    origin: str
    note: Optional[str] = None
    created: Optional[str] = None       # display string, UTC
    modified: Optional[str] = None      # display string, UTC (SKILL.md mtime)
    rows: List[Tuple[str, str]] = field(default_factory=list)  # extra facts


def classify(skill_dir: str, roots) -> Provenance:
    """Where `skill_dir` came from, read from the sidecars beside it.

    `roots` is the resolved skill-root allow list (the route already holds it).
    Never raises: any unreadable sidecar simply narrows what can be said.
    """
    real = skills._real(skill_dir)
    name = skills._identity(skill_dir) or os.path.basename(real)
    modified = _mtime(real)

    # External dirs are user-configured and carry no sidecars — the origin is
    # the configuration itself, and it is knowable without reading anything.
    for ext in skills._external_dirs():
        if skills._within(real, skills._real(ext)):
            return Provenance(*EXTERNAL, modified=modified)

    root = skills.containing_root(real, roots)
    if root is None:
        return Provenance(*UNKNOWN, note=_UNKNOWN_NOTE, modified=modified)

    bundled = _bundled_names(root)
    hub, hub_entries = _hub_installed(root)
    usage = _usage_record(root, name)
    have_sidecars = any(
        os.path.exists(p) for p in (
            os.path.join(root, ".bundled_manifest"),
            os.path.join(root, ".hub", "lock.json"),
            os.path.join(root, ".usage.json"),
        )
    )

    created = _fmt(usage.get("created_at")) if usage else None
    rows = _usage_rows(usage) if usage else []

    # Precedence mirrors hermes' own (tools/skill_usage.py): a hub install wins
    # over a bundled seed, and an explicit agent-created record over neither.
    if name in hub:
        key, label = HUB
        rows = _hub_rows(hub_entries.get(name) or {}) + rows
        note = None
    elif name in bundled:
        key, label, note = (*BUNDLED, None)
    elif usage and (usage.get("created_by") == "agent"
                    or usage.get("agent_created") is True):
        key, label, note = (*AGENT, None)
    elif have_sidecars:
        key, label, note = (*USER, _AGENT_CAVEAT)
    else:
        key, label, note = (*UNKNOWN, _UNKNOWN_NOTE)

    return Provenance(origin_key=key, origin=label, note=note,
                      created=created, modified=modified, rows=rows)


# --- reading the sidecars, all best-effort -------------------------------

def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _bundled_names(root: str) -> set:
    """Skill names seeded from the hermes repo — the `name` before each `:`."""
    names: set = set()
    try:
        with open(os.path.join(root, ".bundled_manifest"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    names.add(line.split(":", 1)[0])
    except OSError:
        pass
    return names


def _hub_installed(root: str) -> Tuple[set, dict]:
    """(names, entries) from `.hub/lock.json`'s `installed` map."""
    data = _read_json(os.path.join(root, ".hub", "lock.json"))
    installed = data.get("installed") if isinstance(data, dict) else None
    if isinstance(installed, dict):
        return set(installed.keys()), installed
    return set(), {}


def _usage_record(root: str, name: str) -> Optional[dict]:
    data = _read_json(os.path.join(root, ".usage.json"))
    if not isinstance(data, dict):
        return None
    # A `skills:` wrapper if a future format grows one, else the flat map.
    table = data.get("skills") if isinstance(data.get("skills"), dict) else data
    record = table.get(name)
    return record if isinstance(record, dict) else None


# --- the extra fact rows -------------------------------------------------

def _usage_rows(rec: dict) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    last_used = _fmt(rec.get("last_used_at"))
    if last_used:
        rows.append(("Last used", last_used))
    count = rec.get("use_count")
    if isinstance(count, int) and count > 0:
        rows.append(("Times used", str(count)))
    state = rec.get("state")
    if isinstance(state, str) and state and state != "active":
        rows.append(("State", state))
    if rec.get("pinned") is True:
        rows.append(("Pinned", "yes"))
    return rows


def _hub_rows(entry: dict) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    ident = entry.get("identifier") or entry.get("source")
    if isinstance(ident, str) and ident:
        rows.append(("Hub source", ident))
    version = entry.get("version")
    if version:
        rows.append(("Version", str(version)))
    return rows


# --- timestamps ----------------------------------------------------------

def _mtime(skill_dir: str) -> Optional[str]:
    """When the skill's SKILL.md was last written (its directory as a fallback)."""
    manifest = os.path.join(skill_dir, skills.MANIFEST)
    target = manifest if os.path.isfile(manifest) else skill_dir
    try:
        ts = os.path.getmtime(target)
    except OSError:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt(iso) -> Optional[str]:
    """An ISO-8601 timestamp as `YYYY-MM-DD HH:MM UTC`, or None if unparseable."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
