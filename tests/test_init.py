"""Tests for gitagent.init: bootstrapping a brand-new project."""
from __future__ import annotations

import subprocess

import pytest

import gitagent.init as init


def test_init_project_bootstraps_fresh_directory(fresh_workspace):
    result_path = init.init_project(
        "TestProject", goal="build a thing", motivation="learning", methodology="step by step"
    )

    assert result_path == init.STATE_TRACKER_PATH
    text = result_path.read_text(encoding="utf-8")
    assert "TestProject" in text
    assert "build a thing" in text
    assert (fresh_workspace / ".git").exists()
    assert (fresh_workspace / "branches").is_dir()


def test_init_project_requires_goal(fresh_workspace):
    with pytest.raises(init.GitAgentError, match="goal is required"):
        init.init_project("TestProject", goal="")


def test_init_project_refuses_if_already_initialized(fresh_workspace):
    init.init_project("TestProject", goal="build a thing")

    with pytest.raises(init.GitAgentError, match="already exists"):
        init.init_project("TestProject", goal="build it again")


def test_init_project_requires_main_branch_if_repo_exists(fresh_workspace):
    subprocess.run(
        ["git", "init", "-q", "-b", "not-main"], cwd=fresh_workspace, check=True
    )

    with pytest.raises(init.GitAgentError, match="not 'main'"):
        init.init_project("TestProject", goal="build a thing")
