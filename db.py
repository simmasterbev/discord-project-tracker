"""SQLite storage layer for the Discord project tracker.

Everything is scoped by guild_id so one bot instance can serve many servers
without projects bleeding across them.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
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
    grp         TEXT    NOT NULL DEFAULT 'Universal',
    region      TEXT    NOT NULL DEFAULT 'Universal',
    team        TEXT    NOT NULL DEFAULT 'Universal',
    difficulty  REAL    NOT NULL DEFAULT 1,
    last_activity TEXT,
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
    signoff_role   INTEGER,
    layout         TEXT NOT NULL DEFAULT 'lr',   -- lr or tb
    universal_role INTEGER,

    digest_channel INTEGER,
    digest_weekday INTEGER NOT NULL DEFAULT 0,   -- 0 = Monday
    digest_hour    INTEGER NOT NULL DEFAULT 9,   -- UTC
    last_digest    TEXT,

    board_channel  INTEGER,                      -- scheduled tree-image post
    board_weekday  INTEGER NOT NULL DEFAULT 0,
    board_hour     INTEGER NOT NULL DEFAULT 9,
    board_tree     TEXT,                          -- which tree, or all if null
    last_board     TEXT,
    stale_channel  INTEGER,
    stale_days     INTEGER NOT NULL DEFAULT 7
);

-- ---- tech tree --------------------------------------------------------
CREATE TABLE IF NOT EXISTS milestones (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    key          TEXT    NOT NULL,              -- short slug used in commands
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',   -- what this milestone actually is
    unlocks      TEXT    NOT NULL DEFAULT '',   -- what becomes possible
    xp           INTEGER NOT NULL DEFAULT 100,
    completed_at TEXT,
    settled      INTEGER NOT NULL DEFAULT 0,    -- XP already minted?
    auto_close   INTEGER NOT NULL DEFAULT 1,    -- flip to done at 100%, or wait for sign-off?
    pending_notified INTEGER NOT NULL DEFAULT 0,
    is_stub      INTEGER NOT NULL DEFAULT 0,    -- named as a dependency, not yet defined
    completed_by INTEGER,
    credit_ids   TEXT,                          -- explicit even-split credit list
    grp          TEXT NOT NULL DEFAULT 'Universal',
    region       TEXT NOT NULL DEFAULT 'Universal',
    team         TEXT NOT NULL DEFAULT 'Universal',
    difficulty   REAL NOT NULL DEFAULT 1,        -- 1..10, half steps
    private      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT,
    announce_on_close INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS notify_targets (
    guild_id    INTEGER NOT NULL,
    scope       TEXT    NOT NULL,
    scope_id    INTEGER NOT NULL,
    target_kind TEXT    NOT NULL,
    target_id   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, scope, scope_id, target_kind, target_id)
);

CREATE TABLE IF NOT EXISTS project_alerts (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL, -- stale | blocked
    sent_at    TEXT    NOT NULL,
    PRIMARY KEY (project_id, kind)
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
    grp         TEXT    NOT NULL DEFAULT 'Universal',
    region      TEXT    NOT NULL DEFAULT 'Universal',
    team        TEXT    NOT NULL DEFAULT 'Universal',
    project_id  INTEGER,
    UNIQUE (guild_id, key)
);

CREATE TABLE IF NOT EXISTS tree_members (
    tree_id      INTEGER NOT NULL REFERENCES trees(id)      ON DELETE CASCADE,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    PRIMARY KEY (tree_id, milestone_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_projects_guild ON projects(guild_id);
-- ---- levels ------------------------------------------------------------
-- Scaffolding only for now: thresholds, names, and a free-text `perk` that
-- describes what the level is meant to grant. Acting on a perk (granting a
-- Discord role, unlocking a command, whatever) belongs in LEVEL_HOOKS below.
CREATE TABLE IF NOT EXISTS taxonomy (
    guild_id  INTEGER NOT NULL,
    kind      TEXT    NOT NULL,          -- grp | region | team
    value     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, kind, value)
);

CREATE TABLE IF NOT EXISTS cmd_perms (
    guild_id INTEGER NOT NULL,
    command  TEXT    NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, command)
);

CREATE TABLE IF NOT EXISTS milestone_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    author_id    INTEGER NOT NULL,
    body         TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS levels (
    guild_id  INTEGER NOT NULL,
    threshold INTEGER NOT NULL,          -- cumulative XP required
    name      TEXT    NOT NULL,
    perk      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, threshold)
);

CREATE INDEX IF NOT EXISTS idx_credit_guild ON credit(guild_id, user_id);
"""

_conn: Optional[sqlite3.Connection] = None
# every call now runs inside asyncio.to_thread, so a shared connection needs a
# lock: SQLite tolerates cross-thread use with check_same_thread off, but two
# writers interleaving statements does not end well
_lock = threading.RLock()

VALID_STATUSES = ("todo", "doing", "blocked", "done")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# (table, column, definition) — applied on connect if the column is missing, so
# an existing tracker.db picks up new fields without losing anything.
MIGRATIONS = [
    ("milestones", "description", "TEXT NOT NULL DEFAULT ''"),
    ("milestones", "auto_close", "INTEGER NOT NULL DEFAULT 1"),
    ("milestones", "pending_notified", "INTEGER NOT NULL DEFAULT 0"),
    ("milestones", "is_stub", "INTEGER NOT NULL DEFAULT 0"),
    ("milestones", "completed_by", "INTEGER"),
    ("milestones", "credit_ids", "TEXT"),
    ("settings", "signoff_role", "INTEGER"),
    ("settings", "layout", "TEXT NOT NULL DEFAULT 'lr'"),
    ("settings", "universal_role", "INTEGER"),
    ("projects", "grp", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("projects", "region", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("projects", "team", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("projects", "difficulty", "REAL NOT NULL DEFAULT 1"),
    ("projects", "last_activity", "TEXT"),
    ("trees", "grp", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("trees", "region", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("trees", "team", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("trees", "project_id", "INTEGER"),
    ("milestones", "grp", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("milestones", "region", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("milestones", "team", "TEXT NOT NULL DEFAULT 'Universal'"),
    ("milestones", "difficulty", "REAL NOT NULL DEFAULT 1"),
    ("milestones", "private", "INTEGER NOT NULL DEFAULT 0"),
    ("milestones", "created_at", "TEXT"),
    ("milestones", "announce_on_close", "INTEGER NOT NULL DEFAULT 0"),
    ("settings", "board_channel", "INTEGER"),
    ("settings", "board_weekday", "INTEGER NOT NULL DEFAULT 0"),
    ("settings", "board_hour", "INTEGER NOT NULL DEFAULT 9"),
    ("settings", "board_tree", "TEXT"),
    ("settings", "last_board", "TEXT"),
    ("settings", "stale_channel", "INTEGER"),
    ("settings", "stale_days", "INTEGER NOT NULL DEFAULT 7"),
]

# the three taxonomy dimensions share one shape
TAXONOMIES = ("grp", "region", "team")
TAXONOMY_LABEL = {"grp": "group", "region": "region", "team": "team"}


def _migrate(c: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    if applied:
        c.commit()
    return applied


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
        for change in _migrate(_conn):
            print(f"[db] migrated: added {change}")
        return _conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.connect() must be called before use")
    return _conn


def _q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------

def create_project(guild_id: int, name: str, description: str, owner_id: int,
                   difficulty: float = 1.0) -> int:
    created = now()
    cur = _exec(
        "INSERT INTO projects (guild_id, name, description, owner_id, created_at, last_activity, difficulty) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, name, description, owner_id, created, created, clamp_difficulty(difficulty)),
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


def set_project_difficulty(project_id: int, value: float) -> None:
    _exec("UPDATE projects SET difficulty = ? WHERE id = ?",
          (clamp_difficulty(value), project_id))


def touch_project(project_id: int) -> None:
    _exec("UPDATE projects SET last_activity = ? WHERE id = ?", (now(), project_id))


def touch_task_project(task_id: int) -> None:
    _exec("UPDATE projects SET last_activity = ? WHERE id = "
          "(SELECT project_id FROM tasks WHERE id = ?)", (now(), task_id))


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
    touch_project(project_id)
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
    touch_task_project(task_id)


def assign_task(task_id: int, assignee_id: Optional[int]) -> None:
    _exec("UPDATE tasks SET assignee_id = ? WHERE id = ?", (assignee_id, task_id))
    touch_task_project(task_id)


def update_task_details(task_id: int, assignee_id: Optional[int], due_date: Optional[str],
                        weight: int) -> None:
    """Update imported planning details without changing a task's live status."""
    _exec(
        "UPDATE tasks SET assignee_id = ?, due_date = ?, weight = ? WHERE id = ?",
        (assignee_id, due_date, weight, task_id),
    )
    touch_task_project(task_id)


def delete_task(task_id: int) -> None:
    rows = _q("SELECT project_id FROM tasks WHERE id = ?", (task_id,))
    _exec("DELETE FROM tasks WHERE id = ?", (task_id,))
    if rows:
        touch_project(rows[0]["project_id"])


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
    touch_project(project_id)


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

def create_milestone(guild_id: int, key: str, name: str, unlocks: str = "",
                     xp: int = 100, description: str = "", auto_close: bool = True) -> int:
    cur = _exec(
        "INSERT INTO milestones (guild_id, key, name, description, unlocks, xp, auto_close, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, key.lower(), name, description, unlocks, xp, int(auto_close), now()),
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


def creates_cycle(milestone_id: int, requires_id: int) -> bool:
    """True if requiring `requires_id` would make the graph circular.

    Walks up from the proposed prerequisite; if we reach the milestone itself,
    the edge closes a loop and both nodes would lock forever with no explanation.
    """
    if milestone_id == requires_id:
        return True
    seen, stack = set(), [requires_id]
    while stack:
        cur = stack.pop()
        if cur == milestone_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack += [r["requires_id"] for r in _q(
            "SELECT requires_id FROM milestone_deps WHERE milestone_id = ?", (cur,))]
    return False


def add_dep(milestone_id: int, requires_id: int) -> bool:
    """Returns False and changes nothing if the edge would create a cycle."""
    if creates_cycle(milestone_id, requires_id):
        return False
    _exec(
        "INSERT OR IGNORE INTO milestone_deps (milestone_id, requires_id) VALUES (?, ?)",
        (milestone_id, requires_id),
    )
    return True


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


def private_project_ids(guild_id: int) -> set[int]:
    """Projects linked to private milestones must not appear on the public site."""
    rows = _q(
        "SELECT DISTINCT mp.project_id FROM milestone_projects mp "
        "JOIN milestones m ON m.id = mp.milestone_id "
        "WHERE m.guild_id = ? AND m.private = 1",
        (guild_id,),
    )
    return {row["project_id"] for row in rows}


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


def _bulk_progress(guild_id: int) -> dict[int, dict[str, int]]:
    """Progress for every milestone in the guild, in one query."""
    rows = _q(
        "SELECT mp.milestone_id AS mid,"
        "       COALESCE(SUM(t.weight), 0) AS total,"
        "       COALESCE(SUM(CASE WHEN t.status='done' THEN t.weight ELSE 0 END), 0) AS done_w,"
        "       COUNT(t.id) AS n,"
        "       COALESCE(SUM(t.status != 'done'), 0) AS remaining "
        "FROM milestones m "
        "JOIN milestone_projects mp ON mp.milestone_id = m.id "
        "LEFT JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE m.guild_id = ? GROUP BY mp.milestone_id",
        (guild_id,),
    )
    out = {}
    for r in rows:
        total = r["total"] or 0
        out[r["mid"]] = {
            "pct": round(100 * (r["done_w"] or 0) / total) if total else 0,
            "tasks": r["n"] or 0,
            "remaining": r["remaining"] or 0,
        }
    return out


def _bulk_people(guild_id: int) -> dict[int, dict[str, list[int]]]:
    """Assignees per milestone, split into those with open work and those who
    closed something. One query for the whole guild."""
    rows = _q(
        "SELECT mp.milestone_id AS mid, t.assignee_id AS uid,"
        "       SUM(t.status = 'done') AS done_n,"
        "       SUM(t.status != 'done') AS open_n "
        "FROM milestones m "
        "JOIN milestone_projects mp ON mp.milestone_id = m.id "
        "JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE m.guild_id = ? AND t.assignee_id IS NOT NULL "
        "GROUP BY mp.milestone_id, t.assignee_id "
        "ORDER BY open_n DESC, t.assignee_id",
        (guild_id,),
    )
    out: dict[int, dict[str, list[int]]] = {}
    for r in rows:
        slot = out.setdefault(r["mid"], {"done": [], "open": []})
        if r["done_n"]:
            slot["done"].append(r["uid"])
        if r["open_n"]:
            slot["open"].append(r["uid"])
    return out


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

    # four queries total, regardless of how many milestones there are
    bulk_prog = _bulk_progress(guild_id)
    bulk_people = _bulk_people(guild_id)
    blank = {"pct": 0, "tasks": 0, "remaining": 0}
    prog = {m["key"]: bulk_prog.get(m["id"], blank) for m in ms}

    # "the tasks are finished" and "we agree it's achieved" are different claims.
    # auto_close milestones treat them as one; the rest wait for a human.
    work_done = {m["key"]: prog[m["key"]]["tasks"] > 0 and prog[m["key"]]["pct"] == 100
                 for m in ms}
    complete = {
        m["key"]: bool(m["completed_at"]) or (bool(m["auto_close"]) and work_done[m["key"]])
        for m in ms
    }
    pending = {
        m["key"]: (not complete[m["key"]]) and work_done[m["key"]] and not m["auto_close"]
        for m in ms
    }

    out = []
    for m in ms:
        k = m["key"]
        gate_open = all(complete.get(p, False) for p in prereqs[k])
        if complete[k]:
            state = "complete"
        elif pending[k]:
            state = "pending"          # work done, still gates everything downstream
        elif not gate_open:
            state = "locked"
        elif prog[k]["pct"] > 0:
            state = "active"
        else:
            state = "available"
        out.append({
            "out_of_order": complete[k] and not gate_open,
            "id": m["id"], "key": k, "name": m["name"], "unlocks": m["unlocks"],
            "description": m["description"],
            "people": _people_from(m, bulk_people.get(m["id"]),
                                    complete[k] or pending[k]),
            "auto_close": bool(m["auto_close"]), "is_stub": bool(m["is_stub"]),
            "completed_at": m["completed_at"], "completed_by": m["completed_by"],
            "xp": m["xp"], "state": state, "pct": 100 if complete[k] else prog[k]["pct"],
            "private": bool(m["private"]),
            "grp": m["grp"], "region": m["region"], "team": m["team"],
            "remaining": prog[k]["remaining"], "prereqs": prereqs[k],
            "blocked_by": [p for p in prereqs[k] if not complete.get(p, False)],
            "settled": bool(m["settled"]),
        })
    return out


def contributors(milestone_id: int) -> list[int]:
    """Everyone who closed a task under this milestone. Order is stable."""
    rows = _q(
        "SELECT DISTINCT t.assignee_id AS uid "
        "FROM milestone_projects mp JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE mp.milestone_id = ? AND t.status = 'done' AND t.assignee_id IS NOT NULL "
        "ORDER BY t.assignee_id",
        (milestone_id,),
    )
    return [r["uid"] for r in rows]


def even_split(xp: int, people: list[int]) -> dict[int, int]:
    """Split as evenly as whole numbers allow; leftovers go to the first names."""
    if not people:
        return {}
    base, extra = divmod(xp, len(people))
    return {uid: base + (1 if i < extra else 0) for i, uid in enumerate(people)}


def settle_milestone(guild_id: int, milestone_id: int, xp: int) -> dict[int, int]:
    """Mint XP once, split across contributors by effort. Returns user_id -> xp.

    A milestone with no tasks has no contributors to split across, so the credit
    goes to whoever signed it off.
    """
    with _lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            m = c.execute(
                "SELECT * FROM milestones WHERE id = ? AND guild_id = ?",
                (milestone_id, guild_id),
            ).fetchone()
            if m is None or m["settled"]:
                c.rollback()
                return {}

            # Priority: names given at close > people who did tasks > signer.
            if m["credit_ids"]:
                people = [int(x) for x in m["credit_ids"].split(",") if x.strip().isdigit()]
            else:
                rows = c.execute(
                    "SELECT DISTINCT t.assignee_id AS uid FROM milestone_projects mp "
                    "JOIN tasks t ON t.project_id = mp.project_id "
                    "WHERE mp.milestone_id = ? AND t.status = 'done' "
                    "AND t.assignee_id IS NOT NULL ORDER BY t.assignee_id",
                    (milestone_id,),
                ).fetchall()
                people = [row["uid"] for row in rows]
            if not people and m["completed_by"]:
                people = [m["completed_by"]]

            awards = even_split(xp, people)
            created_at = now()
            for uid, amount in awards.items():
                c.execute(
                    "INSERT INTO credit (guild_id, user_id, milestone_id, xp, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (guild_id, uid, milestone_id, amount, created_at),
                )
            c.execute(
                "UPDATE milestones SET settled = 1, completed_at = COALESCE(completed_at, ?) "
                "WHERE id = ?",
                (created_at, milestone_id),
            )
            c.commit()
            return awards
        except Exception:
            c.rollback()
            raise


def complete_milestone(milestone_id: int, user_id: Optional[int] = None,
                       credit_ids: Optional[list[int]] = None) -> None:
    _exec(
        "UPDATE milestones SET completed_at = ?, completed_by = COALESCE(completed_by, ?), "
        "credit_ids = COALESCE(?, credit_ids) WHERE id = ?",
        (now(), user_id,
         ",".join(str(u) for u in credit_ids) if credit_ids else None,
         milestone_id),
    )


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
    allowed = {"name", "unlocks", "xp", "description", "auto_close", "is_stub"}
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


def people_on(milestone_id: int, is_complete: bool) -> list[int]:
    """Who to show on the node.

    Finished milestone -> who actually did the work. Live milestone -> who is
    holding open tasks right now, which is the more useful question mid-flight.
    """
    if is_complete:
        row = _q("SELECT credit_ids FROM milestones WHERE id = ?", (milestone_id,))
        if row and row[0]["credit_ids"]:
            return [int(x) for x in row[0]["credit_ids"].split(",") if x.strip().isdigit()]
        return contributors(milestone_id)
    rows = _q(
        "SELECT t.assignee_id AS uid, COUNT(*) AS n "
        "FROM milestone_projects mp JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE mp.milestone_id = ? AND t.status != 'done' AND t.assignee_id IS NOT NULL "
        "GROUP BY t.assignee_id ORDER BY n DESC",
        (milestone_id,),
    )
    return [r["uid"] for r in rows]


def set_auto_close(milestone_id: int, auto: bool) -> None:
    _exec("UPDATE milestones SET auto_close = ? WHERE id = ?", (int(auto), milestone_id))


def mark_pending_notified(milestone_id: int, flag: bool = True) -> None:
    _exec("UPDATE milestones SET pending_notified = ? WHERE id = ?",
          (int(flag), milestone_id))


def pending_notified(milestone_id: int) -> bool:
    rows = _q("SELECT pending_notified FROM milestones WHERE id = ?", (milestone_id,))
    return bool(rows and rows[0]["pending_notified"])


def milestones_for_project(project_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT m.* FROM milestone_projects mp JOIN milestones m ON m.id = mp.milestone_id "
        "WHERE mp.project_id = ?",
        (project_id,),
    )


def create_stub(guild_id: int, name: str) -> int:
    """A placeholder created because something was declared to depend on it.

    Stubs let you build a tree top-down — name the gate first, describe it later.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "stub"
    key, n = base, 2
    while get_milestone(guild_id, key):
        key, n = f"{base}-{n}", n + 1
    cur = _exec(
        "INSERT INTO milestones (guild_id, key, name, is_stub) VALUES (?, ?, ?, 1)",
        (guild_id, key, name.strip()[:80]),
    )
    return cur.lastrowid


def find_or_stub(guild_id: int, term: str) -> tuple[int, bool]:
    """Match a milestone by key, exact name, then partial name. Stub it if absent.

    Returns (milestone_id, was_created).
    """
    low = term.strip().lower()
    existing = list_milestones(guild_id)
    hit = next((m for m in existing if m["key"] == low), None)
    hit = hit or next((m for m in existing if m["name"].lower() == low), None)
    hit = hit or next((m for m in existing if low in m["name"].lower()), None)
    if hit:
        return hit["id"], False
    return create_stub(guild_id, term), True


def clear_stub(milestone_id: int) -> None:
    _exec("UPDATE milestones SET is_stub = 0 WHERE id = ?", (milestone_id,))


def list_stubs(guild_id: int) -> list[sqlite3.Row]:
    return _q(
        "SELECT * FROM milestones WHERE guild_id = ? AND is_stub = 1 ORDER BY id",
        (guild_id,),
    )


def closure_history(guild_id: int, tree_key: Optional[str] = None) -> list[sqlite3.Row]:
    """Milestones that have been closed, newest first, with who and when."""
    sql = (
        "SELECT m.*, GROUP_CONCAT(DISTINCT c.user_id) AS credited "
        "FROM milestones m LEFT JOIN credit c ON c.milestone_id = m.id "
        "WHERE m.guild_id = ? AND m.completed_at IS NOT NULL "
    )
    args: list[Any] = [guild_id]
    if tree_key:
        sql += ("AND m.id IN (SELECT tm.milestone_id FROM tree_members tm "
                "JOIN trees t ON t.id = tm.tree_id WHERE t.guild_id = ? AND t.key = ?) ")
        args += [guild_id, tree_key]
    sql += "GROUP BY m.id ORDER BY m.completed_at DESC"
    return _q(sql, tuple(args))


def set_signoff_role(guild_id: int, role_id: Optional[int]) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET signoff_role = ? WHERE guild_id = ?", (role_id, guild_id))


def get_signoff_role(guild_id: int) -> Optional[int]:
    return get_settings(guild_id)["signoff_role"]


def project_delete_impact(project_id: int) -> dict[str, Any]:
    """What a /project delete would actually destroy."""
    tasks = _q("SELECT COUNT(*) AS n FROM tasks WHERE project_id = ?", (project_id,))[0]["n"]
    return {"tasks": tasks, "milestones": [m["name"] for m in milestones_for_project(project_id)]}


def set_layout(guild_id: int, mode: str) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET layout = ? WHERE guild_id = ?",
          ("tb" if mode == "tb" else "lr", guild_id))


def get_layout(guild_id: int) -> str:
    return get_settings(guild_id)["layout"] or "lr"


# --------------------------------------------------------------------------
# levels — framework, deliberately unfinished
# --------------------------------------------------------------------------
# XP currently only accumulates. This gives it a shape: thresholds with names,
# and a place for consequences to be attached later. Nothing here grants
# anything yet; `perk` is descriptive text and LEVEL_HOOKS is the extension
# point where real effects should be registered.

DEFAULT_LEVELS = [
    (0,    "Newcomer",   ""),
    (250,  "Regular",    ""),
    (750,  "Contributor",""),
    (1750, "Steward",    ""),
    (3500, "Anchor",     ""),
]

# Callables invoked as hook(guild_id, user_id, old_level, new_level) whenever
# someone crosses a threshold. Register from bot.py to grant roles, post
# announcements, unlock commands. Kept as a plain list so nothing here needs to
# know about Discord.
LEVEL_HOOKS: list = []


def ensure_default_levels(guild_id: int) -> None:
    if _q("SELECT 1 FROM levels WHERE guild_id = ? LIMIT 1", (guild_id,)):
        return
    for threshold, name, perk in DEFAULT_LEVELS:
        _exec("INSERT OR IGNORE INTO levels (guild_id, threshold, name, perk) "
              "VALUES (?, ?, ?, ?)", (guild_id, threshold, name, perk))


def list_levels(guild_id: int) -> list[sqlite3.Row]:
    ensure_default_levels(guild_id)
    return _q("SELECT * FROM levels WHERE guild_id = ? ORDER BY threshold", (guild_id,))


def set_level(guild_id: int, threshold: int, name: str, perk: str = "") -> None:
    ensure_default_levels(guild_id)
    _set_level_raw(guild_id, threshold, name, perk)


def _set_level_raw(guild_id: int, threshold: int, name: str, perk: str = "") -> None:
    _exec("INSERT INTO levels (guild_id, threshold, name, perk) VALUES (?, ?, ?, ?) "
          "ON CONFLICT(guild_id, threshold) DO UPDATE SET name = ?, perk = ?",
          (guild_id, threshold, name, perk, name, perk))


def remove_level(guild_id: int, threshold: int) -> None:
    _exec("DELETE FROM levels WHERE guild_id = ? AND threshold = ?", (guild_id, threshold))


def user_xp(guild_id: int, user_id: int) -> int:
    rows = _q("SELECT COALESCE(SUM(xp), 0) AS xp FROM credit "
              "WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return rows[0]["xp"] or 0


def level_for(guild_id: int, xp: int) -> dict[str, Any]:
    """Current level plus how far into the next one this much XP sits."""
    ladder = list_levels(guild_id)
    current = ladder[0] if ladder else None
    nxt = None
    for row in ladder:
        if xp >= row["threshold"]:
            current = row
        else:
            nxt = row
            break
    floor = current["threshold"] if current else 0
    span = (nxt["threshold"] - floor) if nxt else 0
    return {
        "name": current["name"] if current else "—",
        "perk": current["perk"] if current else "",
        "threshold": floor,
        "index": [r["threshold"] for r in ladder].index(floor) + 1 if current else 0,
        "total": len(ladder),
        "next_name": nxt["name"] if nxt else None,
        "next_at": nxt["threshold"] if nxt else None,
        "into": xp - floor,
        "span": span,
        "pct": round(100 * (xp - floor) / span) if span else 100,
    }


def apply_level_ups(guild_id: int, awards: dict[int, int]) -> list[dict[str, Any]]:
    """Given XP just minted, work out who crossed a threshold and fire hooks.

    `awards` is user_id -> xp added, so the pre-award total is today's total
    minus the award.
    """
    crossed = []
    for uid, amount in awards.items():
        after = user_xp(guild_id, uid)
        before = after - amount
        old, new = level_for(guild_id, before), level_for(guild_id, after)
        if new["threshold"] > old["threshold"]:
            crossed.append({"user_id": uid, "from": old, "to": new})
            for hook in LEVEL_HOOKS:
                try:
                    hook(guild_id, uid, old, new)
                except Exception:      # a broken hook must not lose the XP
                    pass
    return crossed


def _people_from(m: sqlite3.Row, slot: Optional[dict], is_complete: bool) -> list[int]:
    """Same rule as people_on, fed from the bulk map instead of its own queries.

    Finished milestone -> who did the work (or the explicit credit list).
    Live milestone -> who is holding open tasks right now.
    """
    if is_complete:
        if m["credit_ids"]:
            return [int(x) for x in m["credit_ids"].split(",") if x.strip().isdigit()]
        return sorted(slot["done"]) if slot else []
    return slot["open"] if slot else []


# ==========================================================================
# STAGE 1 additions: taxonomy, difficulty, privacy, permissions, audit
# ==========================================================================

# --- taxonomy (group / region / team) -------------------------------------

def add_taxonomy(guild_id: int, kind: str, value: str) -> None:
    _exec("INSERT OR IGNORE INTO taxonomy (guild_id, kind, value) VALUES (?, ?, ?)",
          (guild_id, kind, value.strip()))


def remove_taxonomy(guild_id: int, kind: str, value: str) -> None:
    _exec("DELETE FROM taxonomy WHERE guild_id = ? AND kind = ? AND value = ?",
          (guild_id, kind, value.strip()))


def list_taxonomy(guild_id: int, kind: str) -> list[str]:
    """Configured values for a dimension, always including Universal."""
    rows = _q("SELECT value FROM taxonomy WHERE guild_id = ? AND kind = ? "
              "ORDER BY value COLLATE NOCASE", (guild_id, kind))
    vals = [r["value"] for r in rows]
    return ["Universal"] + [v for v in vals if v != "Universal"]


def tag_columns(grp: str = None, region: str = None, team: str = None) -> dict:
    """Only the dimensions that were supplied, for a partial update."""
    out = {}
    if grp is not None:
        out["grp"] = grp
    if region is not None:
        out["region"] = region
    if team is not None:
        out["team"] = team
    return out


def set_project_tags(project_id: int, **tags) -> None:
    cols = tag_columns(**tags)
    if cols:
        _exec(f"UPDATE projects SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
              (*cols.values(), project_id))


def set_tree_tags(tree_id: int, **tags) -> None:
    cols = tag_columns(**tags)
    if cols:
        _exec(f"UPDATE trees SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
              (*cols.values(), tree_id))


def set_milestone_tags(milestone_id: int, **tags) -> None:
    cols = tag_columns(**tags)
    if cols:
        _exec(f"UPDATE milestones SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
              (*cols.values(), milestone_id))


def visible_filter(grp: str = None, region: str = None, team: str = None,
                   alias: str = "") -> tuple[str, list]:
    """SQL fragment: rows in the named group/region/team, plus Universal ones.

    A None dimension isn't constrained. Returns ('' , []) when nothing is set,
    so callers can append it unconditionally.
    """
    a = f"{alias}." if alias else ""
    clauses, args = [], []
    for col, val in (("grp", grp), ("region", region), ("team", team)):
        if val and val != "Universal":
            clauses.append(f"({a}{col} = ? OR {a}{col} = 'Universal')")
            args.append(val)
    return (" AND ".join(clauses), args)


# --- difficulty & privacy on create ---------------------------------------

_orig_create_milestone = create_milestone       # noqa: F821  (defined earlier)


def create_milestone(guild_id: int, key: str, name: str, unlocks: str = "",
                     xp: int = 100, description: str = "", auto_close: bool = True,
                     difficulty: float = 1.0, private: bool = False,
                     grp: str = "Universal", region: str = "Universal",
                     team: str = "Universal") -> int:
    mid = _orig_create_milestone(guild_id, key, name, unlocks, xp, description, auto_close)
    _exec("UPDATE milestones SET difficulty=?, private=?, grp=?, region=?, team=? "
          "WHERE id=?",
          (clamp_difficulty(difficulty), int(bool(private)), grp, region, team, mid))
    return mid


def clamp_difficulty(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    v = max(1.0, min(10.0, v))
    return round(v * 2) / 2               # snap to half steps


def set_difficulty(milestone_id: int, value: float) -> None:
    _exec("UPDATE milestones SET difficulty=? WHERE id=?",
          (clamp_difficulty(value), milestone_id))


def set_private(milestone_id: int, private: bool) -> None:
    _exec("UPDATE milestones SET private=? WHERE id=?",
          (int(bool(private)), milestone_id))


def can_read_description(guild_id: int, milestone_id: int, user_id: int,
                         user_role_ids: set[int], is_manager: bool) -> bool:
    """Private descriptions: assignees, permitted roles, or managers only."""
    row = _q("SELECT private FROM milestones WHERE id=?", (milestone_id,))
    if not row or not row[0]["private"]:
        return True
    if is_manager:
        return True
    perm_role = get_cmd_perm(guild_id, "milestone_private")
    if perm_role and perm_role in user_role_ids:
        return True
    # assignees of any task under this milestone
    rows = _q("SELECT 1 FROM milestone_projects mp JOIN tasks t ON t.project_id=mp.project_id "
              "WHERE mp.milestone_id=? AND t.assignee_id=? LIMIT 1", (milestone_id, user_id))
    return bool(rows)


# --- per-command role gates -----------------------------------------------

def set_cmd_perm(guild_id: int, command: str, role_id: Optional[int]) -> None:
    if role_id is None:
        _exec("DELETE FROM cmd_perms WHERE guild_id=? AND command=?", (guild_id, command))
    else:
        _exec("INSERT INTO cmd_perms (guild_id, command, role_id) VALUES (?, ?, ?) "
              "ON CONFLICT(guild_id, command) DO UPDATE SET role_id=?",
              (guild_id, command, role_id, role_id))


def get_cmd_perm(guild_id: int, command: str) -> Optional[int]:
    rows = _q("SELECT role_id FROM cmd_perms WHERE guild_id=? AND command=?",
              (guild_id, command))
    return rows[0]["role_id"] if rows else None


def list_cmd_perms(guild_id: int) -> list[sqlite3.Row]:
    return _q("SELECT command, role_id FROM cmd_perms WHERE guild_id=? ORDER BY command",
              (guild_id,))


def set_universal_role(guild_id: int, role_id: Optional[int]) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET universal_role=? WHERE guild_id=?", (role_id, guild_id))


def get_universal_role(guild_id: int) -> Optional[int]:
    return get_settings(guild_id)["universal_role"]


# --- milestone update log (appended into the description) ------------------

def append_milestone_note(milestone_id: int, author_id: int, body: str) -> str:
    """Records an audit row and returns the stamped line for display.

    Timestamp is UTC by deliberate choice — Discord can't tell the bot a user's
    local zone, and one shared clock beats several guessed ones.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _exec("INSERT INTO milestone_audit (milestone_id, author_id, body, created_at) "
          "VALUES (?, ?, ?, ?)", (milestone_id, author_id, body, now()))
    line = f"[{ts}] <@{author_id}>: {body}"
    cur = _q("SELECT description FROM milestones WHERE id=?", (milestone_id,))[0]["description"]
    joined = (cur + "\n" + line) if cur else line
    _exec("UPDATE milestones SET description=? WHERE id=?", (joined, milestone_id))
    return line


def milestone_audit(milestone_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return _q("SELECT * FROM milestone_audit WHERE milestone_id=? "
              "ORDER BY id DESC LIMIT ?", (milestone_id, limit))


def link_tree_project(tree_id: int, project_id: int) -> None:
    _exec("UPDATE trees SET project_id=? WHERE id=?", (project_id, tree_id))


def milestones_in_scope(guild_id: int, grp: str = "Universal", region: str = "Universal",
                        team: str = "Universal", exclude_id: int = None) -> list[sqlite3.Row]:
    """Milestones a user in this scope should see in a picker: same group/region/
    team, plus anything Universal. This is the dropdown-visibility rule — it
    deliberately hides other groups' milestones so they can't be linked by
    accident."""
    frag, args = visible_filter(grp=grp, region=region, team=team, alias="m")
    sql = "SELECT m.* FROM milestones m WHERE m.guild_id = ?"
    params = [guild_id]
    if frag:
        sql += " AND " + frag
        params += args
    if exclude_id:
        sql += " AND m.id != ?"
        params.append(exclude_id)
    sql += " ORDER BY m.name COLLATE NOCASE"
    return _q(sql, tuple(params))


# ==========================================================================
# config export / import (the leadership control panel round-trip)
# ==========================================================================
# The panel is an offline HTML page: it can't see the server, so /config export
# hands it the current config plus the role list (names <-> ids) it needs to
# render real dropdowns. /config import takes an edited file back. Semantics are
# REPLACE — the file is the source of truth — with every removal surfaced first.

def export_config(guild_id: int) -> dict:
    """Everything the panel edits, as plain JSON-able data. Role IDs are kept as
    strings so a 64-bit snowflake survives a JSON round trip intact."""
    perms = {r["command"]: str(r["role_id"]) for r in list_cmd_perms(guild_id)}
    tax = {kind: [v for v in list_taxonomy(guild_id, kind) if v != "Universal"]
           for kind in TAXONOMIES}
    lv = [{"xp": r["threshold"], "name": r["name"], "perk": r["perk"]}
          for r in list_levels(guild_id)]
    uni = get_universal_role(guild_id)
    sign = get_signoff_role(guild_id)
    stale = stale_alert_settings(guild_id)
    return {
        "version": 1,
        "permissions": perms,
        "universal_role": str(uni) if uni else None,
        "signoff_role": str(sign) if sign else None,
        "taxonomy": tax,
        "levels": lv,
        "stale_alerts": {"channel": str(stale["channel"]) if stale["channel"] else None,
                         "days": stale["days"], "roles": [str(r) for r in stale["roles"]]},
    }


def diff_config(guild_id: int, doc: dict, valid_role_ids: set[int]) -> dict:
    """What applying `doc` would change, without touching anything.

    Returns adds/changes/removals per section, plus rules skipped because the
    role no longer exists, plus a lockout check.
    """
    def rid(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    report = {"perm_set": [], "perm_change": [], "perm_remove": [],
              "skipped": [], "tax_add": [], "tax_remove": [],
              "level_set": [], "level_remove": [],
              "universal": None, "signoff": None, "stale_alerts": None, "lockout": False}

    # --- permissions (replace) ---
    current = {r["command"]: r["role_id"] for r in list_cmd_perms(guild_id)}
    wanted_raw = doc.get("permissions", {}) or {}
    wanted = {}
    for cmd, role in wanted_raw.items():
        r = rid(role)
        if r is None or r not in valid_role_ids:
            report["skipped"].append((f"permission:{cmd}", str(role)))
            continue
        wanted[cmd] = r
    for cmd, r in wanted.items():
        if cmd not in current:
            report["perm_set"].append((cmd, r))
        elif current[cmd] != r:
            report["perm_change"].append((cmd, current[cmd], r))
    for cmd, r in current.items():
        if cmd not in wanted:
            report["perm_remove"].append((cmd, r))

    # --- universal / signoff roles ---
    for key, getter in (("universal", get_universal_role), ("signoff", get_signoff_role)):
        want = rid(doc.get(f"{key}_role"))
        if want is not None and want not in valid_role_ids:
            report["skipped"].append((f"{key}_role", str(doc.get(f'{key}_role'))))
            want = None
        cur = getter(guild_id)
        if want != cur:
            report[key] = (cur, want)

    # --- taxonomy (replace per dimension) ---
    for kind in TAXONOMIES:
        cur = {v for v in list_taxonomy(guild_id, kind) if v != "Universal"}
        want = {v.strip() for v in (doc.get("taxonomy", {}).get(kind, []) or []) if v.strip()}
        for v in want - cur:
            report["tax_add"].append((kind, v))
        for v in cur - want:
            report["tax_remove"].append((kind, v))

    # --- levels (replace) ---
    cur_levels = {r["threshold"]: (r["name"], r["perk"]) for r in list_levels(guild_id)}
    want_levels = {}
    for lv in doc.get("levels", []) or []:
        try:
            want_levels[int(lv["xp"])] = (lv.get("name", ""), lv.get("perk", ""))
        except (KeyError, ValueError, TypeError):
            report["skipped"].append(("level", str(lv)))
    for xp, (nm, pk) in want_levels.items():
        if xp not in cur_levels or cur_levels[xp] != (nm, pk):
            report["level_set"].append((xp, nm))
    for xp in cur_levels:
        if xp not in want_levels:
            report["level_remove"].append((xp, cur_levels[xp][0]))

    # --- stale-project alert settings ---
    raw_stale = doc.get("stale_alerts", {}) or {}
    channel = rid(raw_stale.get("channel"))
    try:
        days = max(1, min(90, int(raw_stale.get("days", 7))))
    except (TypeError, ValueError):
        days = 7
    roles = sorted({r for value in raw_stale.get("roles", []) or []
                    if (r := rid(value)) in valid_role_ids})
    skipped_roles = [value for value in raw_stale.get("roles", []) or [] if rid(value) not in valid_role_ids]
    report["skipped"].extend(("stale_alert_role", str(value)) for value in skipped_roles)
    wanted_stale = {"channel": channel, "days": days, "roles": roles}
    if wanted_stale != stale_alert_settings(guild_id):
        report["stale_alerts"] = wanted_stale

    # --- lockout guard: would config_import end up with a valid gate AND a
    #     Manage-Server fallback? Manage Server always passes, so the only true
    #     lockout is a broken role gate — which we skip anyway. Flag if the
    #     import tries to gate config_import to a now-invalid role.
    ci = wanted_raw.get("config_import")
    if ci is not None and rid(ci) not in valid_role_ids:
        report["lockout"] = True     # they tried to gate it to a dead role

    return report


def apply_config(guild_id: int, doc: dict, valid_role_ids: set[int]) -> None:
    """Replace-apply. Assumes diff_config was shown; re-validates roles so a
    stale preview can't sneak a broken gate through."""
    def rid(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # permissions: set wanted, clear the rest
    wanted = {}
    for cmd, role in (doc.get("permissions", {}) or {}).items():
        r = rid(role)
        if r is not None and r in valid_role_ids:
            wanted[cmd] = r
    for r in list_cmd_perms(guild_id):
        if r["command"] not in wanted:
            set_cmd_perm(guild_id, r["command"], None)
    for cmd, r in wanted.items():
        set_cmd_perm(guild_id, cmd, r)

    # universal / signoff
    uni = rid(doc.get("universal_role"))
    set_universal_role(guild_id, uni if uni in valid_role_ids else None)
    sign = rid(doc.get("signoff_role"))
    set_signoff_role(guild_id, sign if sign in valid_role_ids else None)

    # taxonomy: replace each dimension's set
    for kind in TAXONOMIES:
        cur = {v for v in list_taxonomy(guild_id, kind) if v != "Universal"}
        want = {v.strip() for v in (doc.get("taxonomy", {}).get(kind, []) or []) if v.strip()}
        for v in cur - want:
            remove_taxonomy(guild_id, kind, v)
        for v in want - cur:
            add_taxonomy(guild_id, kind, v)

    # levels: replace the whole ladder. Delete raw (bypassing the default
    # auto-seed) so the file is truly the source of truth.
    _exec("DELETE FROM levels WHERE guild_id=?", (guild_id,))
    for lv in doc.get("levels", []) or []:
        try:
            _set_level_raw(guild_id, int(lv["xp"]), lv.get("name", "Level"),
                           lv.get("perk", ""))
        except (KeyError, ValueError, TypeError):
            continue

    raw_stale = doc.get("stale_alerts", {}) or {}
    try:
        days = int(raw_stale.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    channel = rid(raw_stale.get("channel"))
    roles = [r for value in raw_stale.get("roles", []) or []
             if (r := rid(value)) in valid_role_ids]
    set_stale_alerts(guild_id, channel, days, roles)


# --------------------------------------------------------------------------
# moderator reporting and notifications
# --------------------------------------------------------------------------

NOTIFY_SCOPES = ("server", "project", "tree", "milestone")


def milestone_assignees(milestone_id: int) -> list[int]:
    rows = _q(
        "SELECT DISTINCT t.assignee_id FROM milestone_projects mp "
        "JOIN tasks t ON t.project_id = mp.project_id "
        "WHERE mp.milestone_id = ? AND t.assignee_id IS NOT NULL",
        (milestone_id,),
    )
    return [r["assignee_id"] for r in rows]


def add_notify(guild_id: int, scope: str, scope_id: int, kind: str, target_id: int) -> None:
    if scope not in NOTIFY_SCOPES or kind not in ("role", "user"):
        raise ValueError("bad notify scope or kind")
    _exec("INSERT OR IGNORE INTO notify_targets "
          "(guild_id, scope, scope_id, target_kind, target_id) VALUES (?, ?, ?, ?, ?)",
          (guild_id, scope, scope_id, kind, target_id))


def remove_notify(guild_id: int, scope: str, scope_id: int, kind: str, target_id: int) -> None:
    _exec("DELETE FROM notify_targets WHERE guild_id=? AND scope=? AND scope_id=? "
          "AND target_kind=? AND target_id=?",
          (guild_id, scope, scope_id, kind, target_id))


def list_notify(guild_id: int, scope: str, scope_id: int) -> list[sqlite3.Row]:
    return _q("SELECT target_kind, target_id FROM notify_targets "
              "WHERE guild_id=? AND scope=? AND scope_id=?",
              (guild_id, scope, scope_id))


def effective_notify(guild_id: int, milestone_id: int) -> dict[str, list[int]]:
    scope_ids = {"milestone": {milestone_id}, "tree": set(), "project": set()}
    for r in _q("SELECT tree_id FROM tree_members WHERE milestone_id=?", (milestone_id,)):
        scope_ids["tree"].add(r["tree_id"])
    for r in _q("SELECT project_id FROM milestone_projects WHERE milestone_id=?", (milestone_id,)):
        scope_ids["project"].add(r["project_id"])
    for tid in scope_ids["tree"]:
        row = _q("SELECT project_id FROM trees WHERE id=?", (tid,))
        if row and row[0]["project_id"]:
            scope_ids["project"].add(row[0]["project_id"])
    targets = {"role": set(), "user": set()}
    for scope, ids in scope_ids.items():
        for sid in ids:
            for target in list_notify(guild_id, scope, sid):
                targets[target["target_kind"]].add(target["target_id"])
    return {kind: sorted(ids) for kind, ids in targets.items()}


def stale_alert_settings(guild_id: int) -> dict:
    settings = get_settings(guild_id)
    roles = [r["target_id"] for r in list_notify(guild_id, "server", guild_id)
             if r["target_kind"] == "role"]
    return {"channel": settings["stale_channel"], "days": settings["stale_days"],
            "roles": sorted(roles)}


def set_stale_alerts(guild_id: int, channel_id: Optional[int], days: int,
                     role_ids: list[int]) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET stale_channel=?, stale_days=? WHERE guild_id=?",
          (channel_id, max(1, min(90, int(days))), guild_id))
    _exec("DELETE FROM notify_targets WHERE guild_id=? AND scope='server' AND scope_id=?",
          (guild_id, guild_id))
    for role_id in set(role_ids):
        add_notify(guild_id, "server", guild_id, "role", role_id)


def all_stale_alert_guilds() -> list[sqlite3.Row]:
    return _q("SELECT * FROM settings WHERE stale_channel IS NOT NULL")


def stale_projects(guild_id: int, stale_days: int) -> list[dict]:
    """Projects that are blocked or have had no recorded activity recently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    out = []
    for project in list_projects(guild_id):
        blocked = _q("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status='blocked'",
                     (project["id"],))[0]["n"]
        if blocked:
            out.append({"id": project["id"], "name": project["name"], "kind": "blocked",
                        "detail": f"{blocked} blocked task(s)"})
            continue
        activity = project["last_activity"] or project["created_at"]
        if activity and activity < cutoff:
            out.append({"id": project["id"], "name": project["name"], "kind": "stale",
                        "detail": f"no activity for {stale_days}+ days"})
    return out


def claim_stale_project_alerts(guild_id: int) -> tuple[dict, list[dict]]:
    """Return newly-stale projects once; activity or unblocking resets the alert."""
    settings = stale_alert_settings(guild_id)
    if not settings["channel"] or not settings["roles"]:
        return settings, []
    current = stale_projects(guild_id, settings["days"])
    wanted = {(item["id"], item["kind"]) for item in current}
    with _lock:
        c = conn()
        existing = c.execute(
            "SELECT a.project_id, a.kind FROM project_alerts a JOIN projects p ON p.id=a.project_id "
            "WHERE p.guild_id=?", (guild_id,)).fetchall()
        for row in existing:
            if (row["project_id"], row["kind"]) not in wanted:
                c.execute("DELETE FROM project_alerts WHERE project_id=? AND kind=?",
                          (row["project_id"], row["kind"]))
        fresh = []
        for item in current:
            cur = c.execute("INSERT OR IGNORE INTO project_alerts (project_id, kind, sent_at) "
                            "VALUES (?, ?, ?)", (item["id"], item["kind"], now()))
            if cur.rowcount:
                fresh.append(item)
        c.commit()
    return settings, fresh


def stuck_report(guild_id: int, stale_days: int = 7) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    idle, stalled = [], []
    for milestone in tree_state(guild_id):
        assignees = milestone_assignees(milestone["id"])
        if milestone["state"] == "available" and milestone["pct"] == 0:
            rows = _q("SELECT created_at FROM milestones WHERE id=?", (milestone["id"],))
            created = rows[0]["created_at"] if rows else None
            if created and created < cutoff:
                idle.append({"name": milestone["name"], "key": milestone["key"],
                             "assignees": assignees})
        elif milestone["state"] == "active":
            rows = _q("SELECT MAX(COALESCE(t.completed_at, t.created_at)) AS last "
                      "FROM milestone_projects mp JOIN tasks t ON t.project_id = mp.project_id "
                      "WHERE mp.milestone_id = ?", (milestone["id"],))
            last = rows[0]["last"] if rows else None
            if last and last < cutoff:
                stalled.append({"name": milestone["name"], "key": milestone["key"],
                                "pct": milestone["pct"], "last": last,
                                "assignees": assignees})
    blocked = _q("SELECT t.title, t.assignee_id, p.name AS project FROM tasks t "
                 "JOIN projects p ON p.id=t.project_id WHERE p.guild_id=? "
                 "AND t.status='blocked' AND p.status='active' ORDER BY p.name", (guild_id,))
    return {"idle": idle, "stalled": stalled,
            "blocked": [{"title": r["title"], "project": r["project"],
                         "assignee": r["assignee_id"]} for r in blocked],
            "stale_days": stale_days}


def stuck_by_owner(report: dict) -> dict[int, dict]:
    owners: dict[int, dict] = {}
    def bucket(user_id: int):
        return owners.setdefault(user_id or 0, {"idle": [], "stalled": [], "blocked": []})
    for item in report["idle"]:
        for user_id in item["assignees"] or [0]:
            bucket(user_id)["idle"].append(item["name"])
    for item in report["stalled"]:
        for user_id in item["assignees"] or [0]:
            bucket(user_id)["stalled"].append(item["name"])
    for item in report["blocked"]:
        bucket(item["assignee"])["blocked"].append(f"{item['title']} ({item['project']})")
    return owners


def set_board(guild_id: int, channel_id: int, weekday: int, hour: int,
              tree: str | None) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET board_channel=?, board_weekday=?, board_hour=?, board_tree=? "
          "WHERE guild_id=?", (channel_id, weekday, hour, tree, guild_id))


def clear_board(guild_id: int) -> None:
    get_settings(guild_id)
    _exec("UPDATE settings SET board_channel=NULL WHERE guild_id=?", (guild_id,))


def mark_board_sent(guild_id: int) -> None:
    _exec("UPDATE settings SET last_board=? WHERE guild_id=?", (now(), guild_id))


def all_board_guilds() -> list[sqlite3.Row]:
    return _q("SELECT * FROM settings WHERE board_channel IS NOT NULL")


def closed_since(guild_id: int, since_iso: str | None) -> list[sqlite3.Row]:
    if since_iso:
        return _q("SELECT name, completed_at FROM milestones WHERE guild_id=? "
                  "AND completed_at IS NOT NULL AND completed_at > ? ORDER BY completed_at",
                  (guild_id, since_iso))
    return _q("SELECT name, completed_at FROM milestones WHERE guild_id=? "
              "AND completed_at IS NOT NULL ORDER BY completed_at", (guild_id,))


def set_announce_on_close(milestone_id: int, on: bool) -> None:
    _exec("UPDATE milestones SET announce_on_close=? WHERE id=?", (int(bool(on)), milestone_id))


def announce_fired(guild_id: int) -> Optional[sqlite3.Row]:
    rows = _q("SELECT * FROM settings WHERE guild_id=? AND board_channel IS NOT NULL", (guild_id,))
    if not rows:
        return None
    board = rows[0]
    if board["last_board"]:
        hit = _q("SELECT 1 FROM milestones WHERE guild_id=? AND announce_on_close=1 "
                 "AND completed_at IS NOT NULL AND completed_at > ? LIMIT 1",
                 (guild_id, board["last_board"]))
    else:
        hit = _q("SELECT 1 FROM milestones WHERE guild_id=? AND announce_on_close=1 "
                 "AND completed_at IS NOT NULL LIMIT 1", (guild_id,))
    return board if hit else None


# --------------------------------------------------------------------------
# planner reference export
# --------------------------------------------------------------------------

def export_for_planner(guild_id: int) -> dict:
    """Return read-only tree/project reference data for the offline planner.

    This is deliberately not an import document: it contains the live keys and
    names the planner needs when extending an existing tree or linking a new
    milestone to an existing project.
    """
    trees = []
    for tree in list_trees(guild_id):
        members = _q(
            "SELECT m.key, m.name FROM tree_members tm "
            "JOIN milestones m ON m.id = tm.milestone_id "
            "WHERE tm.tree_id = ? ORDER BY m.name COLLATE NOCASE",
            (tree["id"],),
        )
        trees.append({
            "key": tree["key"],
            "name": tree["name"],
            "group": tree["grp"],
            "region": tree["region"],
            "team": tree["team"],
            "milestones": [{"key": row["key"], "name": row["name"]}
                           for row in members],
        })
    projects = [{
        "name": project["name"],
        "group": project["grp"],
        "region": project["region"],
        "team": project["team"],
        "difficulty": project["difficulty"],
    } for project in list_projects(guild_id)]
    return {"_kind": "planner_server_export", "trees": trees, "projects": projects}
