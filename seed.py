"""Bulk-load projects, trees, and milestones from YAML, JSON, or a spreadsheet export.

Typing twenty slash commands to stand up a tree is miserable and error-prone.
Write the structure once, run this, done.

    python seed.py my_tree.yaml --guild 123456789012345678
    python seed.py my_tree.csv  --guild 123456789012345678
    python seed.py my_plan.json --guild 123456789012345678

For the spreadsheet route, use these column headers:

    tree, milestone, description, unlocks, requires, xp, auto_close

One row per milestone. Put semicolons between multiple prerequisites.

Re-running is safe: everything upserts by key, so edit the file and run it again
to push changes. Nothing is ever deleted — remove things with the slash commands.

A `requires:` entry naming something the file doesn't define becomes a stub, so
you can sketch a tree top-down and fill the gaps in later passes.

Get your guild ID from Discord: Settings > Advanced > Developer Mode, then
right-click the server icon > Copy Server ID.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import yaml

import db

CSV_COLUMNS = ["tree", "milestone", "description", "unlocks", "requires", "xp", "auto_close"]


def csv_to_doc(source) -> dict:
    """A spreadsheet export becomes the same structure as the YAML format.

    One row per milestone. `requires` holds semicolon-separated names. Anything
    named there but not defined in the file becomes a stub, same as in YAML.
    """
    trees: dict[str, dict] = {}
    loose: list[dict] = []
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8-sig")
    else:
        text = source.lstrip("\ufeff")
    problems: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        known = {c.strip().lower() for c in reader.fieldnames if c}
        if "milestone" not in known and "name" not in known:
            problems.append("No `milestone` column found — check the header row.")
    for i, row in enumerate(reader, start=2):
        if True:
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in row.items() if k}
            name = row.get("milestone") or row.get("name")
            if not name:
                continue
            spec = {
                "key": name.lower().replace(" ", "-")[:20],
                "name": name,
                "description": row.get("description", ""),
                "unlocks": row.get("unlocks", ""),
                "requires": [r.strip() for r in
                             row.get("requires", "").replace(";", ",").split(",") if r.strip()],
            }
            if row.get("xp"):
                try:
                    spec["xp"] = int(row["xp"])
                except ValueError:
                    problems.append(f"row {i}: xp '{row['xp']}' isn't a number, using 100")
            if row.get("auto_close"):
                spec["auto_close"] = row["auto_close"].lower() not in ("false", "no", "0", "n")
            for col, key in (("group", "grp"), ("region", "region"), ("team", "team")):
                if row.get(col, "").strip():
                    spec[key] = row[col].strip()
            if row.get("difficulty", "").strip():
                try:
                    spec["difficulty"] = float(row["difficulty"])
                except ValueError:
                    problems.append(f"row {i}: difficulty '{row['difficulty']}' isn't a number")
            if row.get("private", "").strip():
                spec["private"] = row["private"].lower() not in ("false", "no", "0", "n", "")

            tname = row.get("tree", "").strip()
            if tname:
                t = trees.setdefault(tname, {
                    "key": tname.lower().replace(" ", "-")[:20],
                    "name": tname,
                    "milestones": [],
                })
                t["milestones"].append(spec)
            else:
                loose.append(spec)
    return {"trees": list(trees.values()), "milestones": loose, "problems": problems}


def upsert_project(guild_id: int, spec: dict, owner: int) -> int:
    name = spec["name"]
    existing = db.get_project(guild_id, name)
    if existing:
        db.update_project(existing["id"], description=spec.get("description"))
        if "difficulty" in spec:
            db.set_project_difficulty(existing["id"], spec["difficulty"])
        if any(key in spec for key in ("grp", "region", "team")):
            db.set_project_tags(existing["id"], grp=spec.get("grp"),
                                region=spec.get("region"), team=spec.get("team"))
        return existing["id"]
    pid = db.create_project(guild_id, name, spec.get("description", ""), owner,
                            spec.get("difficulty", 1))
    if any(key in spec for key in ("grp", "region", "team")):
        db.set_project_tags(pid, grp=spec.get("grp"), region=spec.get("region"),
                            team=spec.get("team"))
    return pid


def upsert_tree(guild_id: int, spec: dict) -> int:
    key = spec["key"]
    existing = db.get_tree(guild_id, key)
    if existing:
        db.update_tree(existing["id"], name=spec.get("name"),
                       description=spec.get("description"))
        if any(key in spec for key in ("grp", "region", "team")):
            db.set_tree_tags(existing["id"], grp=spec.get("grp"),
                             region=spec.get("region"), team=spec.get("team"))
        return existing["id"]
    tid = db.create_tree(guild_id, key, spec.get("name", key),
                         spec.get("description", ""))
    if any(key in spec for key in ("grp", "region", "team")):
        db.set_tree_tags(tid, grp=spec.get("grp"), region=spec.get("region"),
                         team=spec.get("team"))
    return tid


def upsert_milestone(guild_id: int, spec: dict) -> int:
    key = spec["key"]
    existing = db.get_milestone(guild_id, key)
    if existing:
        db.update_milestone(existing["id"], name=spec.get("name"),
                            description=spec.get("description"),
                            unlocks=spec.get("unlocks"), xp=spec.get("xp"),
                            auto_close=None if spec.get("auto_close") is None
                            else int(bool(spec["auto_close"])))
        if any(k in spec for k in ("grp", "region", "team")):
            db.set_milestone_tags(existing["id"], grp=spec.get("grp"),
                                  region=spec.get("region"), team=spec.get("team"))
        if "difficulty" in spec:
            db.set_difficulty(existing["id"], spec["difficulty"])
        if "private" in spec:
            db.set_private(existing["id"], spec["private"])
        return existing["id"]
    mid = db.create_milestone(guild_id, key, spec.get("name", key),
                               spec.get("unlocks", ""), int(spec.get("xp", 100)),
                               spec.get("description", ""),
                               bool(spec.get("auto_close", True)),
                               difficulty=spec.get("difficulty", 1.0),
                               grp=spec.get("grp", "Universal"),
                               region=spec.get("region", "Universal"),
                               team=spec.get("team", "Universal"))
    if spec.get("private"):
        db.set_private(mid, True)
    return mid


def parse(text: str, filename: str) -> dict:
    """Text in, plan structure out. Used by both the CLI and `/tree import`."""
    if filename.lower().endswith((".csv", ".tsv")):
        return csv_to_doc(text)
    doc = json.loads(text) if filename.lower().endswith(".json") else yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ValueError("A plan must be a JSON or YAML object.")
    # JSON and YAML both accept the friendly key `group`.
    specs = list(doc.get("projects", []) or []) + list(doc.get("trees", []) or [])
    specs += [m for tree in doc.get("trees", []) or [] for m in tree.get("milestones", []) or []]
    specs += list(doc.get("milestones", []) or [])
    for spec in specs:
        if isinstance(spec, dict) and "group" in spec and "grp" not in spec:
            spec["grp"] = spec.pop("group")
    return doc


def preview(doc: dict, guild_id: int) -> dict:
    """What applying this would do, without touching anything."""
    specs = [m for t in doc.get("trees", []) for m in t.get("milestones", [])] \
            + doc.get("milestones", [])
    # requires: entries name milestones by display name, so match on both
    defined = {s["key"] for s in specs} | {s.get("name", "").strip().lower() for s in specs}
    new_trees, known_trees = [], []
    for t in doc.get("trees", []):
        (known_trees if db.get_tree(guild_id, t["key"]) else new_trees).append(
            t.get("name", t["key"]))
    new_projects, known_projects, new_tasks, known_tasks = [], [], [], []
    for project in doc.get("projects", []) or []:
        existing = db.get_project(guild_id, project["name"])
        (known_projects if existing else new_projects).append(project["name"])
        existing_titles = {task["title"] for task in db.list_tasks(existing["id"])} if existing else set()
        for task in project.get("tasks", []) or []:
            title = task if isinstance(task, str) else task.get("title", "")
            if title:
                (known_tasks if title in existing_titles else new_tasks).append(title)
    created, updated, stubs = [], [], set()
    for spec in specs:
        existing = db.get_milestone(guild_id, spec["key"])
        (updated if existing else created).append(spec.get("name", spec["key"]))
        for req in spec.get("requires", []) or []:
            low = req.strip().lower()
            if low in defined:
                continue
            if not any(m["key"] == low or req.strip().lower() in m["name"].lower()
                       for m in db.list_milestones(guild_id)):
                stubs.add(req.strip())
    return {"new_trees": new_trees, "known_trees": known_trees,
            "new_projects": new_projects, "known_projects": known_projects,
            "new_tasks": new_tasks, "known_tasks": known_tasks,
            "created": created, "updated": updated, "stubs": sorted(stubs),
            "problems": doc.get("problems", [])}


def apply_doc(doc: dict, guild_id: int, owner: int) -> list[str]:
    log: list[str] = []

    # projects first — milestones and tasks both point at them
    for spec in doc.get("projects", []):
        pid = upsert_project(guild_id, spec, owner)
        log.append(f"project  {spec['name']}")
        existing_tasks = {task["title"]: task for task in db.list_tasks(pid)}
        for t in spec.get("tasks", []):
            if isinstance(t, str):
                t = {"title": t}
            try:
                assignee = int(t["assignee"]) if t.get("assignee") else None
                weight = max(1, min(20, int(t.get("weight", 1))))
            except (TypeError, ValueError):
                assignee, weight = None, 1
            due = t.get("due") or None
            existing = existing_tasks.get(t["title"])
            if existing:
                db.update_task_details(existing["id"], assignee, due, weight)
                log.append(f"  task   {t['title']} (updated)")
            else:
                db.add_task(pid, t["title"], assignee, due, weight)
                log.append(f"  task   {t['title']}")

    for spec in doc.get("trees", []):
        tid = upsert_tree(guild_id, spec)
        log.append(f"tree     {spec['key']}")
        for m in spec.get("milestones", []):
            mid = upsert_milestone(guild_id, m)
            db.add_to_tree(tid, mid)
            log.append(f"  node   {m['key']}  ({m.get('xp', 100)} XP)")

    # standalone milestones that belong to no tree
    for m in doc.get("milestones", []):
        upsert_milestone(guild_id, m)
        log.append(f"node     {m['key']} (unfiled)")

    # second pass: dependencies and project links need every milestone to exist
    all_specs = [m for t in doc.get("trees", []) for m in t.get("milestones", [])]
    all_specs += doc.get("milestones", [])
    for m in all_specs:
        node = db.get_milestone(guild_id, m["key"])
        for req in m.get("requires", []) or []:
            pre_id, stubbed = db.find_or_stub(guild_id, req)
            if not db.add_dep(node["id"], pre_id):
                log.append(f"  !! {m['key']} <- {req} skipped: would create a loop")
                continue
            log.append(f"  gate   {m['key']} <- {req}" + ("  (stubbed)" if stubbed else ""))
        for proj in m.get("projects", []) or []:
            p = db.get_project(guild_id, proj)
            if p is None:
                log.append(f"  !! {m['key']} links unknown project '{proj}'")
                continue
            db.link_project(node["id"], p["id"])
            log.append(f"  link   {m['key']} <- {proj}")

    return log


def load(path: Path, guild_id: int, owner: int, dry_run: bool = False) -> None:
    doc = parse(path.read_text(encoding="utf-8-sig"), path.name)
    for p in doc.get("problems", []):
        print(f"  !! {p}")
    log = apply_doc(doc, guild_id, owner)
    print("\n".join(log))
    print(f"\n{len(log)} item(s) applied.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path, help="a .yaml, .json, or .csv plan")
    ap.add_argument("--guild", type=int, required=True, help="Discord server ID")
    ap.add_argument("--owner", type=int, default=0,
                    help="Discord user ID recorded as project owner")
    ap.add_argument("--db", type=Path, default=None, help="Path to tracker.db")
    args = ap.parse_args()

    if not args.file.exists():
        sys.exit(f"No such file: {args.file}")
    db.connect(args.db or db.DB_PATH)
    load(args.file, args.guild, args.owner)


if __name__ == "__main__":
    main()
