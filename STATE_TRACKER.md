# Agentic Workspace Development: State Tracker

## Project Overview & Mission
- **Goal:** Build a personalized assistant and agentic structure within a custom workspace.
- **Motivation:** Learn and master LLMs, Retrieval-Augmented Generation (RAG), and agentic systems through continuous, hands-on implementation.
- **Methodology:** Apply new concepts one by one into the workspace and actively use ("dogfood") the custom-developed tools to experience every step of the development cycle.

## Current State (Main Line)
- **Current Phase:** Developing GitAgent (Agentic Git Memory Structure)
- **Latest Update:** Completed local LLM environment setup — installed Ollama, ran open-weight models on local hardware, executed basic Python tool-calling loops. Shifting focus to building an autonomous versioning system for this tracker. Currently setting up Claude Code as the development tool for implementing GitAgent.

## Core Learning Objectives
- **Local Inference & LLM Fundamentals:** Local runtime setup, model quantization (GGUF/AWQ), latency/VRAM benchmarking, prompt formatting, context window handling.
- **Agentic Architecture:** Tool calling, reasoning loops, memory systems, multi-agent orchestration.
- **RAG:** Chunking strategies, vector embeddings, vector databases, hybrid search.
- **Custom Tooling:** Developing, testing, and dogfooding self-built workspace tools.

## Side Branches (Features & Quests)

| Branch ID | Feature / Quest Name | Description | Status | Target Outcome | Base |
|---|---|---|---|---|---|
| Branch-001 | Local LLM Setup & Benchmarking | Download and test local open-weight models (e.g., Llama 3, Mistral, Qwen) using a local runner (e.g., Ollama) to evaluate hardware performance and latency. | Completed | A validated local LLM runtime with recorded response times and VRAM usage. | main |
| Branch-002 | GitAgent (Agentic Git Memory) | Develop a system to automatically manage Main Line and version memory. Handles creating branches, merging, and updating context. | Active | A functional Git-like context management tool. | main |
| Branch-003 | Define Tech Stack | Finalize base workspace technologies (orchestration frameworks, local vector store, interface). | Planned | Documented architectural decisions. | main |
| Branch-004 | Base Assistant Chat UI | Create a minimal chat interface connected to the local model runtime. | Planned | A lightweight working UI for basic conversation. | main |
| Branch-005 | gitagent-update-branch | Implement update_branch(name, note): append a timestamped note to a branch's MEMORY.md as the git-commit equivalent of a working note. | Completed | The update_branch(name, note) function was implemented in tools.py, allowing timestamped notes to be appended to a branch's MEMORY.md file, and was integrated with the update-branch CLI subcommand. The outcome is a persistent state tracker for branch notes, with ordered and timestamped entries. | main |

## Implementation Guidelines & Rules
1. **Step-by-Step Integration:** Implement each layer locally before building higher-level abstractions.
2. **Dogfooding:** Use each completed tool directly inside the workspace workflow.
3. **State Preservation:** Update this document whenever a branch is completed or merged.
