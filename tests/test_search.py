"""Tests for gitagent.search - the semantic search / RAG layer.

Skipped wholesale when chromadb isn't installed, so the base test suite stays
runnable on an install without the optional [search] extra.

The embedding call is stubbed by default (see the `stub_embeddings` fixture):
these tests are about the storage/indexing/query wiring, and shouldn't depend
on a live Ollama. The fake embedder is still *semantically meaningful* - see
its docstring - so ranking assertions remain real assertions.
"""
from __future__ import annotations

import pytest

pytest.importorskip("chromadb", reason="search extra not installed")

import gitagent.search as search  # noqa: E402
import gitagent.tools as tools  # noqa: E402


VOCAB = ["branch", "nest", "parent", "summary", "ollama", "bread", "garden"]


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    """A deterministic bag-of-words embedder standing in for the real model.

    Each dimension counts occurrences of one vocabulary word, so texts sharing
    vocabulary really do land closer together under cosine distance. That keeps
    "the right branch ranks first" a genuine assertion rather than a tautology,
    without needing a model server in the test loop.
    """
    vectors = []
    for text in texts:
        lowered = text.lower()
        # +0.01 keeps the vector non-zero; cosine distance is undefined at zero.
        vectors.append([float(lowered.count(word)) + 0.01 for word in VOCAB])
    return vectors


@pytest.fixture
def search_repo(gitagent_repo, tmp_path, monkeypatch):
    """gitagent_repo, plus search repointed at a temp Chroma dir and stubbed embeddings."""
    monkeypatch.setattr(search, "ARCHIVED_BRANCHES_DIR", tools.ARCHIVED_BRANCHES_DIR)
    monkeypatch.setattr(search, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(search, "_embed", _fake_embed)
    return gitagent_repo


def _archive(name: str, goal: str, notes: list[str]) -> None:
    """Write an archived MEMORY.md directly, without a full branch lifecycle."""
    memory_dir = tools.ARCHIVED_BRANCHES_DIR / name
    memory_dir.mkdir(parents=True, exist_ok=True)
    decisions = "\n".join(f"- [2026-01-01 00:00 UTC] {note}" for note in notes)
    (memory_dir / "MEMORY.md").write_text(
        f"# Branch Memory: {name}\n\n"
        f"## Branched From\nmain\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Status\nCompleted\n\n"
        f"## Decisions Log\n{decisions}\n\n"
        f"## Sub-Branches\n\n## Open Questions\n",
        encoding="utf-8",
    )


# --- chunking / parsing ------------------------------------------------------

def test_parse_memory_extracts_goal_decisions_status_and_base(search_repo):
    _archive("a-branch", "make nesting work", ["chose the parent approach"])
    text = (tools.ARCHIVED_BRANCHES_DIR / "a-branch" / "MEMORY.md").read_text(encoding="utf-8")

    document, status, base = search._parse_memory(text)

    assert "make nesting work" in document
    assert "chose the parent approach" in document
    assert status == "Completed"
    assert base == "main"
    # template scaffolding must stay out of the embedded text - it is identical
    # across every branch and so contributes nothing but noise
    assert "## Open Questions" not in document
    assert "## Sub-Branches" not in document


def test_parse_memory_tolerates_missing_sections(search_repo):
    document, status, base = search._parse_memory("# Branch Memory: x\n\n## Goal\nonly a goal\n")
    assert document == "only a goal"
    assert status == ""
    assert base == ""


# --- indexing ----------------------------------------------------------------

def test_reindex_all_indexes_every_archived_branch(search_repo):
    _archive("branch-one", "nest a branch under a parent", ["did the nesting"])
    _archive("branch-two", "summarize with ollama", ["called the model"])

    assert search.reindex_all() == 2
    assert search._collection().count() == 2


def test_index_branch_is_idempotent(search_repo):
    _archive("branch-one", "nest a branch under a parent", ["did the nesting"])

    search.index_branch("branch-one")
    search.index_branch("branch-one")

    # upsert, not add - re-indexing overwrites rather than duplicating
    assert search._collection().count() == 1


def test_index_branch_rejects_unknown_branch(search_repo):
    with pytest.raises(tools.GitAgentError, match="no archived MEMORY.md"):
        search.index_branch("never-existed")


# --- query -------------------------------------------------------------------

def test_search_ranks_the_semantically_closest_branch_first(search_repo):
    _archive("nesting-work", "nest a branch under a parent branch", ["parent nest branch"])
    _archive("bread-work", "bake bread in the garden", ["bread garden bread"])
    search.reindex_all()

    hits = search.search_branches("parent nest branch", n_results=2)

    assert hits[0].name == "nesting-work"
    assert hits[0].similarity > hits[1].similarity
    assert hits[0].status == "Completed"
    assert hits[0].base == "main"


def test_search_reports_similarity_not_raw_distance(search_repo):
    _archive("nesting-work", "nest a branch under a parent branch", ["parent nest branch"])
    search.reindex_all()

    hit = search.search_branches("parent nest branch")[0]

    # cosine distance is inverted into a 0..1 score where higher means closer;
    # an (almost) exact text match should score near the top of that range
    assert 0.0 <= hit.similarity <= 1.0
    assert hit.similarity > 0.9


def test_search_on_empty_index_raises_cleanly(search_repo):
    with pytest.raises(tools.GitAgentError, match="index is empty"):
        search.search_branches("anything")


def test_search_caps_n_results_to_index_size(search_repo):
    _archive("only-one", "the sole branch", ["a note"])
    search.reindex_all()

    # asking for more hits than exist must not raise
    assert len(search.search_branches("branch", n_results=10)) == 1


# --- integration with the branch lifecycle -----------------------------------

def test_close_branch_indexes_automatically(search_repo, monkeypatch):
    monkeypatch.setattr(search, "CHROMA_DIR", search.CHROMA_DIR)  # keep the temp dir
    tools.open_branch("auto-indexed", "nest a branch under a parent")
    tools.update_branch("auto-indexed", "parent nest branch decision")
    tools.close_branch("auto-indexed")

    hits = search.search_branches("parent nest branch")
    assert any(hit.name == "auto-indexed" for hit in hits)


def test_close_branch_survives_a_broken_index(search_repo, monkeypatch, capsys):
    """Indexing is a derived cache - its failure must never fail the close."""
    def _boom(texts):
        raise tools.GitAgentError("Ollama is down")

    monkeypatch.setattr(search, "_embed", _boom)

    tools.open_branch("still-closes", "work that closes anyway")
    archived_path = tools.close_branch("still-closes")

    assert archived_path.exists()
    assert "## Status\nCompleted" in archived_path.read_text(encoding="utf-8")
    assert "could not index" in capsys.readouterr().err
