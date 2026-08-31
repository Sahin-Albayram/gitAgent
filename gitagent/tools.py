"""Git-backed tool operations for GitAgent.

Each function here is exposed as a tool call to the local Ollama model.
Git itself is the substrate: branches are real `git branch`es that actually
diverge (each branch's own commits carry its `branches/<name>/MEMORY.md`),
so `close_branch`'s merge is a real merge, not a no-op.

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


def _log_sub_branch_close(root: str, name: str, summary: str) -> None:
    memory_path = BRANCHES_DIR / root / "MEMORY.md"
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    prefix = f"- **{name}**"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"- **{name}** — Completed — {summary}"
            break
    else:
        raise GitAgentError(f"no Sub-Branches entry for {name} under {root}")
    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run_git("add", str(memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Log close of {name} under {root}")


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


def close_branch(name: str, dry_run: bool = False) -> Path | str:
    """Summarize, merge, and archive a branch.

    Summarizes the branch's MEMORY.md via the local Ollama model and merges
    the branch into its recorded base (MAIN_BRANCH for a top-level branch,
    or the parent branch it was nested under - see `open_branch`), then
    moves (never deletes) the MEMORY.md into branches/archived/<name>/ on
    the base branch so the raw log stays retrievable.

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

    _ensure_clean_worktree()

    base = _branched_from(name)

    if dry_run:
        steps = [
            "generate a summary of its MEMORY.md via Ollama",
            f"merge '{name}' into '{base}' (--no-ff)",
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
    _run_git("merge", "--no-ff", "-m", f"Merge branch '{name}'", name)

    archive_dir = ARCHIVED_BRANCHES_DIR / name
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        "mv",
        str((BRANCHES_DIR / name).relative_to(WORKSPACE_ROOT)),
        str(archive_dir.relative_to(WORKSPACE_ROOT)),
    )
    archived_memory_path = archive_dir / "MEMORY.md"
    _set_memory_status(archived_memory_path, "Completed")
    _run_git("add", str(archived_memory_path.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Archive branch: {name}")

    if base == MAIN_BRANCH:
        _update_state_tracker_status(name, "Completed", summary)
        _run_git("add", str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)))
        _run_git("commit", "-m", f"Close branch: {name}")
    else:
        root = _root_branch(base)
        _run_git("checkout", root)
        _log_sub_branch_close(root, name, summary)
        if root != base:
            _run_git("checkout", base)

    return archive_dir / "MEMORY.md"
