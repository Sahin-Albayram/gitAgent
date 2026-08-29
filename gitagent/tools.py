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
from datetime import datetime, timezone
from pathlib import Path

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

BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

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


class GitAgentError(RuntimeError):
    """Raised when a GitAgent tool operation cannot complete."""


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitAgentError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _branch_exists(name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=WORKSPACE_ROOT,
    )
    return result.returncode == 0


def _ensure_clean_worktree() -> None:
    """Refuse to switch branches over uncommitted changes to tracked files.

    Untracked files are fine - git itself already refuses a checkout that
    would actually clobber one, so there's no need to be stricter here.
    """
    status = _run_git("status", "--porcelain")
    dirty = [line for line in status.splitlines() if not line.startswith("??")]
    if dirty:
        raise GitAgentError(
            "working tree has uncommitted changes - commit or stash them "
            "before running a GitAgent tool that switches branches"
        )


def _branched_from(name: str) -> str:
    """Read a branch's recorded parent from its own MEMORY.md.

    Works via `git show`, so it doesn't require checking `name` out, and
    works just as well after `name` has been closed (its own branch ref and
    history stick around even once merged elsewhere).
    """
    memory_text = _run_git("show", f"{name}:branches/{name}/MEMORY.md")
    lines = memory_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "## Branched From")
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


def _insert_state_tracker_row(branch_id: str, name: str, description: str) -> None:
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()

    header_idx = next(i for i, line in enumerate(lines) if line.startswith("| Branch ID"))
    row_idx = header_idx + 2  # skip header and separator row
    while row_idx < len(lines) and lines[row_idx].startswith("|"):
        row_idx += 1

    row = f"| {branch_id} | {name} | {description} | Active | |"
    lines.insert(row_idx, row)

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _section_end(lines: list[str], header: str) -> int:
    start = next(i for i, line in enumerate(lines) if line.strip() == header)
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return end


def _append_bullet(memory_path: Path, header: str, bullet: str) -> None:
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    insert_idx = _section_end(lines, header)
    while insert_idx > 0 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1
    lines.insert(insert_idx, bullet)
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


def open_branch(name: str, description: str, base: str = MAIN_BRANCH) -> Path:
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
    """
    if not BRANCH_NAME_RE.match(name):
        raise GitAgentError(f"invalid branch name: {name!r}")
    if _branch_exists(name):
        raise GitAgentError(f"branch already exists: {name}")
    if not _branch_exists(base):
        raise GitAgentError(f"base branch does not exist: {base}")

    _ensure_clean_worktree()

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


def update_branch(name: str, note: str) -> Path:
    """Append a timestamped note to a branch's MEMORY.md - the commit equivalent.

    Checks out `name` (from wherever the worktree currently is) and commits
    the note there, since MEMORY.md only exists on that branch's own history.
    Ends checked out on `name`.
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    _ensure_clean_worktree()
    _run_git("checkout", name)

    memory_path = BRANCHES_DIR / name / "MEMORY.md"
    if not memory_path.exists():
        raise GitAgentError(f"no MEMORY.md found for branch: {name}")

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

    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[1] == name:
            cells[3] = status
            cells[4] = outcome
            lines[i] = "| " + " | ".join(cells) + " |"
            break
    else:
        raise GitAgentError(f"no STATE_TRACKER.md row found for branch: {name}")

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def close_branch(name: str) -> Path:
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
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    _ensure_clean_worktree()

    memory_text = _run_git("show", f"{name}:branches/{name}/MEMORY.md")
    base = _branched_from(name)
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
