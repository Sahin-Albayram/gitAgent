"""Command-line interface for GitAgent."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .init import init_project
from .tools import (
    MAIN_BRANCH,
    GitAgentError,
    close_branch,
    list_branches,
    open_branch,
    update_branch,
)
from .visualize import render_branch_graph


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

    status_parser = subparsers.add_parser(
        "status", help="List branches from STATE_TRACKER.md"
    )
    status_parser.add_argument(
        "--all",
        action="store_true",
        help="Include Completed/Abandoned branches too (default: Active only)",
    )

    graph_parser = subparsers.add_parser(
        "graph", help="Render the commit/branch graph to a standalone HTML file"
    )
    graph_parser.add_argument(
        "-o", "--output", help="Output path (default: branch_graph.html in the repo root)"
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
            result = close_branch(args.name, dry_run=args.dry_run)
        except GitAgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result if args.dry_run else f"Closed branch '{args.name}' -> {result}")
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
            print(f"[{branch.id}] {branch.name} — {branch.status}")
            print(f"    {branch.description}")
            for sub in branch.sub_branches:
                print(f"    {sub}")
        return 0

    if args.command == "graph":
        output = Path(args.output) if args.output else None
        graph_path = render_branch_graph(output)
        print(f"Wrote graph -> {graph_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
