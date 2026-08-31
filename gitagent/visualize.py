"""Render the repo's commit/branch graph as a standalone HTML page.

Lays commits out top-to-bottom in git's own topo-order, assigns each an
x-lane using the same streaming algorithm tools like `git log --graph` use
(a lane holds the sha it's waiting to reach; a commit lands in the first
lane expecting it, or opens a free one), and draws it as SVG next to a
scrollable commit log. Shells out to `git` rather than a Python git library,
same as the rest of gitagent.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ._git import run_git

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


PAGE_TEMPLATE = """<title>{title}</title>
<style>
:root {{
  --bg: #0b0f14;
  --surface: #121820;
  --surface-2: #1a222c;
  --border: #26303c;
  --text: #dfe6ee;
  --text-dim: #7c8a9a;
  --accent: #e8a33d;
  --accent-soft: rgba(232, 163, 61, 0.14);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Manrope', ui-sans-serif, system-ui, sans-serif;
}}
.page {{
  max-width: 980px;
  margin: 0 auto;
  padding: 32px 20px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}}
.topbar {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}
.repo {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.dot {{
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
  flex: 0 0 auto;
}}
.repo h1 {{
  font-size: 1.15rem;
  font-weight: 700;
  margin: 0;
  letter-spacing: -0.01em;
  text-wrap: balance;
}}
.stats {{
  display: flex;
  gap: 14px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.78rem;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}}
.panel {{
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: auto;
  max-height: 72vh;
}}
.graph-row {{
  display: flex;
  align-items: flex-start;
  min-width: max-content;
  padding: 6px 0;
}}
.graph-svg {{ flex: 0 0 auto; }}
.edge {{ fill: none; stroke-width: 2; opacity: 0.85; }}
.node {{ stroke: var(--surface); stroke-width: 2; transition: r 120ms ease; }}
.node.active {{ r: 7.5; }}
.rows {{
  position: relative;
  flex: 1 1 auto;
  min-width: 460px;
}}
.row {{
  position: absolute;
  left: 0;
  right: 0;
  height: {row_height}px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px 0 4px;
  white-space: nowrap;
  border-radius: 6px;
}}
.row.active {{ background: var(--surface-2); }}
.hash {{
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.76rem;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}}
.subject {{
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.82rem;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.pill {{
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
}}
.pill.head {{
  color: var(--accent);
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
}}
.foot {{
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 0.72rem;
  color: var(--text-dim);
}}
@media (prefers-reduced-motion: reduce) {{
  .node {{ transition: none; }}
}}
</style>
<div class="page">
  <div class="topbar">
    <div class="repo">
      <span class="dot"></span>
      <h1>{repo_name}</h1>
    </div>
    <div class="stats">
      <span>{commit_count} commits</span>
      <span>{branch_count} branches</span>
    </div>
  </div>
  <div class="panel">
    <div class="graph-row">
      <svg class="graph-svg" width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}">
        {svg_body}
      </svg>
      <div class="rows" style="height:{svg_height}px">
        {rows_body}
      </div>
    </div>
  </div>
  <div class="foot">generated {timestamp}</div>
</div>
<script>
(function () {{
  var nodes = document.querySelectorAll('.node');
  var rows = document.querySelectorAll('.row');
  function setActive(sha, on) {{
    nodes.forEach(function (n) {{ if (n.dataset.sha === sha) n.classList.toggle('active', on); }});
    rows.forEach(function (r) {{ if (r.dataset.sha === sha) r.classList.toggle('active', on); }});
  }}
  rows.forEach(function (r) {{
    r.addEventListener('mouseenter', function () {{ setActive(r.dataset.sha, true); }});
    r.addEventListener('mouseleave', function () {{ setActive(r.dataset.sha, false); }});
  }});
  nodes.forEach(function (n) {{
    n.addEventListener('mouseenter', function () {{ setActive(n.dataset.sha, true); }});
    n.addEventListener('mouseleave', function () {{ setActive(n.dataset.sha, false); }});
  }});
}})();
</script>
"""


def _fragment() -> tuple[str, int]:
    commits = _load_commits()
    by_sha = _assign_lanes(commits)
    edges = _edges(commits, by_sha)
    svg_body, svg_width, svg_height = _build_svg(commits, edges)
    rows_body = _build_rows(commits)
    branch_count = len(_run_git("branch", "-a").strip().splitlines())
    repo_name = WORKSPACE_ROOT.name

    fragment = PAGE_TEMPLATE.format(
        title=f"{repo_name} Branch Graph",
        repo_name=html.escape(repo_name),
        commit_count=len(commits),
        branch_count=branch_count,
        svg_width=svg_width,
        svg_height=svg_height,
        svg_body=svg_body,
        rows_body=rows_body,
        row_height=ROW_HEIGHT,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    return fragment, len(commits)


def render_branch_graph(output_path: Path | None = None) -> Path:
    """Render the full commit/branch graph to a standalone HTML file.

    Self-contained - open it directly in any browser, no server needed.
    """
    fragment, _ = _fragment()
    output_path = output_path or (WORKSPACE_ROOT / "branch_graph.html")

    page = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=JetBrains+Mono:wght@400;500;600&family=Manrope:wght@500;600;700&display=swap\" />\n"
        f"</head>\n<body>\n{fragment}\n</body>\n</html>\n"
    )
    output_path.write_text(page, encoding="utf-8")
    return output_path
