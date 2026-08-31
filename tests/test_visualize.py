"""Tests for gitagent.visualize - the graph and dashboard HTML renderers."""
from __future__ import annotations

import pytest

import gitagent.tools as tools
import gitagent.visualize as visualize


@pytest.fixture
def viz_repo(gitagent_repo, monkeypatch):
    """gitagent_repo with visualize pointed at the same throwaway repo."""
    monkeypatch.setattr(visualize, "WORKSPACE_ROOT", gitagent_repo)
    return gitagent_repo


def test_render_branch_graph_writes_a_self_contained_page(viz_repo, tmp_path):
    tools.open_branch("feature-x", "some work")

    output = visualize.render_branch_graph(tmp_path / "graph.html")
    page = output.read_text(encoding="utf-8")

    assert page.startswith("<!doctype html>")
    assert "<title>" in page and "Branch Graph" in page
    assert "Open branch: feature-x" in page  # commit subjects are rendered
    assert "<svg" in page
    # self-contained: no external scripts, styles are inlined
    assert "<script src=" not in page


def test_render_dashboard_shows_branches_and_graph(viz_repo, tmp_path):
    tools.open_branch("active-work", "a branch still in progress")
    tools.open_branch("finished-work", "a branch that will complete")
    tools.close_branch("finished-work")
    tools.open_branch("dropped-work", "a branch that will be dropped")
    tools.abandon_branch("dropped-work", reason="did not pan out")

    output = visualize.render_dashboard(tmp_path / "dashboard.html")
    page = output.read_text(encoding="utf-8")

    # every branch appears, each with its own status pill
    for name in ("active-work", "finished-work", "dropped-work"):
        assert name in page
    assert "is-active" in page
    assert "is-completed" in page
    assert "is-abandoned" in page

    # outcomes are surfaced, not just statuses
    assert "did not pan out" in page
    # the commit graph is embedded on the same page
    assert "<svg" in page
    assert "graph-svg" in page


def test_dashboard_lists_active_branches_first(viz_repo, tmp_path):
    tools.open_branch("zzz-finished", "closed first, sorts last alphabetically")
    tools.close_branch("zzz-finished")
    tools.open_branch("aaa-active", "still going")

    page = visualize.render_dashboard(tmp_path / "dashboard.html").read_text(encoding="utf-8")

    assert page.index("aaa-active") < page.index("zzz-finished")


def test_dashboard_renders_sub_branches_under_their_root(viz_repo, tmp_path):
    tools.open_branch("parent", "a parent line of work")
    tools.open_branch("child", "nested work", base="parent")

    page = visualize.render_dashboard(tmp_path / "dashboard.html").read_text(encoding="utf-8")

    assert "child" in page
    assert 'class="subs"' in page


def test_dashboard_escapes_html_in_branch_text(viz_repo, tmp_path):
    tools.open_branch("xss-check", "a <script>alert(1)</script> description")

    page = visualize.render_dashboard(tmp_path / "dashboard.html").read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_status_class_falls_back_for_unknown_status():
    assert visualize._status_class("Active") == "is-active"
    assert visualize._status_class("Completed") == "is-completed"
    assert visualize._status_class("Abandoned") == "is-abandoned"
    # a hand-written status shouldn't produce an unstyled pill
    assert visualize._status_class("Something Custom") == "is-planned"
