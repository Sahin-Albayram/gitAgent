"""Git-backed tool operations for GitAgent.

Each function here is exposed as a tool call to the local Ollama model.
Git itself is the substrate: branches are real `git branch`es that actually
diverge (each branch's own commits carry its `branches/<name>/MEMORY.md`),
so `close_branch` performs a real git merge, not a no-op. By default that
merge is squashed (see SQUASH_ON_CLOSE) so a branch's per-note bookkeeping
commits don't bury the main line's history; the note-by-note detail stays
reachable on the branch ref, which is never deleted.

STATE_TRACKER.md only ever gets a row for a top-level branch (one opened
straight off MAIN_BRANCH) - it's read/written exclusively on MAIN_BRANCH, so
it stays authoritative and stays small regardless of how much nested work
happens underneath a given side branch. A nested branch instead gets logged
into its *root* branch's own MEMORY.md, under a `## Sub-Branches` section -
one line per descendant, at any depth, updated on open and again on close.
That keeps the detail one hop away (the root's MEMORY.md) instead of
flooding STATE_TRACKER.md with a row per branch ever opened.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ._git import GitAgentError, run_git

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
BRANCHES_DIR = WORKSPACE_ROOT / "branches"
ARCHIVED_BRANCHES_DIR = BRANCHES_DIR / "archived"
STATE_TRACKER_PATH = WORKSPACE_ROOT / "STATE_TRACKER.md"

MAIN_BRANCH = "main"

OLLAMA_HOST = os.environ.get("GITAGENT_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("GITAGENT_OLLAMA_MODEL", "llama3.1:8b")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# How close_branch brings a finished branch's content into its base.
#
# Squashing keeps the base branch's history readable: a branch's per-note
# "Update branch: X" commits are bookkeeping, and replaying them into the main
# line buries the commits you actually care about. Squashing collapses them
# into one commit while the full note-by-note history stays reachable on the
# branch ref itself (which is never deleted) and, in prose, in the archived
# MEMORY.md. Set GITAGENT_SQUASH_ON_CLOSE=0, or pass --no-squash, for a
# traditional --no-ff merge commit instead.
SQUASH_ON_CLOSE = _env_flag("GITAGENT_SQUASH_ON_CLOSE", True)

SUMMARY_PROMPT = """You are compressing a project branch's working notes into a \
single changelog entry for a persistent state tracker.

Summarize the branch memory below in 2-3 plain-text sentences covering: what \
changed, the key decisions made, and the outcome. Do not use markdown, \
headers, bullet points, or the "|" character - this goes into a table cell. \
Respond with only the summary sentences themselves - no preamble like "Here \
is a summary", no closing remarks.

Branch memory:
{memory}
"""

BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

MEMORY_TEMPLATE = """# Branch Memory: {name}

## Branched From
{base}

## Goal
{description}

## Status
Active

## Decisions Log

## Sub-Branches

## Open Questions
"""


def _run_git(*args: str) -> str:
    return run_git(WORKSPACE_ROOT, *args)


def _find_line_index(lines: list[str], predicate, description: str) -> int:
    """Like next(i for i, line in enumerate(lines) if predicate(line)), but
    raises GitAgentError instead of StopIteration when nothing matches - a
    hand-edited MEMORY.md/STATE_TRACKER.md missing an expected section
    should fail with a clear message, not a raw traceback."""
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    raise GitAgentError(f"malformed memory file: could not find {description}")


def _branch_exists(name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=WORKSPACE_ROOT,
    )
    return result.returncode == 0


def _is_gitagent_path(path: str) -> bool:
    return path == "STATE_TRACKER.md" or path.startswith("branches/")


def _ensure_clean_worktree() -> None:
    """Refuse to switch branches over uncommitted changes to GitAgent's own
    tracked files (STATE_TRACKER.md, branches/).

    Only those paths matter - an in-progress edit to some unrelated file
    elsewhere in the workspace shouldn't block a branch operation. Untracked
    files are fine either way - git itself already refuses a checkout that
    would actually clobber one, so there's no need to be stricter here.
    """
    status = _run_git("status", "--porcelain")
    dirty = []
    for line in status.splitlines():
        if line.startswith("??"):
            continue
        paths = line[3:].split(" -> ")  # rename lines are "old -> new"
        if any(_is_gitagent_path(p) for p in paths):
            dirty.append(line)
    if dirty:
        raise GitAgentError(
            "STATE_TRACKER.md or branches/ has uncommitted changes - commit "
            "or stash them before running a GitAgent tool that switches branches"
        )


def _read_file_at_ref(ref: str, path: str) -> str:
    """Read a file's contents as of `ref`, without touching the worktree."""
    return _run_git("show", f"{ref}:{path}")


def _branched_from(name: str) -> str:
    """Read a branch's recorded parent from its own MEMORY.md.

    Works via `git show`, so it doesn't require checking `name` out, and
    works just as well after `name` has been closed (its own branch ref and
    history stick around even once merged elsewhere).
    """
    memory_text = _read_file_at_ref(name, f"branches/{name}/MEMORY.md")
    lines = memory_text.splitlines()
    start = _find_line_index(
        lines,
        lambda line: line.strip() == "## Branched From",
        f"'## Branched From' section in {name}'s MEMORY.md",
    )
    return lines[start + 1].strip()


def _root_branch(name: str) -> str:
    """Walk `## Branched From` up until it hits a branch opened off MAIN_BRANCH."""
    current = name
    while _branched_from(current) != MAIN_BRANCH:
        current = _branched_from(current)
    return current


def _next_branch_id(lines: list[str]) -> str:
    ids = []
    for line in lines:
        match = re.match(r"\|\s*Branch-(\d+)\s*\|", line)
        if match:
            ids.append(int(match.group(1)))
    return f"Branch-{max(ids, default=0) + 1:03d}"


def _iter_table_rows(lines: list[str]):
    """Yield (line_index, cells) for each STATE_TRACKER.md branch row,
    skipping the header and separator rows. Shared by status updates and
    `list_branches`."""
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] in ("Branch ID", "---"):
            continue
        yield i, cells


def _insert_state_tracker_row(branch_id: str, name: str, description: str) -> None:
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()

    header_idx = _find_line_index(
        lines,
        lambda line: line.startswith("| Branch ID"),
        "'| Branch ID' table header in STATE_TRACKER.md",
    )
    row_idx = header_idx + 2  # skip header and separator row
    while row_idx < len(lines) and lines[row_idx].startswith("|"):
        row_idx += 1

    row = f"| {branch_id} | {name} | {description} | Active | |"
    lines.insert(row_idx, row)

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _section_end(lines: list[str], header: str) -> int:
    start = _find_line_index(
        lines, lambda line: line.strip() == header, f"{header!r} section"
    )
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return end


def _section_body(lines: list[str], header: str) -> list[str]:
    """Return the non-empty lines within a `## <header>` section."""
    start = _find_line_index(
        lines, lambda line: line.strip() == header, f"{header!r} section"
    )
    end = _section_end(lines, header)
    return [line for line in lines[start + 1 : end] if line.strip()]


def _format_dry_run(action: str, steps: list[str]) -> str:
    lines = [f"[dry run] {action}"] + [f"  - {step}" for step in steps]
    return "\n".join(lines)


def _append_bullet(memory_path: Path, header: str, bullet: str) -> None:
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    insert_idx = _section_end(lines, header)
    while insert_idx > 0 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1
    lines.insert(insert_idx, bullet)
    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_memory_status(memory_path: Path, status: str) -> None:
    """Overwrite a MEMORY.md's own `## Status` value in place."""
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    header_idx = _find_line_index(
        lines, lambda line: line.strip() == "## Status", f"'## Status' section in {memory_path}"
    )
    value_idx = header_idx + 1
    while value_idx < len(lines) and lines[value_idx].strip() == "":
        value_idx += 1
    if value_idx < len(lines) and not lines[value_idx].startswith("## "):
        lines[value_idx] = status
    else:
        lines.insert(header_idx + 1, status)
    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log_sub_branch_open(root: str, name: str, description: str) -> None:
    memory_path = BRANCHES_DIR / root / "MEMORY.md"
    _append_bullet(memory_path, "## Sub-Branches", f"- **{name}** — Active — {description}")
    _run_git("add", str(memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Log open of {name} under {root}")


# The two ways a branch can finish, and the verb each one commits under.
FINISH_VERBS = {"Completed": "Close", "Abandoned": "Abandon"}


def _log_sub_branch_finish(root: str, name: str, status: str, summary: str) -> None:
    memory_path = BRANCHES_DIR / root / "MEMORY.md"
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    prefix = f"- **{name}**"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"- **{name}** — {status} — {summary}"
            break
    else:
        raise GitAgentError(f"no Sub-Branches entry for {name} under {root}")
    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run_git("add", str(memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Log {FINISH_VERBS[status].lower()} of {name} under {root}")


def open_branch(
    name: str, description: str, base: str = MAIN_BRANCH, dry_run: bool = False
) -> Path | str:
    """Create a git branch off `base` and its scoped MEMORY.md.

    `base` can be MAIN_BRANCH (the default - a normal top-level side branch)
    or the name of another open branch, to nest work under it. The branch
    actually diverges: its MEMORY.md is only ever committed on its own
    branch.

    A top-level branch gets an Active row in STATE_TRACKER.md, on
    MAIN_BRANCH. A nested branch gets no row at all - instead it's logged
    as an Active line under its root branch's own `## Sub-Branches` section
    (the root being whichever ancestor was itself opened off MAIN_BRANCH),
    so STATE_TRACKER.md stays one row per side branch no matter how much
    nesting happens underneath it. Ends checked out on the new branch,
    ready to work.

    If `dry_run` is True, does all the same validation but no mutation -
    no branch is created, no file is written - and returns a human-readable
    preview string instead of the MEMORY.md path. This is the guard against
    the local Ollama model driving this tool via the tool-calling loop with
    no human confirming each call.
    """
    if not BRANCH_NAME_RE.match(name):
        raise GitAgentError(f"invalid branch name: {name!r}")
    if _branch_exists(name):
        raise GitAgentError(f"branch already exists: {name}")
    if not _branch_exists(base):
        raise GitAgentError(f"base branch does not exist: {base}")

    _ensure_clean_worktree()

    if dry_run:
        if base == MAIN_BRANCH:
            tracker_text = _read_file_at_ref(MAIN_BRANCH, "STATE_TRACKER.md")
            branch_id = _next_branch_id(tracker_text.splitlines())
            steps = [f"insert STATE_TRACKER.md row {branch_id} for '{name}' (Active)"]
        else:
            root = _root_branch(base)
            steps = [f"log open of '{name}' under root branch '{root}' (## Sub-Branches)"]
        steps += [
            f"create branch '{name}' from '{base}'",
            f"write branches/{name}/MEMORY.md",
        ]
        return _format_dry_run(f"open_branch({name!r}, base={base!r})", steps)

    if base == MAIN_BRANCH:
        _run_git("checkout", MAIN_BRANCH)
        lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()
        branch_id = _next_branch_id(lines)
        _insert_state_tracker_row(branch_id, name, description)
        _run_git("add", str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)))
        _run_git("commit", "-m", f"Open branch: {name}")
    else:
        root = _root_branch(base)
        _run_git("checkout", root)
        _log_sub_branch_open(root, name, description)

    _run_git("checkout", "-b", name, base)
    branch_dir = BRANCHES_DIR / name
    branch_dir.mkdir(parents=True, exist_ok=True)
    memory_path = branch_dir / "MEMORY.md"
    memory_path.write_text(
        MEMORY_TEMPLATE.format(name=name, base=base, description=description),
        encoding="utf-8",
    )
    _run_git("add", str(memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Open branch: {name}")

    return memory_path


def update_branch(name: str, note: str, dry_run: bool = False) -> Path | str:
    """Append a timestamped note to a branch's MEMORY.md - the commit equivalent.

    Checks out `name` (from wherever the worktree currently is) and commits
    the note there, since MEMORY.md only exists on that branch's own history.
    Ends checked out on `name`.

    If `dry_run` is True, validates but doesn't check out `name` or write
    anything, returning a preview string instead of the MEMORY.md path.
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    _ensure_clean_worktree()

    memory_rel_path = f"branches/{name}/MEMORY.md"
    try:
        _read_file_at_ref(name, memory_rel_path)
    except GitAgentError:
        raise GitAgentError(f"no MEMORY.md found for branch: {name}") from None

    if dry_run:
        steps = [f"checkout '{name}'", f"append note to {memory_rel_path}'s ## Decisions Log"]
        return _format_dry_run(f"update_branch({name!r})", steps)

    _run_git("checkout", name)
    memory_path = BRANCHES_DIR / name / "MEMORY.md"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _append_bullet(memory_path, "## Decisions Log", f"- [{timestamp}] {note}")

    _run_git("add", str(memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Update branch: {name}")

    return memory_path


def _summarize_with_ollama(memory_text: str) -> str:
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": SUMMARY_PROMPT.format(memory=memory_text),
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GitAgentError(f"failed to reach Ollama at {OLLAMA_HOST}: {exc}") from exc

    summary = body["response"].strip()
    return summary.replace("|", "/").replace("\n", " ")


def _update_state_tracker_status(name: str, status: str, outcome: str) -> None:
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()

    for i, cells in _iter_table_rows(lines):
        if cells[1] == name:
            cells[3] = status
            cells[4] = outcome
            lines[i] = "| " + " | ".join(cells) + " |"
            break
    else:
        raise GitAgentError(f"no STATE_TRACKER.md row found for branch: {name}")

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class BranchStatus:
    id: str
    name: str
    description: str
    status: str
    outcome: str
    sub_branches: list[str] = field(default_factory=list)


def _read_sub_branches(name: str) -> list[str]:
    memory_text = _read_file_at_ref(name, f"branches/{name}/MEMORY.md")
    return _section_body(memory_text.splitlines(), "## Sub-Branches")


def list_branches() -> list[BranchStatus]:
    """Read STATE_TRACKER.md's branch table, with each Active row's own
    `## Sub-Branches` lines from that branch's own MEMORY.md attached -
    read via `git show` (see `_read_file_at_ref`) so it works regardless of
    which branch is currently checked out.

    Not every STATE_TRACKER.md row necessarily corresponds to an actual git
    branch - some are hand-authored higher-level entries (e.g. an umbrella
    row for a whole initiative) that predate a row always meaning "there's a
    real branch of this name". Sub-branches just come back empty for those
    rather than raising.
    """
    tracker_text = _read_file_at_ref(MAIN_BRANCH, "STATE_TRACKER.md")
    lines = tracker_text.splitlines()
    branches = []
    for _, cells in _iter_table_rows(lines):
        branch_id, name, description, status, outcome = cells
        sub_branches = []
        if status == "Active":
            try:
                sub_branches = _read_sub_branches(name)
            except GitAgentError:
                pass
        branches.append(BranchStatus(branch_id, name, description, status, outcome, sub_branches))
    return branches


def _is_archived(base: str, name: str) -> bool:
    """Whether `name`'s memory has already been archived on `base`."""
    try:
        _read_file_at_ref(base, f"branches/archived/{name}/MEMORY.md")
    except GitAgentError:
        return False
    return True


def _ensure_not_already_archived(base: str, name: str) -> None:
    if _is_archived(base, name):
        raise GitAgentError(
            f"branch has already been finished: {name} - its memory is at "
            f"branches/archived/{name}/MEMORY.md on {base}"
        )


def _archive_memory(name: str, status: str) -> Path:
    """Stamp the archived MEMORY.md's own `## Status` and commit the archive.

    The caller must already have placed branches/archived/<name>/MEMORY.md on
    the base branch: `close_branch` gets it there with `git mv` after its
    merge, `abandon_branch` writes the branch's memory text straight there
    since it has no merge to carry the file over.
    """
    archived_memory_path = ARCHIVED_BRANCHES_DIR / name / "MEMORY.md"
    _set_memory_status(archived_memory_path, status)
    _run_git("add", str(archived_memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Archive branch: {name}")
    return archived_memory_path


def _record_branch_outcome(name: str, base: str, status: str, outcome: str) -> None:
    """Record a finished branch's status and outcome in whichever memory owns
    it: STATE_TRACKER.md on MAIN_BRANCH for a top-level branch, or its root
    branch's `## Sub-Branches` line for a nested one. Ends back on `base`.
    """
    verb = FINISH_VERBS[status]
    if base == MAIN_BRANCH:
        _update_state_tracker_status(name, status, outcome)
        _run_git("add", str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)))
        _run_git("commit", "-m", f"{verb} branch: {name}")
    else:
        root = _root_branch(base)
        _run_git("checkout", root)
        _log_sub_branch_finish(root, name, status, outcome)
        if root != base:
            _run_git("checkout", base)


def _try_index(name: str) -> None:
    """Add a just-archived branch to the semantic search index, best-effort.

    Deliberately swallows every failure: the index is a derived, rebuildable
    cache (see search.py), so a missing optional dependency or an unreachable
    Ollama must never fail an otherwise-successful close/abandon and leave the
    branch half-finished. `gitagent reindex` recovers whatever was skipped.

    Imported lazily because chromadb is an optional extra - a bare install has
    to keep working.
    """
    try:
        from .search import index_branch

        index_branch(name)
    except Exception as exc:  # noqa: BLE001 - cache update must never be fatal
        print(f"warning: could not index {name} for search: {exc}", file=sys.stderr)


def close_branch(name: str, dry_run: bool = False, squash: bool | None = None) -> Path | str:
    """Summarize, merge, and archive a branch.

    Summarizes the branch's MEMORY.md via the local Ollama model and brings
    the branch into its recorded base (MAIN_BRANCH for a top-level branch,
    or the parent branch it was nested under - see `open_branch`), then
    moves (never deletes) the MEMORY.md into branches/archived/<name>/ on
    the base branch so the raw log stays retrievable.

    `squash` controls how the content lands, defaulting to SQUASH_ON_CLOSE:
      True  - `git merge --squash`, collapsing the branch's per-note commits
              into one, so the base branch's log stays readable.
      False - a traditional `git merge --no-ff` merge commit, preserving each
              "Update branch" commit in the base's history.
    Either way nothing is lost: the branch ref keeps its full commit-by-commit
    history, and the notes themselves live on in the archived MEMORY.md. Note
    that `--squash` does not *record* a merge, so a squashed branch won't show
    up in `git branch --merged` even though its content is present.

    If `name` was top-level, its STATE_TRACKER.md row is marked Completed
    with that summary, on MAIN_BRANCH. If `name` was nested, STATE_TRACKER.md
    is untouched - instead its root branch's `## Sub-Branches` line for
    `name` is updated to Completed with that summary. Either way, ends
    checked out back on `base`, so the archived MEMORY.md this returns is
    actually visible in the working tree.

    If `dry_run` is True, skips the Ollama call too (so this works even
    without Ollama running) and returns a preview string instead of doing
    the merge/archive/status-update.
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    squash = SQUASH_ON_CLOSE if squash is None else squash

    _ensure_clean_worktree()

    base = _branched_from(name)
    _ensure_not_already_archived(base, name)

    if dry_run:
        how = "squash-merge" if squash else "merge (--no-ff)"
        steps = [
            "generate a summary of its MEMORY.md via Ollama",
            f"{how} '{name}' into '{base}'",
            f"archive branches/{name}/MEMORY.md -> branches/archived/{name}/MEMORY.md "
            "(status -> Completed)",
        ]
        if base == MAIN_BRANCH:
            steps.append(f"mark '{name}' Completed in STATE_TRACKER.md")
        else:
            steps.append(f"mark '{name}' Completed under '{_root_branch(base)}'s ## Sub-Branches")
        return _format_dry_run(f"close_branch({name!r})", steps)

    memory_text = _read_file_at_ref(name, f"branches/{name}/MEMORY.md")
    summary = _summarize_with_ollama(memory_text)

    _run_git("checkout", base)
    if squash:
        # --squash stages the branch's net changes without committing and
        # without recording a merge, so the follow-up commit is ours to name.
        _run_git("merge", "--squash", name)
        _run_git("commit", "-m", f"Squash-merge branch '{name}'")
    else:
        _run_git("merge", "--no-ff", "-m", f"Merge branch '{name}'", name)

    ARCHIVED_BRANCHES_DIR.mkdir(parents=True, exist_ok=True)
    _run_git(
        "mv",
        str((BRANCHES_DIR / name).relative_to(WORKSPACE_ROOT)),
        str((ARCHIVED_BRANCHES_DIR / name).relative_to(WORKSPACE_ROOT)),
    )
    archived_memory_path = _archive_memory(name, "Completed")
    _record_branch_outcome(name, base, "Completed", summary)
    _try_index(name)

    return archived_memory_path


def abandon_branch(name: str, reason: str = "", dry_run: bool = False) -> Path | str:
    """Archive a branch's memory without merging it - for work that didn't pan out.

    Everything `close_branch` does except the merge: the branch's own
    MEMORY.md is written straight into branches/archived/<name>/ on its base
    (there's no merge to carry the file across), stamped Abandoned, and the
    branch is recorded as Abandoned in STATE_TRACKER.md for a top-level
    branch, or under its root's `## Sub-Branches` for a nested one. `reason`
    - why the work was dropped - goes in the outcome cell.

    Deliberately makes no LLM call, unlike close_branch. What's worth
    promoting to the main line here isn't a compression of what the branch
    did (the archived MEMORY.md still holds all of that, one hop away) but
    *why it was dropped* - a fact only you have at abandon time, which no
    summarizer could infer from the log.

    The branch ref and its commits are left in place, exactly as close_branch
    leaves them - the work is never deleted, just left unmerged and marked.
    Ends checked out on the base branch.
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    _ensure_clean_worktree()

    base = _branched_from(name)
    _ensure_not_already_archived(base, name)

    if dry_run:
        steps = [
            f"copy branches/{name}/MEMORY.md to branches/archived/{name}/MEMORY.md "
            f"on '{base}' (status -> Abandoned)",
            f"leave '{name}' unmerged, its branch ref and commits intact",
        ]
        if base == MAIN_BRANCH:
            steps.append(f"mark '{name}' Abandoned in STATE_TRACKER.md")
        else:
            steps.append(f"mark '{name}' Abandoned under '{_root_branch(base)}'s ## Sub-Branches")
        return _format_dry_run(f"abandon_branch({name!r})", steps)

    memory_text = _read_file_at_ref(name, f"branches/{name}/MEMORY.md")

    _run_git("checkout", base)

    archive_dir = ARCHIVED_BRANCHES_DIR / name
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "MEMORY.md").write_text(memory_text + "\n", encoding="utf-8")

    archived_memory_path = _archive_memory(name, "Abandoned")
    _record_branch_outcome(name, base, "Abandoned", reason)
    _try_index(name)

    return archived_memory_path
