# Discord Project Progress Tracker

A self-hosted Discord bot that tracks a project as a **tech tree**: milestones
with prerequisites, drawn as an image, where finishing one visibly unlocks the
next. Built for volunteer and civic organisations where the point is to get
people moving work forward, not just to log it.

Python 3.12, discord.py, SQLite — one file, no database server, no web service,
no privileged intents.

---

## Contents

- [Setup](#setup)
- [The idea in one minute](#the-idea-in-one-minute)
- [Core concepts](#core-concepts) — projects, tasks, milestones, trees
- [Command reference](#command-reference)
- [How the pieces work](#how-the-pieces-work) — XP, sign-off, privacy, taxonomy, and the rest
- [The offline tools](#the-offline-tools) — planner and config panel
- [Running the tests](#running-the-tests)
- [A caution worth keeping](#a-caution-worth-keeping)

---

## Setup

1. Create an application at <https://discord.com/developers/applications>, add a
   **Bot**, and copy the token.
2. Under **OAuth2 → URL Generator**, tick `bot` and `applications.commands`,
   grant *Send Messages* and *Embed Links*, and invite it to your server.
3. Install and run:

   ```bash
   pip install -r requirements.txt
   export DISCORD_TOKEN="your-token"
   python bot.py
   ```

Set a `GUILD_ID` environment variable while setting up, or slash commands sync
globally and take up to an hour to appear. `DEPLOY.md` has the full walkthrough
including the systemd unit, font requirement, and backup notes. No privileged
intents are required.

---

## The idea in one minute

A **project** holds **tasks**. Finishing tasks fills a **milestone**. Milestones
have **prerequisites**, so completing one unlocks the milestones that depended on
it. That dependency graph is a **tree**, rendered as an image where locked nodes
are grey, available ones lit, and finished ones green.

Contributors earn **XP** when a milestone unlocks, which accrues toward **levels**.

The fastest way in is `/start`, which walks you through building a project, a
tree, and its first milestones with fill-in-the-blank forms. Everything below can
also be done one command at a time.

---

## Core concepts

**Project** — a container for work. Has an owner, a status, and group/region/team
tags. Holds tasks.

**Task** — one step inside a project. Has an assignee, a status
(todo/doing/blocked/done), an optional due date, and a weight. Tasks have no
prerequisites; they just move a milestone toward 100%.

**Milestone** — a box on the tree. Has prerequisites, a payoff (what it unlocks),
a difficulty, and optionally a privacy flag. Its percentage is the weighted
completion of the projects linked to it. If something needs a prerequisite, it's
a milestone, not a task.

**Tree** — a named *view* over the shared milestone graph, not a separate graph.
A milestone can appear in several trees at once, so a shared gate like "funding
secured" shows on every board it blocks rather than being duplicated.

---

## Command reference

There are a lot of commands because the bot does a lot; in daily use you touch
about six. They're grouped by noun.

### The daily handful

| Command | What it does |
|---|---|
| `/start` | Guided setup: project → tree → milestones |
| `/next [tree]` | What's ready to work on right now |
| `/tree show [tree] [orientation]` | Draw the tree as an image |
| `/tree confirm key [credit]` | Sign off a milestone, award its XP |
| `/me` | Your open tasks across every project (private) |
| `/task done task_id` | Complete a task; the bar updates |

### Projects

| Command | What it does |
|---|---|
| `/project new name [description] [group] [region] [team]` | Start tracking a project |
| `/project list [include_archived]` | Every project with a progress bar |
| `/project view name` | Tasks, status breakdown, recent updates |
| `/project log name note` | Post a narrative status update |
| `/project archive` · `unarchive` · `delete` | Lifecycle (owner or Manage Server) |

### Tasks

| Command | What it does |
|---|---|
| `/task add project title [assignee] [due] [weight]` | Add work |
| `/task done task_id` | Complete a task |
| `/task status task_id new_status` | todo / doing / blocked / done |
| `/task assign task_id [member]` | Reassign or clear |
| `/task list project [status] [assignee]` | Filtered view |
| `/task delete task_id` | Remove a task (confirms first) |

### Trees and milestones

| Command | What it does |
|---|---|
| `/tree new key name [project] [group] [region] [team]` | Create a named tree |
| `/tree add key name … [difficulty] [private]` | Add a milestone |
| `/tree edit key …` | Change any milestone field, including tags |
| `/tree requires milestone prerequisite` | Add a dependency (announces cross-group links) |
| `/tree link key project` | Attach a project's work to a milestone |
| `/tree confirm` · `complete` | Sign off a milestone |
| `/tree include` · `exclude` | Add or remove a milestone from a tree view |
| `/tree remove` · `drop` | Delete a milestone / a tree (both confirm first) |
| `/tree list` | Overview of all trees |
| `/tree import file:` | Load a plan from an attached spreadsheet |
| `/tree note key note` | Append a timestamped note to a milestone |
| `/tree history [tree] [key]` | Closures across a tree, or one milestone's full timeline |

### Progress and recognition

| Command | What it does |
|---|---|
| `/leaderboard` | XP earned, with each person's level |
| `/levels` | The XP ladder and where you sit |
| `/help` | Plain-language explainer |

### Configuration (Manage Server)

| Command | What it does |
|---|---|
| `/config signoff [role]` | Who may sign off milestones |
| `/config layout orientation` | Default tree orientation, server-wide |
| `/config tag-add` · `tag-remove` · `tags` | Manage group / region / team values |
| `/config permission` · `permissions` | Restrict a command to a role |
| `/config universal-role [role]` | Who may set things Universal |
| `/config level` · `unlevel` | Edit the XP ladder |
| `/config export` · `import` | The leadership config panel round-trip |
| `/digest set channel [weekday] [hour]` | Weekly summary and overdue list |

---

## How the pieces work

### XP and levels

XP is minted only when a **milestone** unlocks, never per task — per-task points
teach people to fragment work. It's split **evenly** among contributors (names
given at sign-off, then task-closers, then the signer), with any remainder going
to the first names.

XP accrues toward levels (Newcomer, Regular, Contributor, Steward, Anchor by
default). Crossing a threshold posts an announcement. Levels are **cosmetic for
now** — the ladder, thresholds, and announcements all work, but nothing is
granted yet. `db.LEVEL_HOOKS` is the extension point: register a callable there
to make a level grant a Discord role, unlock a command, or anything else.

### Auto-close vs sign-off

Each milestone either closes itself at 100% (`auto_close`, the default) or waits
for a person. A milestone set to wait sits at NEEDS SIGN-OFF with its downstream
still locked and its XP unpaid until someone runs `/tree confirm`. Sign-off is
restricted — see below — because a gate anyone can open isn't a gate.

### Out-of-order completion

A milestone whose own work finishes before its prerequisites do is allowed to
complete, tagged **DONE EARLY**. Real work happens out of sequence, and refusing
to record it would make the board less honest, not more.

### Group, region, team

Every project, tree, and milestone carries three independent labels — **group**,
**region**, **team** — each a named value or **Universal**. Milestones inherit
them from their tree and can be retagged later. The rendered box shows the
non-Universal ones in its lower-left corner.

These drive dropdown visibility: in the guided flow, you only see milestones from
your own group plus anything Universal, so cross-group edits can't happen by
accident. Cross-group *dependencies* remain possible via `/tree requires`, which
posts a notice naming both groups. Setting something **Universal** is itself gated
(`/config universal-role`), so it can't be used to quietly make something visible
everywhere.

### Difficulty

Each milestone has a difficulty from 1 to 10, half-steps allowed, set at creation
and editable after (default 1). It renders as pips along the top of the box. It's
a label, not a mechanic — nothing keys off it yet.

### Private descriptions

A milestone marked `private` renders as **🔒 restricted** on the image. The real
description and its update log are readable only by task assignees, a permitted
role, or a server manager.

This hides descriptions from casual view in a shared channel. It is **not
compliance-grade**: the text lives in the database in plain form, and Discord
itself sees anything the bot sends. Don't store data you'd be liable for leaking.

### The milestone update log

`/tree note` appends a timestamped, attributed line to a milestone rather than
overwriting its description:

> [2026-07-24 19:32 UTC] @Darius: Hall confirmed, deposit paid

Timestamps are UTC — Discord can't tell the bot a user's zone. The full history
lives in a separate audit table (`/tree history key:…`) and survives even when the
description fills.

### Role-gated commands

Beyond Discord's own permissions, any command can be restricted:

```
/config permission command:tree_import role:@Coordinators
```

Manage Server always passes and can't be locked out. `/config permissions` lists
what's gated. This is a bot-level layer on top of Discord's own command settings —
the two are separate systems.

### Rendering

Trees render left-to-right by default, top-to-bottom on request. Orientation is a
per-image option on `/tree show` and a server default via `/config layout`; every
rendered tree also carries a 🧭 button to flip it in place. It matters more than
it sounds — a six-step chain is 2156×299 wide versus 346×1249 tall.

Under the hood, `tree_state` reads the whole graph in four queries regardless of
size, and rendering runs in a worker thread so a large tree doesn't stall the
bot's heartbeat.

---

## The offline tools

Two HTML files run entirely in a browser — nothing is sent anywhere.

**`planner.html`** — build a tree visually and export it as a CSV, then load it
with `/tree import`. It can load a `config.json` from the config panel to tag
milestones with your real groups/regions/teams as you build, so the two tools
share one vocabulary. Milestones carry everything the bot supports — difficulty,
privacy flag, group/region/team — not just name and dependencies. The pages link to each other via a nav bar; each still
exports its own file (a tree CSV from the planner, config JSON from the panel).

**`config_panel.html`** — leadership tool for setting permissions, taxonomy,
sign-off, universal-role, and levels in bulk. Run `/config export` to download a
file carrying the current settings *and your role list*, open it in the panel so
its dropdowns show real role names and its command list matches the live bot,
edit, and apply with `/config import`. To
gate to a role that isn't in the export, add it in the panel's **Roles** section
by name and ID (right-click the role in Discord → Copy ID); the bot matches by
ID, so a role added without one is flagged and won't apply.

Import is **replace** — the file becomes the source of truth. The preview spells
out every addition, change, and **removal** before anything is applied. A rule
naming a role that no longer exists is skipped, never applied as a broken gate.
Both `/config import` and `/config export` are role-gated, and Manage Server can
never be locked out of them.

---

## Running the tests

```bash
python -m unittest discover tests
```

71 tests, standard library only. They cover state derivation, weighted progress,
XP settling once, cycle refusal, stubs, tree views, migrations, levels, taxonomy
scope-filtering, privacy gates, command permissions, the audit log, the config
round-trip, and rendering in both orientations. They're mutation-checked: the
 suite has been verified to *fail* when each guarantee is deliberately broken.

### Live Discord channel check

To verify the bot can actually reach a test channel, run:

```bash
python discord_channel_smoke_test.py
```

Enter the bot token and the channel ID. The script sends one labelled test
message and edits it once. It does not create projects, tasks, trees, or
database records.

---

## A caution worth keeping

This tool assumes the bottleneck is **visibility** — that people don't contribute
because they can't see what needs doing. Sometimes that's true. Often the real
constraint is capacity, authority, or willingness, and a clearer board doesn't
touch any of those. If work stalls because one person is the only one who can do
it, the tree will render that stall beautifully in grey and gold while nothing
moves. Worth watching whether the board changes behaviour or just documents it
more attractively.
