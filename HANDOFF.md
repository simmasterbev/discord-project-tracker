# Project handoff — Discord tech-tree progress tracker

Paste this into a new chat and attach `discord-project-tracker.zip`. The zip is
the source of truth; this file exists to explain the decisions behind it so they
don't get quietly undone.

---

## What it is

A self-hosted Discord bot that tracks projects as a **tech tree**: milestones
with prerequisites, rendered as an image, where finishing one visibly unlocks
others. Built for a volunteer/civic organisation where the aim is to get people
engaged in moving work forward, not just to log it.

Python 3.12, discord.py 2.7, SQLite, Pillow. No web server, no open ports, no
privileged Discord intents.

| File | Lines | Role |
|---|---|---|
| `bot.py` | 1355 | 41 slash commands, Discord wiring |
| `db.py` | 927 | All SQL. No ORM, one function per query |
| `tree_render.py` | 403 | Layered-DAG PNG renderer |
| `wizard.py` | 368 | `/start` pop-up forms, import + delete confirmations |
| `seed.py` | 240 | Bulk load from YAML or CSV |
| `planner.html` | 234 | Offline browser planner, exports CSV |

---

## Taxonomy, difficulty, privacy (newest layer)

Projects/trees/milestones carry **group·region·team**, each a value or
`Universal`, inheriting downward. Dropdowns are scope-filtered so you only link
within your group — cross-group links need the typed `/tree requires` and post a
notice. Setting `Universal` is role-gated. Milestones have a **difficulty** 1–10
(half-steps, pips on the box) and a **private** flag that renders the description
as 🔒 and gates its text to assignees/roles/managers. `/tree note` appends
UTC audit lines (`/tree history key:…` reads them, interleaved with the closure). Any command can be role-gated via `/config permission`.

Privacy is **not compliance-grade** — plaintext in the DB, visible to Discord.
Documented as such.

## Config panel (leadership bulk-edit)

`config_panel.html` is offline — it produces and consumes files, never touches
the server. `/config export` dumps config + the role list so the panel's
dropdowns show real names; `/config import` takes an edited JSON back. Import is
**replace** (file = source of truth) with every removal previewed. Broken-role
gates are skipped; `/config import`/`export` are gated with Manage Server never
locked out. `db.export_config` / `diff_config` / `apply_config` are the core;
`_set_level_raw` exists so a replace doesn't re-seed default levels.

## The core model — get this right first

Two ideas, and conflating them is the single most likely way to break things:

- **Milestones** are the boxes on the tree. They have **prerequisites** and a
  **payoff** (`unlocks`). They are what the tree is made of.
- **Tasks** are small steps inside a **project**. They have no prerequisites of
  their own. Projects are linked to milestones; a milestone's percentage is the
  weighted completion of its linked projects.

If something needs a prerequisite, it is a milestone, not a task.

**Trees** are *named views over one shared milestone graph*, not separate graphs.
A milestone can belong to several trees at once — deliberate, so a shared gate
like "funding secured" appears on every board it blocks rather than being
duplicated and drifting.

### Node state is derived, never stored

`db.tree_state()` computes state on every call. Do not add a status column.

| State | Meaning |
|---|---|
| `locked` | a prerequisite is unfinished |
| `available` | prerequisites done, no work started |
| `active` | prerequisites done, work underway |
| `pending` | tasks all done, waiting on a human (`auto_close = 0`) |
| `complete` | done |

Plus an orthogonal `is_stub` flag: a placeholder created because something was
declared to depend on it.

---

## Decisions that were made deliberately

Reversing any of these should be a conscious choice, not an accident.

**XP mints only when a milestone unlocks, never per task.** Per-task points
teach people to fragment work and close trivia. `settled` guards against double
minting.

**XP is split evenly**, never weighted. Priority: names given at close time →
people who closed tasks → whoever signed off. Remainders go to the first names.

**Sign-off is permission-gated.** `/tree confirm` and `/tree complete` need
Manage Server or a role set via `/config signoff`. A gate anyone can open is not
a gate. Everything else stays open to everyone.

**`auto_close` is per-milestone**, defaulting true. False means the node sits at
NEEDS SIGN-OFF with downstream still locked and XP unpaid until confirmed.

**Out-of-order completion is allowed** — a milestone can finish before its
prerequisites. Deliberate; real work happens out of sequence. Those nodes are
tagged DONE EARLY rather than being prevented.

**Unknown prerequisites become stubs**, in every route (`/tree add`,
`/tree requires`, the wizard, the file loader). This is what lets a tree be
sketched top-down instead of entered in dependency order.

**Schema changes go through `MIGRATIONS` in `db.py`** — add to the `SCHEMA`
string *and* the `MIGRATIONS` list. `CREATE TABLE IF NOT EXISTS` silently does
nothing for a new column, and there is live data in the wild.

**Database calls and Pillow rendering run in `asyncio.to_thread`,** with a lock
around every SQL statement. A 30-node render takes ~0.3s and was stalling the
gateway heartbeat inline.

**Layout**: longest-path depth, four alternating barycenter sweeps, dummy routing
lanes for edges spanning multiple columns. Orientation is `lr` or `tb` via
`/config layout`. Output downscales past 2600px.

---

## Where things stand

**GitHub**: `https://github.com/simmasterbev/discord-project-tracker` — public.
As of handoff its `main` is at `2e9091d`, which is **two rounds behind**. A
prepared commit `1b80ee7` exists in `github-update.bundle`, applied on top of
that history:

```bash
git clone https://github.com/simmasterbev/discord-project-tracker.git
cd discord-project-tracker
git pull ~/Downloads/github-update.bundle main
git push
```

Claude cannot push — no credential reaches the sandbox and GitHub returns
`remote: No anonymous write access`. No GitHub connector exists in the MCP
directory. Don't spend turns retrying this.

**Deployment**: not yet live on a server. `DEPLOY.md` has the full walkthrough.
Gotchas that bite: install `fonts-dejavu-core` or the renderer falls back to a
bitmap font; set `GUILD_ID` or slash commands take an hour to appear; avoid
hosts with ephemeral filesystems or `tracker.db` is wiped on redeploy.

**Testing so far**: one real test on a live server confirmed project tracking,
weighted progress, milestone auto-complete, XP award, and image generation. The
locked/available/pending states have been verified in a sandbox but not on
Discord.

---

## Known gaps, highest value first

0. **Deploy it.** Nothing past the first live test has run on Discord — and the
   Stage-2 guided wizard (modal→select→modal chains) has *only* been verified to
   construct, never clicked through in a live client. That's the biggest untested
   surface in the codebase.
1. **Tests exist now** — `python -m unittest discover tests`, 66 of them. They
   were mutation-checked: breaking the cycle guard, the even split, the
   double-mint guard, `auto_close`, weighted progress, the credit list, or the
   bulk-query map each makes them fail. Two mutations they do *not* catch are
   the edge-routing waypoints, because routing lanes make no measurable
   difference (see README). Extend rather than trust blindly.
4. **Levels are scaffolding only.** The ladder, thresholds, `/levels`, and
   level-up announcements all work, but nothing is actually granted. Fill in
   `db.LEVEL_HOOKS` to make levels do something.
5. **Top-to-bottom rendering** was verified geometrically but never viewed as an
   image. Worth a visual check.

Fixed since the last audit: global error handler, `/tree list` embed field cap,
`/tree import` permission gate, digest loop threading, per-guild name cache
with a TTL, and a 🧭 flip button on rendered trees.

---

## The thing worth keeping in view

This tool assumes the bottleneck is **visibility** — that people don't contribute
because they can't see what needs doing. Often the real constraint is capacity,
authority, or willingness, and a clearer board doesn't touch those. If work
stalls because one person is the only one who can do it, the tree will render
that stall beautifully while nothing moves.

Watch whether the board changes behaviour or just documents inertia more
attractively. That question matters more than any feature on the list above.
