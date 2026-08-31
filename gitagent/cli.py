"""Command-line interface for GitAgent."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .init import init_project
from .tools import (
    MAIN_BRANCH,
    GitAgentError,
    abandon_branch,
    close_branch,
    list_branches,
    open_branch,
    update_branch,
)
from .visualize import render_branch_graph, render_dashboard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitagent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Bootstrap STATE_TRACKER.md (and the repo/branches dir) for a new project"
    )
    init_parser.add_argument("--name", help="Project name (prompted if omitted)")
    init_parser.add_argument(
        "--goal", help="What you're building, in your own words (prompted if omitted)"
    )
    init_parser.add_argument(
        "--motivation", help="Why it matters to you (prompted if omitted; optional)"
    )
    init_parser.add_argument(
        "--methodology", help="How you want to work on it (prompted if omitted; optional)"
    )

    open_branch_parser = subparsers.add_parser(
        "open-branch", help="Create a new branch and its scoped MEMORY.md"
    )
    open_branch_parser.add_argument("name", help="Branch name (also used as the git branch name)")
    open_branch_parser.add_argument("description", help="Short description of the branch's goal")
    open_branch_parser.add_argument(
        "-b",
        "--base",
        default=MAIN_BRANCH,
        help=f"Branch to open this one from (default: {MAIN_BRANCH}); pass another open "
        "branch's name to nest under it",
    )
    open_branch_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without doing it"
    )

    update_branch_parser = subparsers.add_parser(
        "update-branch", help="Append a timestamped note to a branch's MEMORY.md"
    )
    update_branch_parser.add_argument("name", help="Branch name")
    update_branch_parser.add_argument("note", help="Note to append to the branch's Decisions Log")
    update_branch_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without doing it"
    )

    close_branch_parser = subparsers.add_parser(
        "close-branch", help="Summarize, merge, and archive a branch"
    )
    close_branch_parser.add_argument("name", help="Branch name")
    close_branch_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without doing it"
    )
    # default=None on both, so that when neither flag is passed the choice
    # falls through to GITAGENT_SQUASH_ON_CLOSE rather than being overridden.
    close_branch_parser.add_argument(
        "--squash",
        dest="squash",
        action="store_true",
        default=None,
        help="Collapse the branch's per-note commits into one (default)",
    )
    close_branch_parser.add_argument(
        "--no-squash",
        dest="squash",
        action="store_false",
        help="Keep every commit and record a --no-ff merge commit instead",
    )

    abandon_branch_parser = subparsers.add_parser(
        "abandon-branch",
        help="Archive a branch without merging it - for work that didn't pan out",
    )
    abandon_branch_parser.add_argument("name", help="Branch name")
    abandon_branch_parser.add_argument(
        "-r",
        "--reason",
        default="",
        help="Why the work is being dropped (recorded as the branch's outcome)",
    )
    abandon_branch_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without doing it"
    )

    status_parser = subparsers.add_parser(
        "status", help="List branches from STATE_TRACKER.md"
    )
    status_parser.add_argument(
        "--all",
        action="store_true",
        help="Include Completed/Abandoned branches too (default: Active only)",
    )

    search_parser = subparsers.add_parser(
        "search", help="Semantic search over archived branch memories"
    )
    search_parser.add_argument("query", help="What to look for, in plain language")
    search_parser.add_argument(
        "-n", "--n-results", type=int, default=5, help="How many hits to show (default: 5)"
    )

    subparsers.add_parser(
        "reindex", help="Rebuild the search index from every archived MEMORY.md"
    )

    graph_parser = subparsers.add_parser(
        "graph", help="Render the commit/branch graph to a standalone HTML file"
    )
    graph_parser.add_argument(
        "-o", "--output", help="Output path (default: branch_graph.html in the repo root)"
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Render the branch table and commit graph as one HTML page"
    )
    dashboard_parser.add_argument(
        "-o", "--output", help="Output path (default: dashboard.html in the repo root)"
    )

    args = parser.parse_args(argv)

    if args.command == "init":
        name = args.name or input("Project name: ").strip()
        goal = args.goal or input("What are you building? ").strip()
        motivation = args.motivation
        if motivation is None:
            motivation = input("Why does this matter to you? (optional) ").strip()
        methodology = args.methodology
        if methodology is None:
            methodology = input("How do you want to work on it? (optional) ").strip()
        try:
            state_tracker_path = init_project(name, goal, motivation, methodology)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Initialized project '{name}' -> {state_tracker_path}")
        return 0

    if args.command == "open-branch":
        try:
            result = open_branch(args.name, args.description, base=args.base, dry_run=args.dry_run)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result if args.dry_run else f"Opened branch '{args.name}' -> {result}")
        return 0

    if args.command == "update-branch":
        try:
            result = update_branch(args.name, args.note, dry_run=args.dry_run)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result if args.dry_run else f"Updated branch '{args.name}' -> {result}")
        return 0

    if args.command == "close-branch":
        try:
            result = close_branch(args.name, dry_run=args.dry_run, squash=args.squash)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result if args.dry_run else f"Closed branch '{args.name}' -> {result}")
        return 0

    if args.command == "abandon-branch":
        try:
            result = abandon_branch(args.name, args.reason, dry_run=args.dry_run)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result if args.dry_run else f"Abandoned branch '{args.name}' -> {result}")
        return 0

    if args.command == "status":
        try:
            branches = list_branches()
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not args.all:
            branches = [b for b in branches if b.status == "Active"]
        if not branches:
            print("No branches." if args.all else "No active branches.")
            return 0
        for branch in branches:
            # ASCII separator on purpose - this goes to a terminal, which on
            # Windows may still be cp1252 and would mangle an em-dash.
            print(f"[{branch.id}] {branch.name} - {branch.status}")
            print(f"    {branch.description}")
            if branch.outcome:
                print(f"    -> {branch.outcome}")
            for sub in branch.sub_branches:
                print(f"    {sub}")
        return 0

    if args.command == "search":
        # Imported here, not at module scope, so the CLI still starts on a bare
        # install without the optional chromadb dependency.
        from .search import search_branches

        try:
            hits = search_branches(args.query, n_results=args.n_results)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not hits:
            print("No matches.")
            return 0
        for hit in hits:
            # `base` is blank for branches archived before MEMORY.md gained its
            # '## Branched From' section - don't print an empty "(from )".
            origin = f" (from {hit.base})" if hit.base else ""
            print(f"[{hit.similarity:.3f}] {hit.name} - {hit.status}{origin}")
            print(f"    {hit.snippet}")
        return 0

    if args.command == "reindex":
        from .search import reindex_all

        try:
            count = reindex_all()
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Indexed {count} archived branch memories.")
        return 0

    if args.command == "graph":
        output = Path(args.output) if args.output else None
        graph_path = render_branch_graph(output)
        print(f"Wrote graph -> {graph_path}")
        return 0

    if args.command == "dashboard":
        output = Path(args.output) if args.output else None
        try:
            dashboard_path = render_dashboard(output)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote dashboard -> {dashboard_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
