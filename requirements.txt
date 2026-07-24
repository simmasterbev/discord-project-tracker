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
| `/start` | Guided form-based setup |
| `/help` | Plain-language explainer |
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
| `/tree edit key [name] [description] [unlocks] [xp] [auto_close]` | Change a milestone after the fact |
| `/tree confirm key [credit]` | Sign off a milestone; optionally name who splits the XP |
| `/tree history [tree]` | Who closed what, and when |
| `/tree import file:` | Load a plan from an attached spreadsheet |
| `/config signoff role:` | Which role may sign off milestones |
| `/config layout orientation:` | Left-to-right or top-to-bottom, server-wide |
| `/tree show orientation:` | Override the orientation for one image |
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

## The two ideas

Everything rests on one distinction, and it's the only thing worth learning:

- **Milestones** are the boxes on the tree. They have **prerequisites** (what must
  finish first) and a **payoff** (what they unlock).
- **Tasks** are the small steps inside one milestone. They have no prerequisites
  of their own — they just tick a milestone toward 100%.

If you find yourself wanting to give a *task* a prerequisite, that task is really
a milestone. Promote it.

## Easiest way in: `/start`

`/start` opens a pop-up form. Name the tree, then press **Add milestone** up to
four times. Each milestone is one screen with five labelled boxes:

| Box | Example |
|---|---|
| Milestone name | Venue booked |
| What is it? | Call three halls, compare quotes, sign, pay the deposit |
| What does finishing it make possible? | the date becomes announceable |
| Must come after… | Funding secured, Scope locked |
| XP when it unlocks | 250 |

Four is the cap because a Discord form allows five inputs and because a first
tree with more than four boxes is usually one nobody reads. Add more afterwards
with `/tree add`.

### Stubs

Prerequisites are matched **by name**. Anything that doesn't exist yet is created
as a **stub** — a placeholder node rendered in grey as **NEEDS DEFINING**.

This is what makes top-down sketching possible. Start from the thing you actually
want ("Promo campaign live"), name what it waits on, and the gates appear as
placeholders. Fill them in on a later pass with `/tree edit` — supplying a
description or a payoff clears the stub flag automatically. `/next` lists any
still undefined.

Stubs behave like ordinary milestones otherwise: they gate their dependents and
can be completed. They just look unfinished, because they are.

### Ways in that aren't Discord

| Route | Who it's for |
|---|---|
| **`/tree import`** | Drag a `.csv` onto the Discord message box and attach it to the command. The bot shows a preview of exactly what it will create, change, or stub — nothing is written until you press **Apply**. |
| **`planner.html`** | Open it in any browser — no install, no server, nothing sent anywhere. Fill in milestones, tick which ones come first, watch the tree assemble, download a spreadsheet. |
| **A spreadsheet** | Columns: `tree, milestone, description, unlocks, requires, xp, auto_close`. One row per milestone, semicolons between multiple prerequisites. Edit in Excel or Google Sheets, export as CSV. |
| **A YAML file** | Same structure, better for version control. See `example_tree.yaml`. |

The last two end at the same place, either through Discord with `/tree import`
or from a shell:

```bash
python seed.py your-plan.csv --guild YOUR_SERVER_ID
```

`/tree import` needs no server access at all, which makes it the route to give
anyone who isn't going to SSH anywhere.

Re-running updates rather than duplicating, so the file stays the source of truth
if you want it to. Prerequisites naming something the file doesn't define become
stubs.

### Milestones without tasks

A milestone with no linked project can't track itself, so it stays at 0% until
someone runs `/tree confirm`. That's the normal case for a tree built through
`/start` — the description carries the detail, and the node is a yes/no.

### How XP is split

Always an **even split**, never weighted. In priority order:

1. Names given at close time — `/tree confirm key:x credit:"@ana @ben @cy"`.
   Accepts mentions, IDs, or plain display names.
2. Everyone who closed a task under the milestone.
3. Whoever signed it off, if there's nobody else.

Remainders go to the first names, so 100 XP across three people is 34/33/33.

`/help` prints the same explanation in-channel.

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

## What a node shows

Each card carries: state tag, XP, milestone name, **description** (what it is),
progress bar, **unlocks** (what it buys you), and **who's on it**.

The people row answers a different question depending on state. On a live
milestone it lists whoever holds *open* tasks — the useful mid-flight question is
who to nudge. On a completed one it lists whoever actually closed the work, in
contribution order, which is who earned the XP.

Names are resolved through a REST fetch when the member isn't cached, so this
works without the privileged members intent.

## Who may sign off

`/tree confirm` and `/tree complete` are restricted, because a sign-off gate that
anyone can press is not a gate. By default only **Manage Server** qualifies.
Widen it with:

```
/config signoff role:@Coordinators
```

Everything else — creating milestones, closing tasks, importing plans — stays
open to everyone.

## Deleting things

`/project delete`, `/tree remove`, and `/tree drop` show what will be destroyed
and wait for a confirmation press. `/tree remove` also names any milestones the
deletion would unlock, since removing a gate silently opens whatever it held.

## Cycles

A dependency that would make the graph circular is refused at the point of
creation, in every route: `/tree requires`, `/tree add`, the `/start` form, and
the file loader. Without that check both milestones lock permanently and nothing
in the interface explains why.

## Closure record

Every closed milestone keeps who signed it and when. It shows in three places:
on the node itself (the footer switches from the payoff line to
`closed by Ana · 23 Jul`), in `/tree history`, and in the credit ledger behind
`/leaderboard`. Auto-closed milestones record `auto` as the signer, since no
human pressed anything.

## Schema changes

New columns are applied on startup by `_migrate()` in `db.py`, which checks
`PRAGMA table_info` and issues an `ALTER TABLE` only when the column is missing.
Existing databases upgrade in place with no data loss — you'll see a
`[db] migrated:` line in the journal the first time. Take a backup first anyway.

## Auto-close vs sign-off

Each milestone carries `auto_close`, defaulting to **true**.

- **true** — the node flips to complete the moment its linked projects hit 100%.
- **false** — the node enters **NEEDS SIGN-OFF** (purple) instead. Downstream
  stays locked and **no XP is paid** until someone runs `/tree confirm`.

The distinction matters because "every task I wrote down is done" and "this is
genuinely achieved" are different claims, and they diverge exactly when someone
under-scoped the list. Cheap, well-understood milestones should close themselves.
The two or three that other people's plans hang on are worth a human saying yes.

When a sign-off milestone reaches 100%, the bot posts a purple notice once. If
the work reopens, the notice resets and fires again when it returns. `/next`
lists anything waiting.

`/tree confirm` on a milestone below 100% works — sometimes scope legitimately
changes — but it says the percentage out loud in the channel so the override is
visible rather than quiet.

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

## Performance notes

Database calls and PNG rendering both run in worker threads via
`asyncio.to_thread`, and `db` holds a lock around every statement. Rendering a
30-node tree takes roughly a third of a second — long enough to stall the gateway
heartbeat if it ran inline, which can drop the bot's connection.

Trees render **left to right** by default and **top to bottom** on request.
Orientation is one setting — `/config layout` — with a per-command override on
`/tree show`. It matters more than it sounds: a six-step chain renders 2156×299
left-to-right, a strip too wide to read on a phone, and 346×1249 top-to-bottom.
Wide shallow trees want left-to-right; deep narrow ones want top-to-bottom.

Layout is a layered DAG: longest-path depth, then four alternating barycenter
sweeps, with dummy routing lanes inserted for edges spanning more than one
column so they route around intermediate nodes rather than through them. Output
is downscaled past 2600px on the long edge so a large tree always fits Discord's
upload limit.

Reads are still N+1 — about two queries per milestone. Fine at this scale;
the fix if it ever matters is a single joined query in `tree_state`.

## Extending

`db.py` is a plain function-per-query module with no ORM, so adding a field is a
schema line plus a query. Natural next steps: threads per project, a `/burndown`
chart via matplotlib, GitHub issue sync, or reminder DMs for overdue tasks.
