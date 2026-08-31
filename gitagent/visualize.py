"""Render GitAgent's state as standalone HTML pages.

Two views, both self-contained files you open directly in a browser - no
server, no build step, regenerated on demand:

  render_branch_graph()  the commit/branch graph alone (`gitagent graph`)
  render_dashboard()     STATE_TRACKER's branch table above that same graph
                         (`gitagent dashboard`)

The graph lays commits out top-to-bottom in git's own topo-order, assigns each
an x-lane using the same streaming algorithm tools like `git log --graph` use
(a lane holds the sha it's waiting to reach; a commit lands in the first lane
expecting it, or opens a free one), and draws it as SVG next to a scrollable
commit log. Shells out to `git` rather than a Python git library, same as the
rest of gitagent.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ._git import run_git
from .tools import MAIN_BRANCH, GitAgentError, _read_file_at_ref, list_branches

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

LANE_COLORS = [
    "#ff8a5c",
    "#4ade80",
    "#38bdf8",
    "#e879f9",
    "#facc15",
    "#fb7185",
    "#a78bfa",
    "#2dd4bf",
    "#f97316",
    "#84cc16",
]

ROW_HEIGHT = 34
TOP_MARGIN = ROW_HEIGHT // 2
LANE_WIDTH = 26
LEFT_MARGIN = 17
NODE_RADIUS = 5.5


@dataclass
class Commit:
    sha: str
    parents: list[str]
    refs: list[str]
    subject: str
    lane: int = 0
    y: int = 0


def _run_git(*args: str) -> str:
    return run_git(WORKSPACE_ROOT, *args)


def _load_commits() -> list[Commit]:
    out = _run_git("log", "--all", "--topo-order", "--format=%H%x01%P%x01%D%x01%s")
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, parents, refs, subject = line.split("\x01")
        commits.append(
            Commit(
                sha=sha,
                parents=parents.split() if parents else [],
                refs=[r.strip() for r in refs.split(",") if r.strip()],
                subject=subject,
            )
        )
    return commits


def _assign_lanes(commits: list[Commit]) -> dict[str, Commit]:
    """Lane-assignment pass: walk newest-to-oldest, each lane tracks the sha
    it's waiting to reach next. A commit claims the first lane expecting it
    (or the first free lane, or a new one), then hands that lane to its
    first parent and opens fresh lanes for any merge parents."""
    lanes: list[str | None] = []
    by_sha = {c.sha: c for c in commits}

    for y, commit in enumerate(commits):
        commit.y = y

        lane_idx = next(
            (i for i, expected in enumerate(lanes) if expected == commit.sha), None
        )
        if lane_idx is None:
            lane_idx = next((i for i, expected in enumerate(lanes) if expected is None), None)
        if lane_idx is None:
            lane_idx = len(lanes)
            lanes.append(None)
        commit.lane = lane_idx

        lanes[lane_idx] = None
        if commit.parents:
            first_parent, *merge_parents = commit.parents
            lanes[lane_idx] = first_parent
            for parent in merge_parents:
                if parent in lanes:
                    continue
                free = next((i for i, e in enumerate(lanes) if e is None), None)
                if free is None:
                    free = len(lanes)
                    lanes.append(None)
                lanes[free] = parent

    return by_sha


def _edges(commits: list[Commit], by_sha: dict[str, Commit]) -> list[tuple[Commit, Commit]]:
    edges = []
    for commit in commits:
        for parent_sha in commit.parents:
            parent = by_sha.get(parent_sha)
            if parent is not None:
                edges.append((commit, parent))
    return edges


def _x(lane: int) -> float:
    return LEFT_MARGIN + lane * LANE_WIDTH


def _y(row: int) -> float:
    return TOP_MARGIN + row * ROW_HEIGHT


def _lane_color(lane: int) -> str:
    return LANE_COLORS[lane % len(LANE_COLORS)]


def _build_svg(commits: list[Commit], edges: list[tuple[Commit, Commit]]) -> tuple[str, int, int]:
    max_lane = max((c.lane for c in commits), default=0)
    width = int(LEFT_MARGIN * 2 + max_lane * LANE_WIDTH)
    height = int(TOP_MARGIN * 2 + max(len(commits) - 1, 0) * ROW_HEIGHT)

    parts = []
    for child, parent in edges:
        x1, y1 = _x(child.lane), _y(child.y)
        x2, y2 = _x(parent.lane), _y(parent.y)
        color = _lane_color(child.lane if child.lane == parent.lane else parent.lane)
        if x1 == x2:
            path = f"M {x1} {y1} L {x2} {y2}"
        else:
            mid_y = (y1 + y2) / 2
            path = f"M {x1} {y1} C {x1} {mid_y}, {x2} {mid_y}, {x2} {y2}"
        parts.append(f'<path class="edge" d="{path}" stroke="{color}" />')

    for commit in commits:
        x, y = _x(commit.lane), _y(commit.y)
        color = _lane_color(commit.lane)
        parts.append(
            f'<circle class="node" data-sha="{commit.sha}" cx="{x}" cy="{y}" '
            f'r="{NODE_RADIUS}" fill="{color}" />'
        )

    return "\n".join(parts), width, height


def _ref_badges(refs: list[str]) -> list[tuple[str, bool]]:
    badges = []
    for ref in refs:
        if ref.startswith("HEAD -> "):
            badges.append((ref[len("HEAD -> ") :], True))
        elif ref == "HEAD" or ref.startswith("tag:"):
            continue
        else:
            badges.append((ref, False))
    return badges


def _build_rows(commits: list[Commit]) -> str:
    rows = []
    for commit in commits:
        color = _lane_color(commit.lane)
        pills = "".join(
            f'<span class="pill{" head" if is_head else ""}" '
            f'style="--pill-color:{color}">{html.escape(name)}</span>'
            for name, is_head in _ref_badges(commit.refs)
        )
        rows.append(
            f'<div class="row" data-sha="{commit.sha}" '
            f'style="top:{commit.y * ROW_HEIGHT}px" title="{html.escape(commit.sha)}">'
            f'<span class="hash">{commit.sha[:7]}</span>'
            f'{pills}'
            f'<span class="subject">{html.escape(commit.subject)}</span>'
            f"</div>"
        )
    return "\n".join(rows)


# Plain CSS, deliberately not run through str.format() - that's why it can use
# normal single braces instead of the doubled ones a format template needs. The
# one genuinely dynamic value, the commit row height, is passed as the
# --row-height custom property on the .rows element instead of interpolated here.
BASE_CSS = """
:root {
  --bg: #0b0f14;
  --surface: #121820;
  --surface-2: #1a222c;
  --border: #26303c;
  --text: #dfe6ee;
  --text-dim: #7c8a9a;
  --accent: #e8a33d;
  --accent-soft: rgba(232, 163, 61, 0.14);
  --ok: #4ade80;
  --dropped: #fb7185;
  --planned: #7c8a9a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Manrope', ui-sans-serif, system-ui, sans-serif;
}
.page {
  max-width: 980px;
  margin: 0 auto;
  padding: 32px 20px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.topbar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.repo { display: flex; align-items: center; gap: 10px; }
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
  flex: 0 0 auto;
}
.repo h1 {
  font-size: 1.15rem;
  font-weight: 700;
  margin: 0;
  letter-spacing: -0.01em;
  text-wrap: balance;
}
.stats {
  display: flex;
  gap: 14px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.78rem;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.phase {
  font-size: 0.85rem;
  color: var(--text-dim);
  margin: -6px 0 0;
}
.phase strong { color: var(--text); font-weight: 600; }
.section-label {
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-dim);
  margin: 8px 0 -6px;
}
.panel {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: auto;
  max-height: 72vh;
}
.branches { max-height: none; padding: 6px; }
.branch {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 4px 10px;
  align-items: baseline;
  padding: 12px 14px;
  border-radius: 8px;
}
.branch + .branch { border-top: 1px solid var(--border); border-radius: 0; }
.branch-id {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.72rem;
  color: var(--text-dim);
}
.branch-name { font-weight: 700; font-size: 0.95rem; letter-spacing: -0.01em; }
.branch-desc {
  grid-column: 2 / -1;
  font-size: 0.85rem;
  color: var(--text-dim);
  line-height: 1.45;
}
.branch-outcome {
  grid-column: 2 / -1;
  font-size: 0.82rem;
  line-height: 1.45;
  padding-left: 10px;
  border-left: 2px solid var(--border);
  color: var(--text);
}
.subs { grid-column: 2 / -1; margin: 4px 0 0; padding: 0; list-style: none; }
.subs li {
  font-size: 0.8rem;
  color: var(--text-dim);
  padding: 2px 0 2px 12px;
  border-left: 2px solid var(--border);
}
.status {
  font-size: 0.66rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 3px 9px;
  border-radius: 100px;
  white-space: nowrap;
  color: var(--status-color);
  background: color-mix(in srgb, var(--status-color) 15%, transparent);
  border: 1px solid color-mix(in srgb, var(--status-color) 45%, transparent);
}
.status.is-active { --status-color: var(--accent); }
.status.is-completed { --status-color: var(--ok); }
.status.is-abandoned { --status-color: var(--dropped); }
.status.is-planned { --status-color: var(--planned); }
.empty { padding: 20px 16px; color: var(--text-dim); font-size: 0.85rem; }
.graph-row {
  display: flex;
  align-items: flex-start;
  min-width: max-content;
  padding: 6px 0;
}
.graph-svg { flex: 0 0 auto; }
.edge { fill: none; stroke-width: 2; opacity: 0.85; }
.node { stroke: var(--surface); stroke-width: 2; transition: r 120ms ease; }
.node.active { r: 7.5; }
.rows { position: relative; flex: 1 1 auto; min-width: 460px; }
.row {
  position: absolute;
  left: 0;
  right: 0;
  height: var(--row-height);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px 0 4px;
  white-space: nowrap;
  border-radius: 6px;
}
.row.active { background: var(--surface-2); }
.hash {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.76rem;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.subject {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.82rem;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pill {
  font-family: 'Manrope', ui-sans-serif, sans-serif;
  font-size: 0.66rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px 7px;
  border-radius: 100px;
  color: var(--pill-color);
  background: color-mix(in srgb, var(--pill-color) 16%, transparent);
  border: 1px solid color-mix(in srgb, var(--pill-color) 45%, transparent);
  flex: 0 0 auto;
}
.pill.head {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
}
.foot {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.72rem;
  color: var(--text-dim);
}
@media (prefers-reduced-motion: reduce) {
  .node { transition: none; }
}
"""

GRAPH_JS = """
(function () {
  var nodes = document.querySelectorAll('.node');
  var rows = document.querySelectorAll('.row');
  function setActive(sha, on) {
    nodes.forEach(function (n) { if (n.dataset.sha === sha) n.classList.toggle('active', on); });
    rows.forEach(function (r) { if (r.dataset.sha === sha) r.classList.toggle('active', on); });
  }
  rows.forEach(function (r) {
    r.addEventListener('mouseenter', function () { setActive(r.dataset.sha, true); });
    r.addEventListener('mouseleave', function () { setActive(r.dataset.sha, false); });
  });
  nodes.forEach(function (n) {
    n.addEventListener('mouseenter', function () { setActive(n.dataset.sha, true); });
    n.addEventListener('mouseleave', function () { setActive(n.dataset.sha, false); });
  });
})();
"""


def _page(title: str, body: str) -> str:
    """Wrap page content in a complete, self-contained HTML document."""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=JetBrains+Mono:wght@400;500;600&family=Manrope:wght@500;600;700&display=swap" />\n'
        f"<style>{BASE_CSS}</style>\n"
        f"</head>\n<body>\n{body}\n"
        f"<script>{GRAPH_JS}</script>\n</body>\n</html>\n"
    )


def _topbar(heading: str, stats: list[str]) -> str:
    stat_spans = "".join(f"<span>{html.escape(s)}</span>" for s in stats)
    return (
        '<div class="topbar">'
        f'<div class="repo"><span class="dot"></span><h1>{html.escape(heading)}</h1></div>'
        f'<div class="stats">{stat_spans}</div>'
        "</div>"
    )


def _graph_panel() -> tuple[str, int, int]:
    """Build the commit-graph panel. Returns (html, commit_count, branch_count)."""
    commits = _load_commits()
    by_sha = _assign_lanes(commits)
    edges = _edges(commits, by_sha)
    svg_body, svg_width, svg_height = _build_svg(commits, edges)
    rows_body = _build_rows(commits)
    branch_count = len(_run_git("branch", "-a").strip().splitlines())

    panel = (
        '<div class="panel"><div class="graph-row">'
        f'<svg class="graph-svg" width="{svg_width}" height="{svg_height}" '
        f'viewBox="0 0 {svg_width} {svg_height}">{svg_body}</svg>'
        f'<div class="rows" style="height:{svg_height}px; --row-height:{ROW_HEIGHT}px">'
        f"{rows_body}</div>"
        "</div></div>"
    )
    return panel, len(commits), branch_count


def _foot() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f'<div class="foot">generated {stamp}</div>'


def render_branch_graph(output_path: Path | None = None) -> Path:
    """Render the full commit/branch graph to a standalone HTML file.

    Self-contained - open it directly in any browser, no server needed.
    """
    repo_name = WORKSPACE_ROOT.name
    panel, commit_count, branch_count = _graph_panel()

    body = (
        '<div class="page">'
        + _topbar(repo_name, [f"{commit_count} commits", f"{branch_count} branches"])
        + panel
        + _foot()
        + "</div>"
    )

    output_path = output_path or (WORKSPACE_ROOT / "branch_graph.html")
    output_path.write_text(_page(f"{repo_name} Branch Graph", body), encoding="utf-8")
    return output_path


def _status_class(status: str) -> str:
    """Map a STATE_TRACKER status to its pill CSS class, defaulting to neutral."""
    known = {"Active", "Completed", "Abandoned", "Planned"}
    return f"is-{status.lower()}" if status in known else "is-planned"


def _project_heading() -> tuple[str, str]:
    """Pull the project title and current phase out of STATE_TRACKER.md.

    Read from MAIN_BRANCH's committed copy, matching how tools.list_branches
    reads it, so the dashboard is consistent no matter which branch is checked
    out. Falls back to the directory name if the file isn't shaped as expected.
    """
    title, phase = WORKSPACE_ROOT.name, ""
    try:
        text = _read_file_at_ref(MAIN_BRANCH, "STATE_TRACKER.md")
    except GitAgentError:
        return title, phase

    for line in text.splitlines():
        if line.startswith("# ") and title == WORKSPACE_ROOT.name:
            title = line[2:].strip()
        elif "**Current Phase:**" in line:
            phase = line.split("**Current Phase:**", 1)[1].strip()
    return title, phase


def _branches_panel(branches) -> str:
    if not branches:
        return '<div class="panel branches"><div class="empty">No branches yet.</div></div>'

    cards = []
    for branch in branches:
        subs = "".join(
            f"<li>{html.escape(sub.lstrip('- '))}</li>" for sub in branch.sub_branches
        )
        outcome = (
            f'<div class="branch-outcome">{html.escape(branch.outcome)}</div>'
            if branch.outcome
            else ""
        )
        cards.append(
            '<div class="branch">'
            f'<span class="branch-id">{html.escape(branch.id)}</span>'
            f'<span class="branch-name">{html.escape(branch.name)}</span>'
            f'<span class="status {_status_class(branch.status)}">'
            f"{html.escape(branch.status)}</span>"
            f'<div class="branch-desc">{html.escape(branch.description)}</div>'
            + outcome
            + (f'<ul class="subs">{subs}</ul>' if subs else "")
            + "</div>"
        )
    return '<div class="panel branches">' + "".join(cards) + "</div>"


def render_dashboard(output_path: Path | None = None) -> Path:
    """Render STATE_TRACKER's branch table above the commit graph, as one page.

    A point-in-time snapshot, regenerated on demand exactly like
    `render_branch_graph` - not a live view. Genuinely live refresh would need
    a local HTTP server, which is a much bigger piece of scope than the value
    it adds for a workspace you regenerate whenever you want to look.
    """
    title, phase = _project_heading()
    branches = list_branches()
    panel, commit_count, branch_count = _graph_panel()

    tallies = {}
    for branch in branches:
        tallies[branch.status] = tallies.get(branch.status, 0) + 1
    # Active first so the thing you're working on leads; everything else follows
    # in the tracker's own order.
    branches = sorted(branches, key=lambda b: b.status != "Active")

    stats = [f"{count} {status.lower()}" for status, count in sorted(tallies.items())]
    stats.append(f"{commit_count} commits")

    phase_html = (
        f'<p class="phase"><strong>Current phase:</strong> {html.escape(phase)}</p>'
        if phase
        else ""
    )

    body = (
        '<div class="page">'
        + _topbar(title, stats)
        + phase_html
        + '<p class="section-label">Branches</p>'
        + _branches_panel(branches)
        + '<p class="section-label">History</p>'
        + panel
        + _foot()
        + "</div>"
    )

    output_path = output_path or (WORKSPACE_ROOT / "dashboard.html")
    output_path.write_text(_page(f"{title} Dashboard", body), encoding="utf-8")
    return output_path
