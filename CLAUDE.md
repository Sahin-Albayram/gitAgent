# GitAgent — Project Context for Claude Code

## What this project is
A personal learning project: building a custom agentic workspace to master LLMs,
RAG, and agentic systems by dogfooding self-built tools. The current focus is
**GitAgent** — a git-backed memory management system for agent sessions.

## Two separate layers — don't conflate them
1. **Build-time (this session):** Claude Code writes, debugs, and iterates on the
   GitAgent codebase.
2. **Run-time:** GitAgent itself calls a **local Ollama model** to perform its
   operations (branch summarization, tool-calling loop). Claude is never called
   at runtime unless explicitly building a hybrid step — see "Open questions" below.

## Core design (decided so far)
GitAgent uses real git as the substrate rather than reinventing branching.

```
workspace/
├── STATE_TRACKER.md          # main line — persistent project memory
├── branches/
│   └── <branch-name>/
│       └── MEMORY.md         # scoped working notes for that branch only
└── gitagent/
    ├── cli.py
    └── tools.py
```

**Three core tool-calling operations** (to be exposed to the local Ollama model
via a tool-calling loop):

- `open_branch(name, description)` — creates a real `git branch`, creates
  `branches/<name>/MEMORY.md` from a template (goal, status, decisions log,
  open questions), updates the branch table in `STATE_TRACKER.md` to `Active`.
- `update_branch(name, note)` — appends a timestamped note to that branch's
  `MEMORY.md`. No LLM call needed — structured logging only. This is the
  "commit" equivalent.
- `close_branch(name)` — reads the full `MEMORY.md` for the branch, generates a
  compressed summary via LLM call (what changed, decisions made, outcome),
  writes that summary into the `STATE_TRACKER.md` row (status → Completed),
  performs the actual `git merge`, and archives the branch memory file
  (never deletes — keep the raw log for later retrieval).

## Learning objectives this maps onto
- `update_branch` → pure tool-calling practice.
- `close_branch` → summarization/compression practice — deciding what's salient
  enough to promote from branch memory to main memory (a mini RAG problem).
- Later: `search_branches` over archived branch memories via embeddings — this
  is where the RAG learning branch naturally plugs in.

## Conventions
- Language: Python.
- Local model runtime: Ollama.
- Git operations: prefer shelling out to real `git` commands over a Python git
  library, to keep the repo genuinely inspectable/debuggable with plain git.
- Keep `MEMORY.md` files small and scoped — a new agent session for a branch
  should only need to read that branch's memory file, not the whole state
  tracker.

## Open questions / not yet decided
- Whether `close_branch`'s summarization step should stay on the local model or
  become a hybrid call to Claude (via API) if local summarization quality proves
  insufficient.
- Whether to add a `search_branches` tool backed by embeddings over archived
  branch memories (RAG branch).
- Final tech stack for orchestration framework / vector store (tracked as
  Branch-003 in STATE_TRACKER.md, currently Planned).

## Working style
- Step-by-step integration: implement each layer locally before building
  higher-level abstractions.
- Dogfood every completed tool inside the actual workspace workflow.
- Update `STATE_TRACKER.md` whenever a branch is completed or merged — this is
  a hard rule, not a suggestion, since the whole point of GitAgent is to make
  this update automatic instead of manual.