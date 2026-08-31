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

Any of `open-branch`, `update-branch`, or `close-branch` accepts `--dry-run`
to print what it would do without doing it - no branch created, no file
written, no git history touched.

See what's active without opening `STATE_TRACKER.md` by hand:

```bash
gitagent status          # active branches only
gitagent status --all    # include Completed/Abandoned too
```

Render the commit/branch graph to a standalone HTML file:

```bash
gitagent graph
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `GITAGENT_OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `GITAGENT_OLLAMA_MODEL` | `llama3.1:8b` | Model used for summarization/polishing |
