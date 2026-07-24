"""Renders a milestone dependency graph as a game-style tech tree PNG.

Pure Pillow — no graphviz, no headless browser, no external service. Layout is a
layered DAG: depth = longest path from a root, then a barycenter pass to reduce
edge crossings.
"""

from __future__ import annotations

import io
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

# --- palette ---------------------------------------------------------------
BG = "#0e1116"
GRID = "#161b22"

THEME = {
    "locked":    {"fill": "#161b22", "edge": "#2b3240", "text": "#525c6b", "accent": "#3a4150"},
    "available": {"fill": "#2a2008", "edge": "#f0b429", "text": "#f7dfa5", "accent": "#f0b429"},
    "active":    {"fill": "#0d2137", "edge": "#3b82f6", "text": "#bfdbfe", "accent": "#3b82f6"},
    "complete":  {"fill": "#0d2b1c", "edge": "#22c55e", "text": "#bbf7d0", "accent": "#22c55e"},
}

LABEL = {
    "locked": "LOCKED",
    "available": "READY TO START",
    "active": "IN PROGRESS",
    "complete": "COMPLETE",
}

NODE_W, NODE_H = 236, 112
H_GAP, V_GAP = 108, 34
PAD = 46
TITLE_H = 74

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(f"{FONT_DIR}/{name}", size)
    except OSError:
        return ImageFont.load_default()


F_TITLE = _font("DejaVuSans-Bold.ttf", 26)
F_NAME = _font("DejaVuSans-Bold.ttf", 15)
F_SMALL = _font("DejaVuSans.ttf", 11)
F_TAG = _font("DejaVuSans-Bold.ttf", 9)


# --- layout ----------------------------------------------------------------

def compute_layers(nodes: list[dict], edges: list[tuple[str, str]]) -> dict[str, int]:
    """edges are (prerequisite_key, dependent_key). Returns key -> column index."""
    prereqs: dict[str, list[str]] = {n["key"]: [] for n in nodes}
    for src, dst in edges:
        if dst in prereqs and src in prereqs:
            prereqs[dst].append(src)

    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def walk(k: str) -> int:
        if k in depth:
            return depth[k]
        if k in visiting:          # cycle guard
            return 0
        visiting.add(k)
        d = 0 if not prereqs[k] else 1 + max(walk(p) for p in prereqs[k])
        visiting.discard(k)
        depth[k] = d
        return d

    for n in nodes:
        walk(n["key"])
    return depth


def order_columns(nodes, edges, depth) -> dict[str, tuple[int, int]]:
    """Returns key -> (column, row), barycenter-ordered to reduce crossings."""
    cols: dict[int, list[str]] = {}
    for n in nodes:
        cols.setdefault(depth[n["key"]], []).append(n["key"])

    prereqs: dict[str, list[str]] = {n["key"]: [] for n in nodes}
    for src, dst in edges:
        if dst in prereqs and src in prereqs:
            prereqs[dst].append(src)

    pos: dict[str, tuple[int, int]] = {}
    for c in sorted(cols):
        keys = cols[c]
        if c > 0:
            def bary(k):
                ps = [pos[p][1] for p in prereqs[k] if p in pos]
                return sum(ps) / len(ps) if ps else 99
            keys.sort(key=bary)
        for r, k in enumerate(keys):
            pos[k] = (c, r)
    return pos


# --- drawing ---------------------------------------------------------------

def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and draw.textlength(lines[-1], font=font) > max_w - 12:
        lines[-1] = lines[-1][:26] + "…"
    return lines


def render_tree(
    nodes: list[dict],
    edges: Iterable[tuple[str, str]],
    title: str = "Tech Tree",
) -> io.BytesIO:
    """nodes: [{key, name, state, pct, xp, unlocks}]  edges: [(prereq_key, key)]"""
    edges = list(edges)
    if not nodes:
        nodes = [{"key": "_", "name": "No milestones yet", "state": "locked",
                  "pct": 0, "xp": 0, "unlocks": "Add one with /tree add"}]
        edges = []

    depth = compute_layers(nodes, edges)
    pos = order_columns(nodes, edges, depth)
    by_key = {n["key"]: n for n in nodes}

    n_cols = max(c for c, _ in pos.values()) + 1
    n_rows = max(r for _, r in pos.values()) + 1
    width = PAD * 2 + n_cols * NODE_W + (n_cols - 1) * H_GAP
    height = TITLE_H + PAD * 2 + n_rows * NODE_H + (n_rows - 1) * V_GAP

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    for x in range(0, width, 34):
        d.line([(x, 0), (x, height)], fill=GRID, width=1)
    for y in range(0, height, 34):
        d.line([(0, y), (width, y)], fill=GRID, width=1)

    d.text((PAD, 26), title, font=F_TITLE, fill="#e6edf3")
    own = [n for n in nodes if not n.get("external_from")]
    done = sum(1 for n in own if n["state"] == "complete")
    d.text((width - PAD - 190, 33), f"{done}/{len(own)} milestones unlocked",
           font=F_SMALL, fill="#8b949e")

    def box(k) -> tuple[int, int, int, int]:
        c, r = pos[k]
        x0 = PAD + c * (NODE_W + H_GAP)
        y0 = TITLE_H + PAD // 2 + r * (NODE_H + V_GAP)
        return x0, y0, x0 + NODE_W, y0 + NODE_H

    # edges first, so nodes sit on top
    for src, dst in edges:
        if src not in pos or dst not in pos:
            continue
        sx0, sy0, sx1, sy1 = box(src)
        dx0, dy0, dx1, dy1 = box(dst)
        ax, ay = sx1, (sy0 + sy1) // 2
        bx, by = dx0, (dy0 + dy1) // 2
        mid = ax + (bx - ax) // 2
        lit = by_key[src]["state"] == "complete"
        colour = "#22c55e" if lit else "#30363d"
        w = 3 if lit else 2
        segs = [((ax, ay), (mid, ay)), ((mid, ay), (mid, by)), ((mid, by), (bx, by))]
        for (p, q) in segs:
            if lit:
                d.line([p, q], fill=colour, width=w)
            else:                                   # dashed for not-yet-earned paths
                x1, y1 = p
                x2, y2 = q
                steps = max(abs(x2 - x1), abs(y2 - y1)) // 9 or 1
                for i in range(steps):
                    if i % 2:
                        continue
                    t0, t1 = i / steps, min((i + 0.9) / steps, 1)
                    d.line([(x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0),
                            (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1)],
                           fill=colour, width=w)
        d.ellipse([bx - 5, by - 5, bx + 5, by + 5], fill=colour)

    for n in nodes:
        k = n["key"]
        t = THEME.get(n["state"], THEME["locked"])
        x0, y0, x1, y1 = box(k)
        ext = n.get("external_from")

        if n["state"] == "available" and not ext:   # glow for the actionable ones
            for g in range(7, 0, -2):
                d.rounded_rectangle([x0 - g, y0 - g, x1 + g, y1 + g], radius=14 + g,
                                    outline="#5a4408", width=1)

        border = 1 if ext else (3 if n["state"] != "locked" else 2)
        d.rounded_rectangle([x0, y0, x1, y1], radius=13, fill=t["fill"],
                            outline=t["edge"], width=border)

        tag = f"FROM {ext.upper()}"[:18] if ext else LABEL[n["state"]]
        tw = d.textlength(tag, font=F_TAG)
        d.rounded_rectangle([x0 + 12, y0 + 10, x0 + 24 + tw, y0 + 26], radius=6,
                            fill="#30363d" if ext else t["accent"])
        d.text((x0 + 18, y0 + 14), tag, font=F_TAG,
               fill=t["text"] if ext else "#0e1116")

        if n.get("xp"):
            xp = f"{n['xp']} XP"
            d.text((x1 - 12 - d.textlength(xp, font=F_TAG), y0 + 15), xp,
                   font=F_TAG, fill=t["text"])

        for i, line in enumerate(_wrap(d, n["name"], F_NAME, NODE_W - 26, 2)):
            d.text((x0 + 13, y0 + 34 + i * 18), line, font=F_NAME, fill=t["text"])

        bar_y = y1 - 30
        d.rounded_rectangle([x0 + 13, bar_y, x1 - 13, bar_y + 8], radius=4, fill="#21262d")
        pct = max(0, min(100, int(n.get("pct", 0))))
        if pct:
            fill_w = int((NODE_W - 26) * pct / 100)
            d.rounded_rectangle([x0 + 13, bar_y, x0 + 13 + max(fill_w, 8), bar_y + 8],
                                radius=4, fill=t["accent"])
        sub = n.get("unlocks") or ""
        foot = f"{pct}%" + (f"  ·  {sub}" if sub else "")
        d.text((x0 + 13, bar_y + 13), _wrap(d, foot, F_SMALL, NODE_W - 26, 1)[0],
               font=F_SMALL, fill="#8b949e")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


if __name__ == "__main__":
    demo_nodes = [
        {"key": "scope", "name": "Feasibility scope signed", "state": "complete", "pct": 100, "xp": 150, "unlocks": "opens funder talks"},
        {"key": "partner", "name": "Anchor partner MOU", "state": "complete", "pct": 100, "xp": 200, "unlocks": "site access"},
        {"key": "demand", "name": "Demand validation", "state": "active", "pct": 60, "xp": 250, "unlocks": ""},
        {"key": "budget", "name": "Budget gap closed", "state": "available", "pct": 0, "xp": 200, "unlocks": ""},
        {"key": "grant", "name": "CFA application filed", "state": "locked", "pct": 0, "xp": 400, "unlocks": "state funding"},
        {"key": "build", "name": "Site build-out", "state": "locked", "pct": 0, "xp": 800, "unlocks": "cohort one"},
    ]
    demo_edges = [("scope", "demand"), ("scope", "budget"), ("partner", "demand"),
                  ("demand", "grant"), ("budget", "grant"), ("grant", "build")]
    out = render_tree(demo_nodes, demo_edges, "Demo Tech Tree")
    open("demo_tree.png", "wb").write(out.read())
    print("wrote demo_tree.png")
