"""What this app's known scopes show, scope by scope (ADR 7).

The vocabulary is in `scope_spec`; this file is only the data. Each entry is
keyed by the hermes tool's own scope name, checked against the tool's
signature in the hermes source rather than against shapes seen in the log —
`$HERMES_SOURCE/tools/`, and `$HERMES_SOURCE/plugins/memory/mem0/` for the
mem0 tools. `$HERMES_SOURCE` is wherever hermes-agent is checked out; it is
notation for a reader, not a variable anything here resolves, and it is not
`$HERMES_HOME` (that is hermes' config directory, which this app does read).

A scope with no entry here renders its payload through the generic fallback.
That is the right destination for a tool this app has never heard of, and a
spec module named in `hobserver.toml` can override or extend this table
without forking — see docs/design/design-principles.md.

Specs are alphabetical, and so is the SCOPES table at the foot, so a scope can
be looked up rather than hunted for. Nothing depends on the order — a spec is
reached by key — so where two belong together, adjacency wins over the
alphabet and the exception is marked.

`docs/design/span-rendering.md` narrates what each of these shows and why.
"""

from scope_spec import (Alt, Diff, Each, Field, Full, Items, Link,
                                        Row, Scope, const, first, item, joined,
                                        mapped)


def _view_skill_label(_name):
    """The 'view skill' link's text is fixed; the source it reads
    (`skill_name`) is only there to gate it — a skill scope that names no skill
    resolves to nothing and gets no link (ADR 22)."""
    return "view skill"


def tilde(path):
    """Collapse this host's home-dir prefix to ~ for display. Shared with the
    template filter of the same name — a spec transforms the text while the
    title attribute keeps the path in full."""
    import os

    home = os.path.expanduser("~")
    if path and home != "~":
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home):]
    return path


DELEGATE_TASK = Scope(rows=[
    Row([Field(first("delegate_goals"), clip="wide",
               title=joined("delegate_goals"), more="delegate_tasks")],
        layer="summary"),
    Each("delegate_tasks", [
        Row([Field(item("goal"), clip="wide-wrap")],
            layer="detail", cls="task-goal"),
        Row([Field(item("context"), clip="wide-wrap")],
            layer="detail", cls="ctx"),
    ]),
])

EXECUTE_CODE = Scope(rows=[
    Row([Field("code_first_line", font="mono", title="code")],
        layer="summary"),
    Row([Field("code", font="mono", clip="wrap")], layer="detail"),
])

# read_file and write_file are the file tools carrying a plain path; the other
# two, patch and search_files, have specs of their own. Checked against the
# tool names registered in $HERMES_SOURCE/tools/file_tools.py.
FILE_PATH = Scope(rows=[
    Row([Field("path", clip="tail", transform=tilde)]),
])

# The in-prompt stores (MEMORY.md / USER.md), not mem0. Most writes are
# entries being shortened to fit the char budget, so a batch reads as the
# list of edits it is. The end payload's stats and current_entries are
# outcome rows and stay with the error row in the template.
#
# The − side is `old_shown`, not the logged `old_text`: the tool matches an
# entry by a fragment of it, so the fragment is what the log holds and the
# whole entry is what a reader is trying to see. `resolve_memory_entries`
# recovers it from a store listing elsewhere in the turn where it can, and
# the `.prov` row below says so — the same shape mem0's recovered text uses,
# for the same reason (design principle 2). Where nothing was recovered the
# − side falls back to the fragment and the note says why.
MEMORY = Scope(rows=[
    Row([Field("memory_action", deco="tag"),
         Field("memory_target", deco="cat", prefix="· ")]),
    Row([Field(first(mapped("memory_ops", "text")), clip="wide",
               title=joined(mapped("memory_ops", "text")),
               more="memory_ops")], layer="summary"),
    # One pass, so each op's label stays with its own − / + pair. A lone op
    # is already named by the mode tag above, hence `when_many`.
    Each("memory_ops", [
        Row([Field(item("action"), deco="action")],
            layer="detail", when_many=True),
        Diff(item("old_shown"), item("content")),
        Row([Field(item("old_entry_note"), deco="plain",
                   title=const(
                       "The log records only the fragment the tool matched "
                       "on, never the entry it resolved to: `old_text` is a "
                       "short unique substring, matched by containment. This "
                       "app recovers the entry by matching that fragment "
                       "against the whole store, which a write rejected for "
                       "the char budget hands back — so it is the entry as "
                       "that listing showed it, not something the tool "
                       "reported having replaced. A listing is only used "
                       "until a write lands, since a successful write says "
                       "what it cost but never what the store now says."))],
            layer="detail", cls="prov"),
    ]),
])

# Two modes: replace edits one path and shows the two sides; patch applies a
# V4A text naming its own files. The Alt is the either/or between them.
PATCH = Scope(rows=[
    Row([Field("patch_mode", deco="tag"),
         Alt([Field("path", clip="tail", transform=tilde),
              Field(first("patch_paths"), clip="tail", transform=tilde,
                    title=joined("patch_paths"), more="patch_paths")])]),
    Diff("patch_old_string", "patch_new_string"),
    Row([Field("patch_text", font="mono", clip="wrap")], layer="detail"),
])

SEARCH_FILES = Scope(rows=[
    Row([Field("search_pattern", font="mono")]),
    Row([Field("file_glob", clip="wide")]),
    Row([Field("path", clip="tail", transform=tilde)]),
])

# Four modes, named by a tag; the per-mode counts the end payload reports sit
# on detail-only rows, each with its own tooltip.
SESSION_SEARCH = Scope(rows=[
    Row([Field("session_search_mode", deco="tag"),
         Field("session_search_summary", font="mono", clip="wrap")]),
    Each("session_search_stats", [
        Row([Field([item("label"), item("value")], deco="plain",
                   title=item("tooltip"))], layer="detail"),
    ]),
])

# skill_view and skill_manage share a shape. The middots are conditional on
# what is beside them, which is why they ride `sep_if` on the following
# field rather than being cells of their own.
SKILL = Scope(rows=[
    Row([Field("skill_action", deco="action"),
         Field("skill_category", deco="cat"),
         Field("skill_name", font="mono", sep_if="skill_category"),
         Field("skill_file_path", clip="tail", sep_if="skill_name"),
         Field("skill_absorbed_into", font="mono",
               label="→ absorbed into")]),
    # a skill_manage patch replaces text; it never carries a V4A patch the
    # way the file tools' patch scope does, so it renders as a replace pair
    Diff("skill_old_string", "skill_new_string"),
    # To the skill on disk (ADR 22): name and file_path are what the route
    # resolves against the configured roots. Detail-only, like the pair above.
    Link(endpoint="turns.skill",
         params={"name": "skill_name", "path": "skill_file_path"},
         text="skill_name", transform=_view_skill_label,
         title=const("Open this skill's SKILL.md and files."),
         layer="detail"),
])

# The command runs in monospace and keeps its line breaks in detail mode, so
# a heredoc or a multi-command script stays readable.
TERMINAL = Scope(rows=[
    Row([Field("command", font="mono", clip="wrap"),
         Field("workdir", prefix="in ", transform=tilde)]),
])

# a todo call without `todos` is a read of the current list and shows nothing
TODO = Scope(rows=[Items("todo_contents")])

VISION_ANALYZE = Scope(rows=[
    Row([Field("vision_image_url", clip="tail", transform=tilde)]),
    Row([Field("vision_question", font="mono", clip="wrap")], layer="detail"),
])

WEB_EXTRACT = Scope(rows=[Items("web_extract_urls")])

WEB_SEARCH = Scope(rows=[
    Row([Field("search_query", font="mono", clip="wrap")]),
])

# --- the one that keeps hand-written Jinja ---------------------------
#
# `render=` is the escape hatch: instead of declaring rows, the scope names a
# macro in one of templates/turns/_scope_*.html that renders it by hand,
# which the turn page dispatches to. Set render or rows, never both. The only names
# that work are the ones that file defines, listed in
# scope_spec.RENDER_MACROS — anything else is rejected at load rather than
# quietly falling through to the payload dump. See `Scope` in scope_spec.py
# for the full contract.
#
# Reach for it only when the vocabulary genuinely cannot say what a scope
# needs. llm is the last one: its token tree runs a separator state machine,
# tracking whether anything precedes a row so one with nothing to its left
# takes no leading dot, and that is not a shape the vocabulary should learn.
#
# ADR 7 shipped three. The two mem0 scopes were the same missing thing twice
# — a link and a published accessor — and ADR 9 put both in the vocabulary
# rather than leaving them privileged for being in this tree. A second
# exception now would mean the same question is worth asking again; a test
# fails when this set changes, so it cannot drift by accident.

# The two full values (ADR 12) are declared here rather than in the macro
# that draws their icons: the route serving the page reads this list, so a
# key exists in both places or in neither.
#
# Both are on the llm scope by *category*, so every model call has them
# whatever it was for — a turn's own prompt, a compaction, a subagent's
# delegated call. That is the point of them: before this, hermes' background
# work was the one kind of call whose prompt appeared nowhere on the page,
# since the turn header shows the turn's user message and an auxiliary call
# does not have one.
LLM = Scope(render="llm", fulls=[
    # `prompt` throughout — the key, the URL segment, the heading, and the row
    # whose icon opens it. That is what the word means everywhere else in the
    # payload too (`usage.prompt_tokens`, `api_specific.prompt_cache_key`):
    # the whole of what a call was sent, which is what this page shows. Only
    # the *source* keeps the other word, because `annotated_request` is what
    # the relay called the thing it wrote.
    Full(key="prompt", source="llm_request_messages", render="sections",
         title=const("Prompt"),
         note=const("Every message of this call's annotated_request. The "
                    "labels are this app's, and so is one thing about the "
                    "order: each tool result is shown under the call it "
                    "answers, where the wire sends all the calls and then "
                    "all the results. Everything inside a box is what went "
                    "on the wire.")),
    Full(key="response", source="llm_response_text", render="markdown",
         title=const("Response"),
         note=const("The assistant message from this call's end payload, "
                    "verbatim.")),
])


# An llm span is named for the provider that answered it ("anthropic"), not
# for what it is, so it is the one scope identified by category.
SCOPES_BY_CATEGORY = {"llm": LLM}

# Alphabetical, so a scope is looked up rather than hunted for. Lookup is by
# key, so the order is for readers only.
#
# One exception, marked below: write_file sits out of order beside read_file
# because they are one spec, and splitting the pair would leave each looking
# like a scope of its own. skill_manage/skill_view and the mem0 writes share a
# spec too, but alphabetical order already puts those together.
SCOPES = {
    "delegate_task": DELEGATE_TASK,
    "execute_code": EXECUTE_CODE,
    "memory": MEMORY,
    "patch": PATCH,
    "read_file": FILE_PATH,
    "write_file": FILE_PATH,   # out of order: one spec with read_file
    "search_files": SEARCH_FILES,
    "session_search": SESSION_SEARCH,
    "skill_manage": SKILL,
    "skill_view": SKILL,
    "terminal": TERMINAL,
    "todo": TODO,
    "vision_analyze": VISION_ANALYZE,
    "web_extract": WEB_EXTRACT,
    "web_search": WEB_SEARCH,
    # The hand-written ones, kept apart because what they have in common is
    # that they are not declared here at all. mem0's writes stay in lifecycle
    # order — they are one spec, and add/update/delete reads as the sequence
    # it is.
}
