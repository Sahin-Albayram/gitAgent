"""Semantic search over archived branch memories - GitAgent's RAG layer.

THE PIPELINE, END TO END
------------------------
1. EMBED    Text goes through an embedding model, which returns a fixed-length
            list of floats (768 of them for nomic-embed-text). That list is a
            coordinate in 768-dimensional space, positioned so that text with
            similar *meaning* lands nearby. This is why "nested branching"
            matches a note about "sub-branches under a parent" even though the
            two share no keywords - something grep can never do.
2. STORE    Chroma keeps, per entry: the vector, the original text, an id, and
            metadata. On top it builds an HNSW index (a navigable graph) so
            nearest-neighbour lookups don't have to compare against every entry.
3. QUERY    The search string takes the *same* trip through the *same* model,
            then Chroma returns whichever entries sit closest to it.

WHY THE INDEX IS DISPOSABLE
---------------------------
Everything here is a derived cache. The source of truth stays exactly where it
already was: the archived MEMORY.md files tracked in git. `.gitagent/chroma/` is
gitignored, and `reindex_all()` rebuilds the whole thing from those files. That
keeps this honest to GitAgent's "git is the substrate" premise - delete the
directory and you lose nothing but the time to regenerate it.

chromadb is an optional dependency (`pip install -e .[search]`), so every import
of it here is deliberately lazy - the day-to-day open/update/close path must
keep working on a bare install.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ._git import GitAgentError
# tools imports *this* module lazily (inside close_branch/abandon_branch), so
# importing tools at module level here creates no circular-import problem.
from .tools import ARCHIVED_BRANCHES_DIR, OLLAMA_HOST, WORKSPACE_ROOT, _find_line_index

# A dedicated embedding model, not a generative one. Generative models like
# llama3.1 are loaded without an embedding pooling layer and Ollama will refuse
# with "This server does not support embeddings" - the error is about the model,
# not the server. Pull it once with: ollama pull nomic-embed-text
OLLAMA_EMBED_MODEL = os.environ.get("GITAGENT_OLLAMA_EMBED_MODEL", "nomic-embed-text")

CHROMA_DIR = WORKSPACE_ROOT / ".gitagent" / "chroma"
COLLECTION_NAME = "branch_memories"


@dataclass
class SearchHit:
    name: str
    status: str
    base: str
    snippet: str
    similarity: float


def _require_chromadb():
    try:
        import chromadb
    except ImportError as exc:
        raise GitAgentError(
            "search needs chromadb, which isn't installed - "
            "install it with: pip install -e .[search]"
        ) from exc
    return chromadb


def _embed(texts: list[str], model: str | None = None, host: str | None = None) -> list[list[float]]:
    """Turn each string into a vector via Ollama's batch embeddings endpoint.

    One HTTP round trip for the whole batch rather than one per document -
    on a reindex of many branches that difference is most of the runtime.
    """
    model = model or OLLAMA_EMBED_MODEL
    host = host or OLLAMA_HOST

    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        f"{host}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read())
    # HTTPError must be caught before URLError - it's a subclass. Ollama answers
    # 501 here when the model has no embedding support, i.e. you pointed this at
    # a generative model. Its response body carries the real explanation, which
    # is far more useful than the status line, so surface that instead.
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise GitAgentError(
            f"Ollama rejected an embedding request for model {model!r} "
            f"(HTTP {exc.code}): {detail}\n"
            f"If that's a generative model, pull a dedicated embedding model "
            f"instead: ollama pull nomic-embed-text"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GitAgentError(f"failed to reach Ollama at {host}: {exc}") from exc

    if "embeddings" not in body:
        # Most commonly: the configured model can't embed at all. Generative
        # models report "This server does not support embeddings" here.
        raise GitAgentError(
            f"Ollama returned no embeddings for model {model!r}: "
            f"{body.get('error', body)}"
        )
    return body["embeddings"]


def _embedding_function():
    """Adapt `_embed` to the interface Chroma expects.

    Chroma has no built-in Ollama provider, so this small shim is what lets it
    call a local model. The parameter MUST be named `input` - Chroma inspects
    the signature of __call__ and rejects (or silently misbehaves with) any
    other name. It's a classic silent-failure trap, hence the explicit note.

    Defined inside a function so `chromadb` is only imported when search is
    actually used - the class has to subclass a chromadb type, which would
    otherwise force the optional dependency at module import time.
    """
    _require_chromadb()
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

    class OllamaEmbeddingFunction(EmbeddingFunction):
        def __init__(self, model: str = OLLAMA_EMBED_MODEL, host: str = OLLAMA_HOST):
            self._model = model
            self._host = host

        def __call__(self, input: Documents) -> Embeddings:
            return _embed(list(input), model=self._model, host=self._host)

        # --- the serialization contract -------------------------------------
        # Chroma persists name() + get_config() next to the collection, then
        # uses build_from_config() to rebuild this object in a later process.
        # That's how it can warn you that a collection was embedded with a
        # different model than the one you're now querying with - which would
        # otherwise silently return nonsense, since vectors from two different
        # models don't share a coordinate space.
        @staticmethod
        def name() -> str:
            return "gitagent-ollama"

        def get_config(self) -> dict:
            return {"model": self._model, "host": self._host}

        @staticmethod
        def build_from_config(config: dict) -> "OllamaEmbeddingFunction":
            return OllamaEmbeddingFunction(model=config["model"], host=config["host"])

        def default_space(self) -> str:
            # Chroma's own default is "l2"; for text embeddings cosine is the
            # better metric (see _collection).
            return "cosine"

    return OllamaEmbeddingFunction()


def _collection():
    """Open (or create) the persistent collection backing the search index."""
    chromadb = _require_chromadb()
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    # PersistentClient writes a SQLite file plus binary index segments under
    # this path. An in-memory client would lose the index between CLI calls,
    # and every command here is its own short-lived process.
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_embedding_function(),
        # Chroma defaults to L2 (squared euclidean). Cosine is the right metric
        # for text embeddings: it compares direction and ignores magnitude, so a
        # long branch log isn't penalised against a short one purely for length.
        metadata={"hnsw:space": "cosine"},
    )


def _section_or_empty(lines: list[str], header: str) -> list[str]:
    """The lines under `## <header>`, or [] if that section is missing.

    Indexing has to tolerate hand-edited or older memory files, so unlike
    tools._section_body this never raises over a missing section.
    """
    try:
        start = _find_line_index(lines, lambda line: line.strip() == header, header)
    except GitAgentError:
        return []
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return [line for line in lines[start + 1 : end] if line.strip()]


def _parse_memory(memory_text: str) -> tuple[str, str, str]:
    """Split a MEMORY.md into (document_to_embed, status, base).

    THE CHUNKING DECISION - the real judgement call in any RAG system.

    One document per branch, built from Goal + Decisions Log only. Reasoning:
      - A branch memory is already a small, topically coherent unit - roughly
        the size that chunking strategies normally *try* to produce.
      - Splitting per-bullet would sever each decision from the goal that gives
        it meaning, so a hit would lose its context.
      - Embedding the whole file would dilute the signal with template
        scaffolding ("## Open Questions", "## Sub-Branches") that is identical
        across every branch and therefore pure noise in the vector.
    If branch logs ever grow past what one vector can faithfully represent,
    per-section chunking is the natural next step.
    """
    lines = memory_text.splitlines()
    goal = _section_or_empty(lines, "## Goal")
    decisions = _section_or_empty(lines, "## Decisions Log")
    status = _section_or_empty(lines, "## Status")
    base = _section_or_empty(lines, "## Branched From")

    document = "\n".join(goal + decisions).strip()
    return document, (status[0] if status else ""), (base[0] if base else "")


def _archived_memory_path(name: str) -> Path:
    return ARCHIVED_BRANCHES_DIR / name / "MEMORY.md"


def index_branch(name: str) -> None:
    """Add or refresh one archived branch in the index.

    Uses upsert rather than add so re-running is idempotent - re-indexing an
    already-indexed branch overwrites it instead of raising or duplicating.
    """
    memory_path = _archived_memory_path(name)
    if not memory_path.exists():
        raise GitAgentError(f"no archived MEMORY.md to index for branch: {name}")

    document, status, base = _parse_memory(memory_path.read_text(encoding="utf-8"))
    if not document:
        return  # nothing meaningful to embed

    _collection().upsert(
        ids=[name],
        documents=[document],
        # Metadata is filterable at query time, which is what makes
        # "show me *abandoned* work about X" possible later - exactly the
        # "did I already try this and drop it?" question this feature exists for.
        metadatas=[{"status": status, "base": base}],
    )


def reindex_all() -> int:
    """Rebuild the index from every archived MEMORY.md on disk.

    Needed because the index is derived: branches archived before this feature
    existed aren't in it, and the whole store can be deleted at any time.
    Embeds in one batch, so this is a single Ollama round trip.
    """
    names, documents, metadatas = [], [], []
    for memory_path in sorted(ARCHIVED_BRANCHES_DIR.glob("*/MEMORY.md")):
        document, status, base = _parse_memory(memory_path.read_text(encoding="utf-8"))
        if not document:
            continue
        names.append(memory_path.parent.name)
        documents.append(document)
        metadatas.append({"status": status, "base": base})

    if not names:
        return 0

    _collection().upsert(ids=names, documents=documents, metadatas=metadatas)
    return len(names)


def search_branches(query: str, n_results: int = 5) -> list[SearchHit]:
    """Find archived branches whose memory is semantically closest to `query`.

    The query string is embedded by the same model that embedded the documents
    - that shared vector space is the whole mechanism. Comparing vectors from
    two different models would be meaningless.
    """
    collection = _collection()
    if collection.count() == 0:
        raise GitAgentError("search index is empty - run 'gitagent reindex' first")

    # Chroma embeds `query_texts` for us, through the same embedding_function
    # the collection was created with.
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, collection.count()),
    )

    hits = []
    # query() returns one list per query string; we only ever pass one.
    for name, document, metadata, distance in zip(
        results["ids"][0],
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        snippet = " ".join(document.split())[:160]
        hits.append(
            SearchHit(
                name=name,
                status=(metadata or {}).get("status", ""),
                base=(metadata or {}).get("base", ""),
                snippet=snippet,
                # With cosine space Chroma reports distance = 1 - similarity,
                # so invert it back into a 0..1 score where higher is closer.
                similarity=round(1.0 - distance, 4),
            )
        )
    return hits
