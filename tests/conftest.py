"""Shared pytest fixtures for GitAgent's test suite.

Formalizes the bootstrap-and-monkeypatch technique used to verify the
branch-name/status/StopIteration fixes: point the gitagent modules at a
throwaway temp git repo instead of the real workspace, and stub the Ollama
calls so tests never need a live model running.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import gitagent.init as init
import gitagent.tools as tools


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def gitagent_repo(tmp_path, monkeypatch):
    """A throwaway, already-initialized git repo with tools.py repointed at it.

    Tests of init_project itself need a repo that ISN'T initialized yet -
    see the `fresh_workspace` fixture below for that case.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", tools.MAIN_BRANCH)
    _run(repo, "config", "user.email", "test@test.com")
    _run(repo, "config", "user.name", "Test")

    state_tracker = repo / "STATE_TRACKER.md"
    state_tracker.write_text(
        "# Test: State Tracker\n\n"
        "## Side Branches (Features & Quests)\n\n"
        "| Branch ID | Feature / Quest Name | Description | Status | Target Outcome |\n"
        "|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    branches_dir = repo / "branches"
    branches_dir.mkdir()
    (branches_dir / ".gitkeep").write_text("", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "init")

    monkeypatch.setattr(tools, "WORKSPACE_ROOT", repo)
    monkeypatch.setattr(tools, "BRANCHES_DIR", branches_dir)
    monkeypatch.setattr(tools, "ARCHIVED_BRANCHES_DIR", branches_dir / "archived")
    monkeypatch.setattr(tools, "STATE_TRACKER_PATH", state_tracker)
    monkeypatch.setattr(tools, "_summarize_with_ollama", lambda memory_text: "stub summary")

    return repo


@pytest.fixture
def fresh_workspace(tmp_path, monkeypatch):
    """An empty, non-git, non-initialized directory for testing init_project."""
    workspace = tmp_path / "fresh"
    workspace.mkdir()

    monkeypatch.setattr(init, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(init, "STATE_TRACKER_PATH", workspace / "STATE_TRACKER.md")
    monkeypatch.setattr(init, "BRANCHES_DIR", workspace / "branches")
    monkeypatch.setattr(init, "_polish_with_ollama", lambda field, raw: raw.strip())

    return workspace
