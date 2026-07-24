"""SQLite storage layer for the Discord project tracker.

Everything is scoped by guild_id so one bot instance can serve many servers
without projects bleeding across them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).with_name("tracker.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'active',   -- active | archived
    owner_id    INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'todo',    -- todo | doing | blocked | done
    assignee_id  INTEGER,
    due_date     TEXT,                               -- ISO date, nullable
    weight       INTEGER NOT NULL DEFAULT 1,         -- effort weighting for % complete
    created_at   TEXT    NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    guild_id       INTEGER PRIMARY KEY,
    digest_channel INTEGER,
    digest_weekday INTEGER NOT NULL DEFAULT 0,   -- 0 = Monday
    digest_hour    INTEGER NOT NULL DEFAULT 9,   -- UTC
    last_digest    TEXT
);

-- ---- tech tree --------------------------------------------------------
CREATE TABLE IF NOT EXISTS milestones (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    key          TEXT    NOT NULL,              -- short slug used in commands
    name         TEXT    NOT NULL,
    unlocks      TEXT    NOT NULL DEFAULT '',   -- what becomes possible
    xp           INTEGER NOT NULL DEFAULT 100,
    completed_at TEXT,
    settled      INTEGER NOT NULL DEFAULT 0,    -- XP already minted?
    UNIQUE (guild_id, key)
);

CREATE TABLE IF NOT EXISTS milestone_deps (
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    requires_id  INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    PRIMARY KEY (milestone_id, requires_id)
);

CREATE TABLE IF NOT EXISTS milestone_projects (
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    project_id   INTEGER NOT NULL REFERENCES projects(id)   ON DELETE CASCADE,
    PRIMARY KEY (milestone_id, project_id)
);

CREATE TABLE IF NOT EXISTS credit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    milestone_id INTEGER REFERENCES milestones(id) ON DELETE CASCADE,
    xp           INTEGER NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE (guild_id, key)
);

CREATE TABLE IF NOT EXISTS tree_members (
    tree_id      INTEGER NOT NULL REFERENCES trees(id)      ON DELETE CASCADE,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    PRIMARY KEY (tree_id, milestone_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_projects_guild ON projects(guild_id);
CREATE INDEX IF NOT EXISTS idx_credit_guild ON credit(guild_id, user_id);
"""

_conn: Optional[sqlite3.Connection] = None

VALID_STATUSES = ("todo", "doing", "blocked", "done")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()
    return _conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.connect() must be called before use")
    return _conn


def _q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    cur = conn().execute(sql, args)
    conn().commit()
    return cur


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------

def create_project(guild_id: int, name: str, description: str, owner_id: int) -> int:
    cur = _exec(
        "INSERT INTO projects (guild_id, name, description, owner_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, name, description, owner_id, now()),
    )
    return cur.lastrowid


def get_project(guild_id: int, name: str) -> Optional[sqlite3.Row]:
    rows = _q(
        "SELECT * FROM projects WHERE guild_id = ? AND name = ? COLLATE NOCASE",
        (guild_id, name),
    )
    return rows[0] if rows else None


def list_projects(guild_id: int, include_archived: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM projects WHERE guild_id = ?"
    if not include_archived:
        sql += " AND status = 'active'"
    sql += " ORDER BY name COLLATE NOCASE"
    return _q(sql, (guild_id,))


def set_project_status(project_id: int, status: str) -> None:
    _exec("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))


def rename_project(project_id: int, name: str) -> None:
    _exec("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))


def delete_project(project_id: int) -> None:
    _exec("DELETE FROM projects WHERE id = ?", (project_id,))


def progress(project_id: int) -> dict[str, Any]:
    """Weighted completion plus a per-status task count."""
    row = _q(
        "SELECT COALESCE(SUM(weight), 0) AS total,"
        "       COALESCE(SUM(CASE WHEN status='done' THEN weight ELSE 0 END), 0) AS done_w,"
        "       COUNT(*) AS n,"
        "       SUM(status='done')    AS n_done,"
        "       SUM(status='doing')   AS n_doing,"
        "       SUM(status='blocked') AS n_blocked,"
        "       SUM(status='todo')    AS n_todo "
        "FROM tasks WHERE project_id = ?",
        (project_id,),
    )[0]
    total = row["total"] or 0
    pct = round(100 * (row["done_w"] or 0) / total) if total else 0
    return {
        "pct": pct,
        "count": row["n"] or 0,
        "done": row["n_done"] or 0,
        "doing": row["n_doing"] or 0,
        "blocked": row["n_blocked"] or 0,
        "todo": row["n_todo"] or 0,
    }


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def add_task(
    project_id: int,
    title: str,
    assignee_id: Optional[int] = None,
    due_date: Optional[str] = None,
    weight: int = 1,
) -> int:
    cur = _exec(
        "INSERT INTO tasks (project_id, title, assignee_id, due_date, weight, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, title, assignee_id, due_date, weight, now()),
    )
    return cur.lastrowid


def get_task(guild_id: int, task_id: int) -> Optional[sqlite3.Row]:
    rows = _q(
        "SELECT t.*, p.name AS project_name, p.guild_id "
        "FROM tasks t JOIN projects p ON p.id = t.project_id "
        "WHERE t.id = ? AND p.guild_id = ?",
        (task_id, guild_id),
    )
    return rows[0] if rows else None


def list_tasks(
    project_id: int,
    status: Optional[str] = None,
    assignee_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM tasks WHERE project_id = ?"
    args: list[Any] = [project_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    if assignee_id:
        sql += " AND assignee_id = ?"
        args.append(assignee_id)
    sql += (
        " ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'doing' THEN 1 "
        "WHEN 'todo' THEN 2 ELSE 3 END, "
        "due_date IS NULL, due_date, id"
    )
    return _q(sql, tuple(args))


def set_task_status(task_id: int, status: str) -> None:
    completed = now() if status == "done" else None
    _exec(
        "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
        (status, completed, task_id),
    )


def assign_task(task_id: int, assignee_id: Optional[int]) -> None:
    _exec("UPDATE tasks SET assignee_id = ? WHERE id = ?", (assignee_id, task_id))


def delete_task(task_id: int) -> None:
    _exec("DELETE FROM tasks WHERE id = ?", (task_id,))


def my_tasks(guild_id: int, user_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT t.*, p.name AS project_name FROM tasks t "
        "JOIN projects p ON p.id = t.project_id "
        "WHERE p.guild_id = ? AND t.assignee_id = ? AND t.status != 'done' "
        "AND p.status = 'active' "
        "ORDER BY t.due_date IS NULL, t.due_date",
        (guild_id, user_id),
    )


def overdue_tasks(guild_id: int, today: str) -> list[sqlite3.Row]:
    return _q(
        "SELECT t.*, p.name AS project_name FROM tasks t "
        "JOIN projects p ON p.id = t.project_id "
        "WHERE p.guild_id = ? AND p.status = 'active' AND t.status != 'done' "
        "AND t.due_date IS NOT NULL AND t.due_date < ? "
        "ORDER BY t.due_date",
        (guild_id, today),
    )


# --------------------------------------------------------------------------
# activity log
# --------------------------------------------------------------------------

def add_log(project_id: int, author_id: int, body: str) -> None:
    _exec(
        "INSERT INTO log (project_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
        (project_id, author_id, body, now()),
    )


def recent_log(project_id: int, limit: int = 5) -> list[sqlite3.Row]:
    return _q(
        "SELECT * FROM log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    )


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_settings(guild_id: int) -> sqlite3.Row:
    rows = _q("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
    if not rows:
        _exec("INSERT INTO settings (guild_id) VALUES (?)", (guild_id,))
        rows = _q("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
    return rows[0]


def set_digest(guild_id: int, channel_id: int, weekday: int, hour: int) -> None:
    get_settings(guild_id)
    _exec(
        "UPDATE settings SET digest_channel = ?, digest_weekday = ?, digest_hour = ? "
        "WHERE guild_id = ?",
        (channel_id, weekday, hour, guild_id),
    )


def mark_digest_sent(guild_id: int) -> None:
    _exec("UPDATE settings SET last_digest = ? WHERE guild_id = ?", (now(), guild_id))


def all_digest_guilds() -> list[sqlite3.Row]:
    return _q("SELECT * FROM settings WHERE digest_channel IS NOT NULL")


# --------------------------------------------------------------------------
# tech tree: milestones, dependencies, and earned XP
# --------------------------------------------------------------------------

def create_milestone(guild_id: int, key: str, name: str, unlocks: str = "", xp: int = 100) -> int:
    cur = _exec(
        "INSERT INTO milestones (guild_id, key, name, unlocks, xp) VALUES (?, ?, ?, ?, ?)",
        (guild_id, key.lower(), name, unlocks, xp),
    )
    return cur.lastrowid


def get_milestone(guild_id: int, key: str) -> Optional[sqlite3.Row]:
    rows = _q(
        "SELECT * FROM milestones WHERE guild_id = ? AND key = ? COLLATE NOCASE",
        (guild_id, key),
    )
    return rows[0] if rows else None


def list_milestones(guild_id: int) -> list[sqlite3.Row]:
    return _q("SELECT * FROM milestones WHERE guild_id = ? ORDER BY id", (guild_id,))


def delete_milestone(mid: int) -> None:
    _exec("DELETE FROM milestones WHERE id = ?", (mid,))


def add_dep(milestone_id: int, requires_id: int) -> None:
    _exec(
        "INSERT OR IGNORE INTO milestone_deps (milestone_id, requires_id) VALUES (?, ?)",
        (milestone_id, requires_id),
    )


def remove_dep(milestone_id: int, requires_id: int) -> None:
    _exec(
        "DELETE FROM milestone_deps WHERE milestone_id = ? AND requires_id = ?",
        (milestone_id, requires_id),
    )


def deps(guild_id: int) -> list[tuple[str, str]]:
    """Returns (prerequisite_key, dependent_key) pairs."""
    rows = _q(
        "SELECT r.key AS src, m.key AS dst FROM milestone_deps d "
        "JOIN milestones m ON m.id = d.milestone_id "
        "JOIN milestones r ON r.id = d.requires_id "
        "WHERE m.guild_id = ?",
        (guild_id,),
    )
    return [(r["src"], r["dst"]) for r in rows]


def link_project(milestone_id: int, project_id: int) -> None:
    _exec(
        "INSERT OR IGNORE INTO milestone_projects (milestone_id, project_id) VALUES (?, ?)",
        (milestone_id, project_id),
    )


def unlink_project(milestone_id: int, project_id: int) -> None:
    _exec(
        "DELETE FROM milestone_projects WHERE milestone_id = ? AND project_id = ?",
        (milestone_id, project_id),
    )


def milestone_projects(milestone_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT p.* FROM milestone_projects mp JOIN projects p ON p.id = mp.project_id "
        "WHERE mp.milestone_id = ?",
        (milestone_id,),
    )


def milestone_progress(milestone_id: int) -> dict[str, Any]:
    """Weighted completion across every project linked to this milestone."""
    row = _q(
        "SELECT COALESCE(SUM(t.weight), 0) AS total,"
        "       COALESCE(SUM(CASE WHEN t.status='done' THEN t.weight ELSE 0 END), 0) AS done_w,"
        "       COUNT(t.id) AS n,"
        "       SUM(t.status != 'done') AS remaining "
        "FROM milestone_projects mp "
        "LEFT JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE mp.milestone_id = ?",
        (milestone_id,),
    )[0]
    total = row["total"] or 0
    return {
        "pct": round(100 * (row["done_w"] or 0) / total) if total else 0,
        "tasks": row["n"] or 0,
        "remaining": row["remaining"] or 0,
    }


def tree_state(guild_id: int) -> list[dict[str, Any]]:
    """Every milestone with a derived state: locked / available / active / complete.

    A milestone is complete when all of its linked projects are fully done (or it
    was closed by hand). It is available once every prerequisite is complete.
    """
    ms = list_milestones(guild_id)
    by_key = {m["key"]: m for m in ms}
    prereqs: dict[str, list[str]] = {m["key"]: [] for m in ms}
    for src, dst in deps(guild_id):
        prereqs[dst].append(src)

    prog = {m["key"]: milestone_progress(m["id"]) for m in ms}
    complete = {
        m["key"]: bool(m["completed_at"]) or (prog[m["key"]]["tasks"] > 0 and prog[m["key"]]["pct"] == 100)
        for m in ms
    }

    out = []
    for m in ms:
        k = m["key"]
        gate_open = all(complete.get(p, False) for p in prereqs[k])
        if complete[k]:
            state = "complete"
        elif not gate_open:
            state = "locked"
        elif prog[k]["pct"] > 0:
            state = "active"
        else:
            state = "available"
        out.append({
            "id": m["id"], "key": k, "name": m["name"], "unlocks": m["unlocks"],
            "xp": m["xp"], "state": state, "pct": 100 if complete[k] else prog[k]["pct"],
            "remaining": prog[k]["remaining"], "prereqs": prereqs[k],
            "blocked_by": [p for p in prereqs[k] if not complete.get(p, False)],
            "settled": bool(m["settled"]),
        })
    return out


def contributors(milestone_id: int) -> dict[int, int]:
    """user_id -> summed weight of tasks they completed under this milestone."""
    rows = _q(
        "SELECT t.assignee_id AS uid, SUM(t.weight) AS w "
        "FROM milestone_projects mp JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE mp.milestone_id = ? AND t.status = 'done' AND t.assignee_id IS NOT NULL "
        "GROUP BY t.assignee_id",
        (milestone_id,),
    )
    return {r["uid"]: r["w"] for r in rows}


def settle_milestone(guild_id: int, milestone_id: int, xp: int) -> dict[int, int]:
    """Mint XP once, split across contributors by effort. Returns user_id -> xp."""
    m = _q("SELECT * FROM milestones WHERE id = ?", (milestone_id,))[0]
    if m["settled"]:
        return {}
    shares = contributors(milestone_id)
    total_w = sum(shares.values())
    awards: dict[int, int] = {}
    if total_w:
        for uid, w in shares.items():
            awards[uid] = max(1, round(xp * w / total_w))
            _exec(
                "INSERT INTO credit (guild_id, user_id, milestone_id, xp, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, uid, milestone_id, awards[uid], now()),
            )
    _exec(
        "UPDATE milestones SET settled = 1, completed_at = COALESCE(completed_at, ?) WHERE id = ?",
        (now(), milestone_id),
    )
    return awards


def complete_milestone(milestone_id: int) -> None:
    _exec("UPDATE milestones SET completed_at = ? WHERE id = ?", (now(), milestone_id))


def leaderboard(guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return _q(
        "SELECT user_id, SUM(xp) AS xp, COUNT(DISTINCT milestone_id) AS unlocks "
        "FROM credit WHERE guild_id = ? GROUP BY user_id ORDER BY xp DESC LIMIT ?",
        (guild_id, limit),
    )


# --------------------------------------------------------------------------
# named trees: views over the milestone graph
# --------------------------------------------------------------------------
# A milestone can sit in several trees at once. That matters when one gate —
# "funding secured", say — blocks work in more than one initiative. Trees are
# filters over a single shared graph, not separate graphs.

def create_tree(guild_id: int, key: str, name: str, description: str = "") -> int:
    cur = _exec(
        "INSERT INTO trees (guild_id, key, name, description, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, key.lower(), name, description, now()),
    )
    return cur.lastrowid


def get_tree(guild_id: int, key: str) -> Optional[sqlite3.Row]:
    rows = _q(
        "SELECT * FROM trees WHERE guild_id = ? AND key = ? COLLATE NOCASE",
        (guild_id, key),
    )
    return rows[0] if rows else None


def list_trees(guild_id: int) -> list[sqlite3.Row]:
    return _q("SELECT * FROM trees WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
              (guild_id,))


def delete_tree(tree_id: int) -> None:
    """Removes the view. Milestones themselves survive."""
    _exec("DELETE FROM trees WHERE id = ?", (tree_id,))


def add_to_tree(tree_id: int, milestone_id: int) -> None:
    _exec(
        "INSERT OR IGNORE INTO tree_members (tree_id, milestone_id) VALUES (?, ?)",
        (tree_id, milestone_id),
    )


def remove_from_tree(tree_id: int, milestone_id: int) -> None:
    _exec(
        "DELETE FROM tree_members WHERE tree_id = ? AND milestone_id = ?",
        (tree_id, milestone_id),
    )


def tree_members(tree_id: int) -> set[str]:
    return {
        r["key"] for r in _q(
            "SELECT m.key FROM tree_members tm JOIN milestones m ON m.id = tm.milestone_id "
            "WHERE tm.tree_id = ?",
            (tree_id,),
        )
    }


def trees_for_milestone(milestone_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT t.* FROM tree_members tm JOIN trees t ON t.id = tm.tree_id "
        "WHERE tm.milestone_id = ?",
        (milestone_id,),
    )


def unfiled_milestones(guild_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT m.* FROM milestones m "
        "LEFT JOIN tree_members tm ON tm.milestone_id = m.id "
        "WHERE m.guild_id = ? AND tm.tree_id IS NULL ORDER BY m.id",
        (guild_id,),
    )


def tree_view(guild_id: int, tree_key: Optional[str] = None) -> list[dict[str, Any]]:
    """State for one named tree, or the whole guild when tree_key is None.

    Prerequisites that live outside the requested tree are still returned, marked
    `external`, so a gate never disappears just because it belongs to another
    initiative.
    """
    everything = tree_state(guild_id)
    if tree_key is None:
        return everything

    t = get_tree(guild_id, tree_key)
    if t is None:
        return []
    members = tree_members(t["id"])
    by_key = {n["key"]: n for n in everything}

    externals: set[str] = set()
    for k in members:
        for p in by_key.get(k, {}).get("prereqs", []):
            if p not in members:
                externals.add(p)

    out = []
    for n in everything:
        if n["key"] not in members and n["key"] not in externals:
            continue
        node = dict(n)
        if n["key"] in externals:
            src = trees_for_milestone(n["id"])
            node["external_from"] = src[0]["key"] if src else "unfiled"
        else:
            node["external_from"] = None
        out.append(node)
    return out


def tree_edges(guild_id: int, nodes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Dependency edges restricted to the given node set."""
    visible = {n["key"] for n in nodes}
    return [(s, d) for s, d in deps(guild_id) if s in visible and d in visible]


def tree_summary(guild_id: int) -> list[dict[str, Any]]:
    """One progress line per tree, for /tree list."""
    out = []
    for t in list_trees(guild_id):
        nodes = [n for n in tree_view(guild_id, t["key"]) if not n["external_from"]]
        done = sum(1 for n in nodes if n["state"] == "complete")
        ready = [n["name"] for n in nodes if n["state"] in ("available", "active")]
        out.append({
            "key": t["key"], "name": t["name"], "description": t["description"],
            "total": len(nodes), "done": done,
            "pct": round(100 * done / len(nodes)) if nodes else 0,
            "ready": ready,
        })
    return out


def update_milestone(milestone_id: int, **fields: Any) -> None:
    """Patch name / unlocks / xp on an existing milestone."""
    allowed = {"name", "unlocks", "xp"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    _exec(f"UPDATE milestones SET {clause} WHERE id = ?", (*sets.values(), milestone_id))


def update_tree(tree_id: int, **fields: Any) -> None:
    allowed = {"name", "description"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    _exec(f"UPDATE trees SET {clause} WHERE id = ?", (*sets.values(), tree_id))


def update_project(project_id: int, **fields: Any) -> None:
    allowed = {"name", "description"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    _exec(f"UPDATE projects SET {clause} WHERE id = ?", (*sets.values(), project_id))
