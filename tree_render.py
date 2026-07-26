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
BG = "#0b1220"
GRID = "#111c2c"

THEME = {
    "locked":    {"fill": "#161f2d", "edge": "#334155", "text": "#b7c2d1", "accent": "#64748b"},
    "available": {"fill": "#12302d", "edge": "#2dd4bf", "text": "#d9fffa", "accent": "#2dd4bf"},
    "active":    {"fill": "#172554", "edge": "#60a5fa", "text": "#dbeafe", "accent": "#60a5fa"},
    "pending":   {"fill": "#2b1c3c", "edge": "#c084fc", "text": "#f3e8ff", "accent": "#c084fc"},
    "stub":      {"fill": "#1c2532", "edge": "#526276", "text": "#b7c2d1", "accent": "#8191a8"},
    "complete":  {"fill": "#123126", "edge": "#34d399", "text": "#dcfce7", "accent": "#34d399"},
}

LABEL = {
    "locked": "LOCKED",
    "available": "READY TO START",
    "active": "IN PROGRESS",
    "pending": "NEEDS SIGN-OFF",
    "early": "DONE EARLY",
    "complete": "COMPLETE",
}

NODE_W, NODE_H = 292, 190
DUMMY_H = 20                      # cross-axis slot reserved for a routed edge
DUMMY_W = 26
H_GAP, V_GAP = 72, 30
MAX_EDGE = 2400                   # downscale past this so Discord always accepts it
PAD = 40
TITLE_H = 78
MAX_LAYER_ROWS = 4


def _font(bold: bool, size: int):
    names = ("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf")
    roots = ("/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts", "")
    for root in roots:
        for name in names:
            try:
                return ImageFont.truetype(f"{root}/{name}" if root else name, size)
            except OSError:
                pass
    return ImageFont.load_default()


F_TITLE = _font(True, 28)
F_NAME = _font(True, 16)
F_SMALL = _font(False, 12)
F_TAG = _font(True, 10)
F_PILL = _font(False, 10)


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


def plan_layout(nodes, edges):
    """Layered layout with routing lanes for long edges.

    An edge spanning more than one column used to be drawn straight across,
    cutting through whatever nodes sat in between. Here each such edge gets a
    chain of invisible dummy slots, one per intermediate column, so it is ordered
    alongside real nodes and routed around them. Ordering is then refined by
    alternating barycenter sweeps rather than the single downward pass we had.
    """
    depth = compute_layers(nodes, edges)
    layers: dict[int, list[str]] = {}
    for n in nodes:
        layers.setdefault(depth[n["key"]], []).append(n["key"])

    routing: list[tuple[str, str]] = []
    chains: dict[tuple[str, str], list[str]] = {}
    for src, dst in edges:
        if src not in depth or dst not in depth:
            continue
        d0, d1 = depth[src], depth[dst]
        if d1 - d0 <= 1:
            routing.append((src, dst))
            chains[(src, dst)] = []
            continue
        chain, prev = [], src
        for d in range(d0 + 1, d1):
            dk = f"\x00{src}>{dst}@{d}"
            depth[dk] = d
            layers.setdefault(d, []).append(dk)
            routing.append((prev, dk))
            chain.append(dk)
            prev = dk
        routing.append((prev, dst))
        chains[(src, dst)] = chain

    preds: dict[str, list[str]] = {}
    succs: dict[str, list[str]] = {}
    for a, b in routing:
        preds.setdefault(b, []).append(a)
        succs.setdefault(a, []).append(b)

    order = {d: list(ks) for d, ks in layers.items()}
    idx: dict[str, int] = {}

    def reindex():
        for ks in order.values():
            for i, k in enumerate(ks):
                idx[k] = i

    reindex()
    for sweep in range(4):                      # down, up, down, up
        downward = sweep % 2 == 0
        rel = preds if downward else succs
        for d in (sorted(order) if downward else sorted(order, reverse=True)):
            def bary(k):
                ns = [idx[x] for x in rel.get(k, []) if x in idx]
                return sum(ns) / len(ns) if ns else idx[k]
            order[d] = sorted(order[d], key=bary)
            reindex()
    return depth, order, chains


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


def difficulty_label(value) -> str:
    difficulty = max(1.0, min(10.0, float(value or 1)))
    return f"DIFFICULTY {difficulty:g}/10" if difficulty > 1 else ""


def render_tree(
    nodes: list[dict],
    edges: Iterable[tuple[str, str]],
    title: str = "Tech Tree",
    orientation: str = "lr",
) -> io.BytesIO:
    """nodes: [{key, name, state, pct, xp, unlocks}]  edges: [(prereq_key, key)]

    orientation "lr" runs prerequisites left to right; "tb" runs them top to
    bottom, which suits deep narrow trees and reads better on a phone.
    """
    tb = str(orientation).lower() in ("tb", "top-to-bottom", "vertical", "down")
    edges = list(edges)
    if not nodes:
        nodes = [{"key": "_", "name": "No milestones yet", "state": "locked",
                  "pct": 0, "xp": 0, "unlocks": "Add one with /tree add"}]
        edges = []

    depth, order, chains = plan_layout(nodes, edges)
    by_key = {n["key"]: n for n in nodes}
    is_real = lambda k: k in by_key

    # One packing routine serves both orientations: layers advance along the
    # "main" axis, items stack along the "cross" axis. Routing lanes are thin on
    # the cross axis and full width on the main one, whichever those happen to be.
    def size(k):
        if is_real(k):
            return NODE_W, NODE_H
        return (DUMMY_W, NODE_H) if tb else (NODE_W, DUMMY_H)

    origin: dict[str, tuple[int, int]] = {}
    if tb:
        for layer, keys in order.items():
            x = PAD
            y = TITLE_H + PAD // 2 + layer * (NODE_H + V_GAP)
            for k in keys:
                w, _ = size(k)
                origin[k] = (x, y)
                x += w + H_GAP
    else:
        # Dense layers used to form one extremely tall column. Split a large
        # logical layer into at most three visual lanes while preserving every
        # dependency's left-to-right direction.
        lane_cols = min(3, max(1, (max(len(keys) for keys in order.values()) + MAX_LAYER_ROWS - 1)
                               // MAX_LAYER_ROWS))
        for layer, keys in order.items():
            rows = (len(keys) + lane_cols - 1) // lane_cols
            for i, k in enumerate(keys):
                lane, row = divmod(i, rows)
                x = PAD + (layer * lane_cols + lane) * (NODE_W + H_GAP)
                y = TITLE_H + PAD // 2 + row * (NODE_H + V_GAP)
                origin[k] = (x, y)

    def box(k) -> tuple[int, int, int, int]:
        x0, y0 = origin[k]
        w, h = size(k)
        return x0, y0, x0 + w, y0 + h

    width = max(box(k)[2] for k in origin) + PAD
    height = max(box(k)[3] for k in origin) + PAD

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    for x in range(0, width, 48):
        d.line([(x, 0), (x, height)], fill=GRID, width=1)
    for y in range(0, height, 48):
        d.line([(0, y), (width, y)], fill=GRID, width=1)

    d.text((PAD, 26), title, font=F_TITLE, fill="#f1f5f9")
    own = [n for n in nodes if not n.get("external_from")]
    done = sum(1 for n in own if n["state"] == "complete")
    summary = f"{done}/{len(own)} milestones unlocked"
    d.text((width - PAD - d.textlength(summary, font=F_SMALL), 35), summary,
           font=F_SMALL, fill="#8fa1b8")

    def mid_y(k):
        _, y0, _, y1 = box(k)
        return (y0 + y1) // 2

    def mid_x(k):
        x0, _, x1, _ = box(k)
        return (x0 + x1) // 2

    def stroke(p, q, colour, w, dashed):
        if not dashed:
            d.line([p, q], fill=colour, width=w)
            return
        x1, y1 = p
        x2, y2 = q
        steps = max(abs(x2 - x1), abs(y2 - y1)) // 9 or 1
        for i in range(0, steps, 2):
            t0, t1 = i / steps, min((i + 0.9) / steps, 1)
            d.line([(x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0),
                    (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1)],
                   fill=colour, width=w)

    # edges first, so nodes sit on top
    for src, dst in edges:
        if src not in depth or dst not in depth:
            continue
        lit = by_key[src]["state"] == "complete"
        colour = "#22c55e" if lit else "#30363d"
        w = 3 if lit else 2

        if tb:
            waypoints = [(mid_x(src), box(src)[3])]
            for dk in chains.get((src, dst), []):
                _, ly0, _, ly1 = box(dk)
                x = mid_x(dk)
                # straight through the band, so bends only fall in empty gaps
                waypoints += [(x, ly0), (x, ly1)]
            waypoints.append((mid_x(dst), box(dst)[1]))
        else:
            waypoints = [(box(src)[2], mid_y(src))]
            for dk in chains.get((src, dst), []):
                lx0, _, lx1, _ = box(dk)
                y = mid_y(dk)
                waypoints += [(lx0, y), (lx1, y)]
            waypoints.append((box(dst)[0], mid_y(dst)))

        for (ax, ay), (bx, by) in zip(waypoints, waypoints[1:]):
            if tb:
                mid = ay + (by - ay) // 2
                stroke((ax, ay), (ax, mid), colour, w, not lit)
                stroke((ax, mid), (bx, mid), colour, w, not lit)
                stroke((bx, mid), (bx, by), colour, w, not lit)
            else:
                mid = ax + (bx - ax) // 2
                stroke((ax, ay), (mid, ay), colour, w, not lit)
                stroke((mid, ay), (mid, by), colour, w, not lit)
                stroke((mid, by), (bx, by), colour, w, not lit)
        bx, by = waypoints[-1]
        d.ellipse([bx - 5, by - 5, bx + 5, by + 5], fill=colour)

    for n in nodes:
        k = n["key"]
        t = THEME.get(n["state"], THEME["locked"])
        x0, y0, x1, y1 = box(k)
        ext = n.get("external_from")
        stub = n.get("is_stub") and not ext
        if stub:
            t = THEME["stub"]
        inner = NODE_W - 32

        d.rounded_rectangle([x0 + 3, y0 + 5, x1 + 3, y1 + 5], radius=16, fill="#070c14")
        border = 1 if ext else 2
        d.rounded_rectangle([x0, y0, x1, y1], radius=16, fill=t["fill"],
                            outline=t["edge"], width=border)
        d.rounded_rectangle([x0, y0, x0 + 5, y1], radius=3, fill=t["accent"])

        tag = ("NEEDS DEFINING" if stub
               else f"FROM {ext.upper()}"[:18] if ext
               else LABEL["early"] if n.get("out_of_order")
               else LABEL[n["state"]])
        tw = d.textlength(tag, font=F_TAG)
        d.rounded_rectangle([x0 + 16, y0 + 14, x0 + 30 + tw, y0 + 34], radius=8,
                            fill="#30363d" if (ext or stub) else t["accent"])
        d.text((x0 + 23, y0 + 19), tag, font=F_TAG,
               fill=t["text"] if (ext or stub) else "#0e1116")

        if n.get("xp"):
            xp = f"{n['xp']} XP"
            d.text((x1 - 16 - d.textlength(xp, font=F_TAG), y0 + 19), xp,
                   font=F_TAG, fill=t["text"])

        level = difficulty_label(n.get("difficulty"))
        if level:
            d.text((x1 - 16 - d.textlength(level, font=F_TAG), y0 + 39), level,
                   font=F_TAG, fill="#8b9bb0")

        # bottom block is anchored first so nodes stay aligned whatever the text
        people = n.get("people") or []
        bar_y = y1 - (72 if people else 52)

        cur_y = y0 + 47
        for line in _wrap(d, n["name"], F_NAME, inner, 2):
            d.text((x0 + 16, cur_y), line, font=F_NAME, fill=t["text"])
            cur_y += 20

        if n.get("private"):
            desc = "🔒 restricted — details hidden"
        else:
            desc = (n.get("description") or "").strip()
            if stub and not desc:
                desc = "Named as a prerequisite. Run /tree edit to describe it."
        if desc:
            cur_y += 2
            for line in _wrap(d, desc, F_SMALL, inner, 2):
                if cur_y + 14 > bar_y - 4:      # never let it crowd the bar
                    break
                d.text((x0 + 16, cur_y), line, font=F_SMALL, fill="#a6b3c4")
                cur_y += 15

        d.rounded_rectangle([x0 + 16, bar_y, x1 - 16, bar_y + 7], radius=4, fill="#263244")
        pct = max(0, min(100, int(n.get("pct", 0))))
        if pct:
            fill_w = int(inner * pct / 100)
            d.rounded_rectangle([x0 + 16, bar_y, x0 + 16 + max(fill_w, 8), bar_y + 7],
                                radius=4, fill=t["accent"])

        payoff = n.get("closed_label") or n.get("unlocks") or ""
        foot = f"{pct}%" + (f"  ·  {payoff}" if payoff else "")
        d.text((x0 + 16, bar_y + 13), _wrap(d, foot, F_SMALL, inner, 1)[0],
               font=F_SMALL, fill="#a6b3c4")

        # tracking tags in the lower-left corner (only the non-Universal ones)
        tags = [v for v in (n.get("grp"), n.get("region"), n.get("team"))
                if v and v != "Universal"]
        if tags:
            label = " · ".join(tags)
            d.text((x0 + 16, y1 - 17),
                   _wrap(d, label, F_TAG, inner, 1)[0], font=F_TAG, fill="#7f8da1")

        if people:
            px, py = x0 + 16, bar_y + 31
            shown, overflow = people[:3], max(0, len(people) - 3)
            for who in shown:
                who = str(who)
                label = who if len(who) <= 12 else who[:11] + "…"
                w = d.textlength(label, font=F_PILL)
                if px + w + 16 > x1 - 16:
                    overflow += 1
                    continue
                d.rounded_rectangle([px, py, px + w + 12, py + 16], radius=8,
                                    fill="#1d2938", outline=t["accent"], width=1)
                d.text((px + 6, py + 3), label, font=F_PILL, fill=t["text"])
                px += w + 18
            if overflow:
                d.text((px, py + 3), f"+{overflow}", font=F_PILL, fill="#a6b3c4")

    if max(img.size) > MAX_EDGE:            # keep it inside Discord's upload limit
        scale = MAX_EDGE / max(img.size)
        img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


if __name__ == "__main__":
    demo_nodes = [
        {"key": "scope", "name": "Feasibility scope signed", "description": "SOW agreed with both consultants", "state": "complete", "pct": 100, "xp": 150, "unlocks": "opens funder talks", "people": ["Darius", "Alex"]},
        {"key": "partner", "name": "Anchor partner MOU", "description": "Signed letter of intent", "state": "complete", "pct": 100, "xp": 200, "unlocks": "site access", "people": ["Alex"]},
        {"key": "demand", "name": "Demand validation", "description": "Enrollment and placement realism checks", "state": "active", "pct": 60, "xp": 250, "unlocks": "defensible numbers", "people": ["Darius", "Kellea", "Chuck", "Martha"]},
        {"key": "budget", "name": "Budget gap closed", "description": "Identify the remaining match", "state": "available", "pct": 0, "xp": 200, "unlocks": "you can hire", "people": []},
        {"key": "grant", "name": "CFA application filed", "description": "Full packet submitted", "state": "locked", "pct": 0, "xp": 400, "unlocks": "state funding", "people": []},
        {"key": "build", "name": "Site build-out", "description": "", "state": "locked", "pct": 0, "xp": 800, "unlocks": "cohort one", "people": []},
    ]
    demo_edges = [("scope", "demand"), ("scope", "budget"), ("partner", "demand"),
                  ("demand", "grant"), ("budget", "grant"), ("grant", "build")]
    for mode, fname in (("lr", "demo_tree.png"), ("tb", "demo_tree_tb.png")):
        open(fname, "wb").write(
            render_tree(demo_nodes, demo_edges, "Demo Tech Tree", mode).read())
        print("wrote", fname)
