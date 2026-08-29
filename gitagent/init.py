"""Bootstrap a brand-new project for GitAgent: git repo + STATE_TRACKER.md.

Mirrors the structured-Q&A-then-Ollama-synthesis pattern already used by
close_branch in tools.py: the user's own raw notes go in, a single Ollama
call per field polishes them into clean prose - nothing gets invented along
the way. Question-asking itself lives in cli.py; this module just takes the
already-collected answers.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .tools import MAIN_BRANCH, OLLAMA_HOST, OLLAMA_MODEL, GitAgentError

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
STATE_TRACKER_PATH = WORKSPACE_ROOT / "STATE_TRACKER.md"
BRANCHES_DIR = WORKSPACE_ROOT / "branches"

POLISH_PROMPT = """You are helping someone write the {field} line of a new \
project's persistent memory file, from their own rough note.

Rewrite the note below into 1-2 clear, plain-text sentences in the same \
voice and intent - fix grammar and clarity only, do not invent facts, \
goals, or details that aren't in the note. Do not use markdown, headers, \
bullet points, or the "|" character. Respond with only the rewritten \
sentences themselves - no preamble like "Here is a summary", no closing \
remarks.

Note:
{raw}
"""

STATE_TRACKER_TEMPLATE = """# {name}: State Tracker

## Project Overview & Mission
{mission_bullets}

## Current State (Main Line)
- **Current Phase:** Getting started
- **Latest Update:** Project initialized via `gitagent init`.

## Side Branches (Features & Quests)

| Branch ID | Feature / Quest Name | Description | Status | Target Outcome |
|---|---|---|---|---|

## Implementation Guidelines & Rules
1. **Step-by-Step Integration:** Implement each layer locally before building higher-level abstractions.
2. **Dogfooding:** Use each completed tool directly inside the workspace workflow.
3. **State Preservation:** Update this document whenever a branch is completed or merged.
"""


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


def _polish_with_ollama(field: str, raw: str) -> str:
    if not raw.strip():
        return ""

    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": POLISH_PROMPT.format(field=field, raw=raw),
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

    polished = body["response"].strip()
    return polished.replace("|", "/").replace("\n", " ")


def _mission_bullets(goal: str, motivation: str, methodology: str) -> str:
    lines = [f"- **Goal:** {goal}"]
    if motivation:
        lines.append(f"- **Motivation:** {motivation}")
    if methodology:
        lines.append(f"- **Methodology:** {methodology}")
    return "\n".join(lines)


def init_project(name: str, goal: str, motivation: str = "", methodology: str = "") -> Path:
    """Bootstrap STATE_TRACKER.md (and the repo/branches/ dir if needed) for a new project.

    `goal`/`motivation`/`methodology` are the user's own raw notes - `goal`
    is required, the other two are optional. Each non-empty one is polished
    into 1-2 clean sentences via a single Ollama call (same pattern as
    close_branch's summarization), so nothing gets invented along the way.

    Refuses to run if STATE_TRACKER.md already exists, so it never clobbers
    an already-initialized project. If the directory isn't a git repo yet,
    initializes one on MAIN_BRANCH; if it already is one, requires it to
    already be on MAIN_BRANCH (renaming an existing repo's default branch
    isn't something this does on your behalf).
    """
    if not goal.strip():
        raise GitAgentError("a goal is required - what are you building?")
    if STATE_TRACKER_PATH.exists():
        raise GitAgentError(
            f"STATE_TRACKER.md already exists at {STATE_TRACKER_PATH} - "
            "this project looks already initialized"
        )

    git_dir = WORKSPACE_ROOT / ".git"
    if not git_dir.exists():
        _run_git("init", "-q", "-b", MAIN_BRANCH)
    else:
        current = _run_git("symbolic-ref", "--short", "HEAD")
        if current != MAIN_BRANCH:
            raise GitAgentError(
                f"this repo's current branch is '{current}', not '{MAIN_BRANCH}' - "
                f"switch to a branch named '{MAIN_BRANCH}' before running init"
            )

    mission_bullets = _mission_bullets(
        _polish_with_ollama("Goal", goal),
        _polish_with_ollama("Motivation", motivation),
        _polish_with_ollama("Methodology", methodology),
    )

    STATE_TRACKER_PATH.write_text(
        STATE_TRACKER_TEMPLATE.format(name=name, mission_bullets=mission_bullets),
        encoding="utf-8",
    )

    BRANCHES_DIR.mkdir(exist_ok=True)
    gitkeep = BRANCHES_DIR / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    _run_git(
        "add",
        str(STATE_TRACKER_PATH.relative_to(WORKSPACE_ROOT)),
        str(gitkeep.relative_to(WORKSPACE_ROOT)),
    )
    _run_git("commit", "-m", f"Initialize project: {name}")

    return STATE_TRACKER_PATH
