# GitAgent

A git-backed memory management system for agent sessions. Real git branches
and commits are the substrate: opening a unit of work creates an actual
`git branch` with its own scoped `MEMORY.md`; closing it does an actual
merge and archives the log, never deletes it. See [CLAUDE.md](CLAUDE.md)
for the full design rationale.

## Prerequisites

- Python 3.10+
- `git`
- [Ollama](https://ollama.com) running locally, with a model pulled (default
  `llama3.1:8b`) - used for `close_branch`'s summary and `init`'s field
  polishing. Everything else works without it.
- For `search`/`reindex` only, a dedicated **embedding** model:
  `ollama pull nomic-embed-text`. A generative model like `llama3.1` cannot
  produce embeddings and Ollama will refuse with "This server does not support
  embeddings" - that error is about the model, not your server.

## Install

```bash
pip install -e .
```

This registers a `gitagent` command. For running the test suite too:

```bash
pip install -e .[dev]
pytest
```

## Usage

Bootstrap a brand-new project (creates the git repo and `STATE_TRACKER.md`
if they don't exist yet):

```bash
gitagent init --name "My Project" --goal "what you're building"
```

Open a branch for a unit of work:

```bash
gitagent open-branch my-feature "short description of the goal"
```

Pass `-b/--base <other-branch>` to nest it under another open branch instead
of `main`.

Log a working note (the "commit" equivalent - no LLM call, just appends a
timestamped bullet):

```bash
gitagent update-branch my-feature "decided to use approach X because Y"
```

Summarize, merge, and archive a branch when the work is done:

```bash
gitagent close-branch my-feature
```

Closing squashes the branch's per-note commits into one by default, so they
don't bury the base branch's history - nothing is lost, since the
note-by-note history stays on the branch ref (never deleted) and in the
archived `MEMORY.md`. Pass `--no-squash` for a traditional `--no-ff` merge
commit, or set `GITAGENT_SQUASH_ON_CLOSE=0` to make that the default.

Or, for work that didn't pan out - archives the memory and marks it
Abandoned, but never merges it (the branch ref and its commits stay put,
nothing is deleted):

```bash
gitagent abandon-branch my-feature --reason "approach didn't work out"
```

Any of `open-branch`, `update-branch`, `close-branch`, or `abandon-branch`
accepts `--dry-run` to print what it would do without doing it - no branch
created, no file written, no git history touched.

See what's active without opening `STATE_TRACKER.md` by hand:

```bash
gitagent status          # active branches only
gitagent status --all    # include Completed/Abandoned too
```

Search archived branch memories by meaning, not keywords (needs the `search`
extra - `pip install -e .[search]`):

```bash
gitagent search "how did I handle nesting?"
```

Closing or abandoning a branch indexes it automatically. Because the index is a
derived cache, it can be deleted at any time and rebuilt - and branches archived
before this feature existed need a one-off backfill:

```bash
gitagent reindex
```

Render the commit/branch graph, or the branch table and that graph together,
to a standalone HTML file:

```bash
gitagent graph
```

```bash
gitagent dashboard
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `GITAGENT_OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `GITAGENT_OLLAMA_MODEL` | `llama3.1:8b` | Model used for summarization/polishing |
| `GITAGENT_OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model used by `search`/`reindex` |
| `GITAGENT_SQUASH_ON_CLOSE` | `1` | Squash a branch's note commits on close; `0` for a `--no-ff` merge |
