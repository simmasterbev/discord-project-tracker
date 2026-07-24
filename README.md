# Discord Project Progress Tracker

A self-hosted Discord bot for tracking projects and their tasks, with live
progress bars, blockers, due dates, and an optional weekly digest. Storage is
SQLite — one file, no database server.

## Setup

1. Create an application at <https://discord.com/developers/applications>,
   add a **Bot**, and copy the token.
2. Under **OAuth2 → URL Generator**, tick `bot` and `applications.commands`,
   grant *Send Messages* and *Embed Links*, and invite it to your server.
3. Install and run:

```bash
pip install -r requirements.txt
export DISCORD_TOKEN="your-token"
python bot.py
```

Slash commands sync globally on first start and can take up to an hour to
appear. To see them instantly while developing, replace `await self.tree.sync()`
in `setup_hook` with:

```python
guild = discord.Object(id=YOUR_GUILD_ID)
self.tree.copy_global_to(guild=guild)
await self.tree.sync(guild=guild)
```

No privileged intents are required.

## Commands

| Command | What it does |
|---|---|
| `/project new name description` | Start tracking a project |
| `/project list [include_archived]` | Every project with a progress bar |
| `/project view name` | Tasks, status breakdown, recent updates |
| `/project log name note` | Post a narrative status update |
| `/project archive` · `unarchive` · `delete` | Lifecycle (owner or Manage Server) |
| `/task add project title [assignee] [due] [weight]` | Add work |
| `/task done task_id` | Complete a task, bar updates |
| `/task status task_id new_status` | todo / doing / blocked / done |
| `/task assign task_id [member]` | Reassign or clear |
| `/task list project [status] [assignee]` | Filtered view |
| `/task delete task_id` | Remove a task |
| `/me` | Your open tasks across all projects (private) |
| `/digest set channel [weekday] [hour]` | Weekly summary + overdue list |

### Tech tree

| Command | What it does |
|---|---|
| `/tree show [tree]` | Renders one tree — or everything, if you name none |
| `/tree new key name description` | Create a named tree |
| `/tree list` | Every tree with its progress |
| `/tree include key tree` · `exclude` | File a milestone into / out of a tree |
| `/tree drop tree` | Delete a view (milestones survive) |
| `/tree add key name unlocks requires xp tree` | Add a milestone, optionally filed into a tree |
| `/tree edit key [name] [unlocks] [xp]` | Change a milestone after the fact |
| `/tree requires key prerequisite` | Gate one milestone behind another |
| `/tree link key project` | Attach a project's tasks to a milestone |
| `/tree complete key` | Close a milestone by hand |
| `/tree remove key` | Delete a milestone |
| `/next [tree]` | What's open now, and the cheapest path to the next unlock |
| `/leaderboard` | XP standings |

## Notes

- **Weighted progress.** `weight` (1–20) lets a two-week task count more than a
  ten-minute one, so the bar reflects effort rather than task count.
- **Due dates** accept `YYYY-MM-DD`, `5d`, `2w`, `1m`, `today`, `tomorrow`.
- **Scoping.** Everything keys on `guild_id`, so one instance can serve several
  servers without projects leaking between them.
- **Permissions.** Anyone can create projects and tasks; only the project owner
  or someone with Manage Server can archive or delete.
- **Backups.** The whole dataset is `tracker.db`. Copy that file.

## Filling it in

Order matters, because each layer references the one before it:

1. `/project new` — the container for real work
2. `/task add` — the work itself, with weights
3. `/tree new` — a named board
4. `/tree add` — milestones, **prerequisites before dependents**
5. `/tree link` — attach projects so the milestone tracks itself

`/tree edit` fixes anything you got wrong later.

### Bulk setup

Standing up a fifteen-node tree by hand is about forty slash commands. Write it
once instead:

```bash
python seed.py example_tree.yaml --guild YOUR_SERVER_ID
```

`example_tree.yaml` documents every available field. The loader upserts by key,
so edit the file and re-run to push changes — nothing duplicates, nothing is
deleted. Dependencies resolve in a second pass, so you can reference a milestone
defined further down the file.

Run it on the server with the bot stopped, or against a copy of `tracker.db` and
copy it back.

## How the tech tree works

Milestones are nodes; `requires` edges gate them. A milestone **completes** when
every project linked to it hits 100%, which means the tree updates itself off
normal task activity — nobody has to maintain it separately.

State is derived, never stored:

- **locked** — at least one prerequisite is unfinished
- **available** — all prerequisites done, no work started
- **active** — prerequisites done, work underway
- **complete** — all linked projects finished

When the last prerequisite lands, the bot posts an unlock announcement naming
everyone whose tasks fed it and listing what just became possible.

### Multiple trees

Trees are **named views over one shared milestone graph**, not separate graphs. A
milestone can belong to several trees at once, which is the point: if "funding
secured" gates both your build-out and your event, it should appear in both
boards rather than being duplicated and drifting out of sync.

When a tree's member depends on a milestone filed elsewhere, that prerequisite
still renders — tagged `FROM <tree>` with a thinner border — so a gate never
disappears just because it belongs to another initiative. Progress counts and
`/next` ignore those external nodes; they're context, not your scoreboard.

Milestones in no tree are "unfiled" and appear only in `/tree show` with no
argument. Existing installs upgrade cleanly: the new tables are additive, and
milestones created before trees existed simply start out unfiled.

### On XP

XP is minted **only when a milestone unlocks**, then split across contributors in
proportion to the weight of tasks they closed. This is deliberate: per-task
points reward creating and closing trivial tasks, which is the failure mode of
most gamified trackers. Tying the mint to milestones means the only way to score
is to move something that was actually gating the project.

`/leaderboard` is optional social pressure. The unlock announcement is the part
that does the real work — it converts "I finished a chore" into "I opened a door
for everyone."

## Extending

`db.py` is a plain function-per-query module with no ORM, so adding a field is a
schema line plus a query. Natural next steps: threads per project, a `/burndown`
chart via matplotlib, GitHub issue sync, or reminder DMs for overdue tasks.
