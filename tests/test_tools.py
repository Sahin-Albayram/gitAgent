"""Tests for gitagent.tools: the open/update/close branch lifecycle,
nesting, and the malformed-markdown error handling."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import gitagent.tools as tools


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# --- branch name validation -------------------------------------------------

@pytest.mark.parametrize("bad_name", ["..", ".", ".hidden"])
def test_open_branch_rejects_dot_names(gitagent_repo, bad_name):
    with pytest.raises(tools.GitAgentError, match="invalid branch name"):
        tools.open_branch(bad_name, "malicious name")


def test_open_branch_accepts_normal_names(gitagent_repo):
    tools.open_branch("gitagent-real-branch", "a normal branch")
    assert tools._branch_exists("gitagent-real-branch")


# --- happy path: open -> update -> close ------------------------------------

def test_open_update_close_lifecycle(gitagent_repo):
    tools.open_branch("gitagent-real-branch", "test branch")
    tools.update_branch("gitagent-real-branch", "did a thing")
    archived_path = tools.close_branch("gitagent-real-branch")

    archived_text = archived_path.read_text(encoding="utf-8")
    assert "## Status\nCompleted" in archived_text
    assert "## Status\nActive" not in archived_text
    assert "[" in archived_text and "did a thing" in archived_text

    tracker_text = tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")
    assert "gitagent-real-branch" in tracker_text
    assert "Completed" in tracker_text


def test_nested_branch_lifecycle(gitagent_repo):
    tools.open_branch("parent", "parent branch")
    tools.open_branch("child", "child branch", base="parent")
    tools.update_branch("child", "did some nested work")
    tools.close_branch("child")

    parent_memory = (tools.BRANCHES_DIR / "parent" / "MEMORY.md").read_text(encoding="utf-8")
    assert "**child** — Completed" in parent_memory

    tools.close_branch("parent")
    tracker_text = tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")
    assert "parent" in tracker_text and "Completed" in tracker_text
    # a nested branch never gets its own STATE_TRACKER row
    assert "child" not in tracker_text


# --- malformed markdown raises GitAgentError, not StopIteration ------------

def test_update_branch_on_malformed_memory_raises_cleanly(gitagent_repo):
    tools.open_branch("gitagent-malformed-branch", "will corrupt this one")
    mem_path = tools.BRANCHES_DIR / "gitagent-malformed-branch" / "MEMORY.md"
    mem_path.write_text(
        mem_path.read_text(encoding="utf-8").replace("## Decisions Log\n\n", ""),
        encoding="utf-8",
    )
    _run(gitagent_repo, "add", "-A")
    _run(gitagent_repo, "commit", "-q", "-m", "corrupt decisions log header")

    with pytest.raises(tools.GitAgentError, match="Decisions Log"):
        tools.update_branch("gitagent-malformed-branch", "note")


def test_close_branch_on_malformed_memory_raises_cleanly(gitagent_repo):
    tools.open_branch("gitagent-malformed-branch-2", "will corrupt this one too")
    mem_path = tools.BRANCHES_DIR / "gitagent-malformed-branch-2" / "MEMORY.md"
    mem_path.write_text(
        mem_path.read_text(encoding="utf-8").replace("## Branched From\nmain\n\n", ""),
        encoding="utf-8",
    )
    _run(gitagent_repo, "add", "-A")
    _run(gitagent_repo, "commit", "-q", "-m", "corrupt branched-from header")
    _run(gitagent_repo, "checkout", "-q", "main")

    with pytest.raises(tools.GitAgentError, match="Branched From"):
        tools.close_branch("gitagent-malformed-branch-2")


def test_open_branch_with_corrupted_state_tracker_raises_cleanly(gitagent_repo):
    saved = tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")
    tools.STATE_TRACKER_PATH.write_text(
        saved.replace(
            "| Branch ID | Feature / Quest Name | Description | Status | Target Outcome |\n",
            "",
        ),
        encoding="utf-8",
    )
    _run(gitagent_repo, "add", "-A")
    _run(gitagent_repo, "commit", "-q", "-m", "corrupt state tracker header")

    with pytest.raises(tools.GitAgentError, match="Branch ID"):
        tools.open_branch("gitagent-should-fail", "state tracker is corrupted")


# --- _ensure_clean_worktree scoping -----------------------------------------

def test_unrelated_dirty_file_does_not_block_tool_calls(gitagent_repo):
    (gitagent_repo / "notes.txt").write_text("scratch notes, not committed", encoding="utf-8")

    # should not raise, even though the worktree has an uncommitted file
    tools.open_branch("gitagent-real-branch", "test branch")
    assert tools._branch_exists("gitagent-real-branch")


def test_dirty_state_tracker_still_blocks_tool_calls(gitagent_repo):
    tools.STATE_TRACKER_PATH.write_text(
        tools.STATE_TRACKER_PATH.read_text(encoding="utf-8") + "\n<!-- uncommitted -->\n",
        encoding="utf-8",
    )
    with pytest.raises(tools.GitAgentError, match="uncommitted changes"):
        tools.open_branch("gitagent-real-branch", "test branch")


def test_dirty_branches_dir_still_blocks_tool_calls(gitagent_repo):
    tools.open_branch("gitagent-real-branch", "test branch")
    mem_path = tools.BRANCHES_DIR / "gitagent-real-branch" / "MEMORY.md"
    mem_path.write_text(mem_path.read_text(encoding="utf-8") + "\nuncommitted edit\n", encoding="utf-8")

    with pytest.raises(tools.GitAgentError, match="uncommitted changes"):
        tools.update_branch("gitagent-real-branch", "note")


# --- list_branches -----------------------------------------------------------

def test_list_branches_reports_active_and_sub_branches(gitagent_repo):
    tools.open_branch("parent", "parent branch")
    tools.open_branch("child", "child branch", base="parent")
    tools.open_branch("gitagent-real-branch", "another top-level branch")
    # deliberately left checked out on gitagent-real-branch, not main -
    # list_branches should work regardless

    branches = tools.list_branches()
    by_name = {b.name: b for b in branches}

    assert by_name["parent"].status == "Active"
    assert any("child" in sub for sub in by_name["parent"].sub_branches)
    assert by_name["gitagent-real-branch"].status == "Active"
    assert by_name["gitagent-real-branch"].sub_branches == []
    # a nested branch never gets its own top-level row
    assert "child" not in by_name


def test_list_branches_tolerates_active_row_with_no_matching_git_branch(gitagent_repo):
    """STATE_TRACKER.md rows aren't all guaranteed to be real git branches -
    e.g. a hand-authored umbrella row for a whole initiative, marked Active,
    whose name was never actually passed to open_branch. list_branches
    should skip sub-branch lookup for those instead of raising."""
    tools.STATE_TRACKER_PATH.write_text(
        tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")
        + "| Branch-999 | Some Umbrella Initiative | not a real git branch | Active | |\n",
        encoding="utf-8",
    )
    _run(gitagent_repo, "add", "-A")
    _run(gitagent_repo, "commit", "-q", "-m", "add hand-authored umbrella row")

    branches = tools.list_branches()
    by_name = {b.name: b for b in branches}
    assert by_name["Some Umbrella Initiative"].status == "Active"
    assert by_name["Some Umbrella Initiative"].sub_branches == []


# --- dry_run -------------------------------------------------------------

def test_open_branch_dry_run_does_not_mutate(gitagent_repo):
    before_tracker = tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")

    result = tools.open_branch("gitagent-real-branch", "test branch", dry_run=True)

    assert "[dry run]" in result
    assert not tools._branch_exists("gitagent-real-branch")
    assert not (tools.BRANCHES_DIR / "gitagent-real-branch").exists()
    assert tools.STATE_TRACKER_PATH.read_text(encoding="utf-8") == before_tracker


def test_update_branch_dry_run_does_not_mutate(gitagent_repo):
    tools.open_branch("gitagent-real-branch", "test branch")
    mem_path = tools.BRANCHES_DIR / "gitagent-real-branch" / "MEMORY.md"
    before_memory = mem_path.read_text(encoding="utf-8")

    result = tools.update_branch("gitagent-real-branch", "should not be written", dry_run=True)

    assert "[dry run]" in result
    after_memory = mem_path.read_text(encoding="utf-8")
    assert after_memory == before_memory
    assert "should not be written" not in after_memory


def test_close_branch_dry_run_does_not_mutate(gitagent_repo):
    tools.open_branch("gitagent-real-branch", "test branch")
    before_tracker = tools.STATE_TRACKER_PATH.read_text(encoding="utf-8")

    result = tools.close_branch("gitagent-real-branch", dry_run=True)

    assert "[dry run]" in result
    assert tools._branch_exists("gitagent-real-branch")
    assert not (tools.ARCHIVED_BRANCHES_DIR / "gitagent-real-branch").exists()
    assert tools.STATE_TRACKER_PATH.read_text(encoding="utf-8") == before_tracker


def test_close_branch_dry_run_works_without_ollama(gitagent_repo, monkeypatch):
    tools.open_branch("gitagent-real-branch", "test branch")

    def _boom(memory_text):
        raise tools.GitAgentError("Ollama is down")

    monkeypatch.setattr(tools, "_summarize_with_ollama", _boom)

    result = tools.close_branch("gitagent-real-branch", dry_run=True)
    assert "[dry run]" in result
