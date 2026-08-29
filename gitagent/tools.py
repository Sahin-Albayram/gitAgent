"""Git-backed tool operations for GitAgent.

Each function here is exposed as a tool call to the local Ollama model.
Git itself is the substrate: branches are real `git branch`es that actually
diverge (each branch's own commits carry its `branches/<name>/MEMORY.md`),
so `close_branch`'s merge is a real merge, not a no-op. STATE_TRACKER.md is
the one exception - it's only ever read/written on MAIN_BRANCH, so it stays
authoritative and current regardless of how deep a branch is nested.
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


def _current_branch() -> str:
    return _run_git("rev-parse", "--abbrev-ref", "HEAD")


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


def _next_branch_id(lines: list[str]) -> str:
    ids = []
    for line in lines:
        match = re.match(r"\|\s*Branch-(\d+)\s*\|", line)
        if match:
            ids.append(int(match.group(1)))
    return f"Branch-{max(ids, default=0) + 1:03d}"


def _insert_state_tracker_row(branch_id: str, name: str, description: str, base: str) -> None:
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()

    header_idx = next(i for i, line in enumerate(lines) if line.startswith("| Branch ID"))
    row_idx = header_idx + 2  # skip header and separator row
    while row_idx < len(lines) and lines[row_idx].startswith("|"):
        row_idx += 1

    row = f"| {branch_id} | {name} | {description} | Active | | {base} |"
    lines.insert(row_idx, row)

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def open_branch(name: str, description: str, base: str = MAIN_BRANCH) -> Path:
    """Create a git branch off `base` and its scoped MEMORY.md, marking it
    Active in STATE_TRACKER.md.

    `base` can be MAIN_BRANCH (the default - a normal top-level branch) or
    the name of another open branch, to nest work under it. The branch
    actually diverges: its MEMORY.md is only ever committed on its own
    branch, never on MAIN_BRANCH. STATE_TRACKER.md, however, is always read
    and written on MAIN_BRANCH specifically, regardless of `base`, so it
    stays visible and current without needing anything merged first. Ends
    checked out on the new branch, ready to work.
    """
    if not BRANCH_NAME_RE.match(name):
        raise GitAgentError(f"invalid branch name: {name!r}")
    if _branch_exists(name):
        raise GitAgentError(f"branch already exists: {name}")
    if not _branch_exists(base):
        raise GitAgentError(f"base branch does not exist: {base}")

    _ensure_clean_worktree()

    _run_git("checkout", MAIN_BRANCH)
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()
    branch_id = _next_branch_id(lines)
    _insert_state_tracker_row(branch_id, name, description, base)
    _run_git("add", str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Open branch: {name}")

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


def _section_end(lines: list[str], header: str) -> int:
    start = next(i for i, line in enumerate(lines) if line.strip() == header)
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return end


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

    lines = memory_path.read_text(encoding="utf-8").splitlines()
    insert_idx = _section_end(lines, "## Decisions Log")
    while insert_idx > 0 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.insert(insert_idx, f"- [{timestamp}] {note}")

    memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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


def _state_tracker_row(text: str, name: str) -> list[str]:
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[1] == name:
            return cells
    raise GitAgentError(f"no STATE_TRACKER.md row found for branch: {name}")


def _branch_base(name: str) -> str:
    canonical = _run_git("show", f"{MAIN_BRANCH}:STATE_TRACKER.md")
    return _state_tracker_row(canonical, name)[5]


def _update_state_tracker_status(name: str, status: str, outcome: str) -> None:
    lines = STATE_TRACKER_PATH.read_text(encoding="utf-8").splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[1] == name:
            cells[3] = status
            cells[4] = outcome
            lines[i] = "| " + " | ".join(cells) + " |"
            break
    else:
        raise GitAgentError(f"no STATE_TRACKER.md row found for branch: {name}")

    STATE_TRACKER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def close_branch(name: str) -> Path:
    """Summarize, merge, and archive a branch.

    Summarizes the branch's MEMORY.md via the local Ollama model, merges the
    branch into its recorded base (MAIN_BRANCH for a top-level branch, or
    the parent branch it was nested under - see `open_branch`), marks it
    Completed in STATE_TRACKER.md with that summary, and moves (never
    deletes) the MEMORY.md into branches/archived/<name>/ on the base branch
    so the raw log stays retrievable. STATE_TRACKER.md is always read and
    written on MAIN_BRANCH regardless of `base`, but the function ends
    checked out back on `base` - not MAIN_BRANCH - so the archived MEMORY.md
    this returns is actually visible in the working tree.
    """
    if not _branch_exists(name):
        raise GitAgentError(f"branch does not exist: {name}")

    _ensure_clean_worktree()

    memory_text = _run_git("show", f"{name}:branches/{name}/MEMORY.md")
    base = _branch_base(name)
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

    _run_git("checkout", MAIN_BRANCH)
    _update_state_tracker_status(name, "Completed", summary)
    _run_git("add", str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)))
    _run_git("commit", "-m", f"Close branch: {name}")

    if base != MAIN_BRANCH:
        _run_git("checkout", base)

    return archive_dir / "MEMORY.md"
