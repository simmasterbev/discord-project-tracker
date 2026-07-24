"""Discord project progress tracker.

Slash commands:
    /project new | list | view | log | archive | unarchive | delete
    /task add | done | status | assign | list | delete
    /me                     -> your open tasks across all projects
    /digest set             -> weekly summary posted to a channel

Run with:  DISCORD_TOKEN=... python bot.py
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import tree_render

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # set for instant command sync on one server
STATUS_EMOJI = {"todo": "⬜", "doing": "🔵", "blocked": "🔴", "done": "✅"}
BAR_LEN = 14


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def bar(pct: int, length: int = BAR_LEN) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


def parse_due(raw: str | None) -> str | None:
    """Accepts YYYY-MM-DD, `5d`, `2w`, `1m`, or `today` / `tomorrow`."""
    if not raw:
        return None
    raw = raw.strip().lower()
    today = date.today()
    if raw == "today":
        return today.isoformat()
    if raw == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if m := re.fullmatch(r"(\d+)\s*([dwm])", raw):
        n, unit = int(m.group(1)), m.group(2)
        days = {"d": 1, "w": 7, "m": 30}[unit] * n
        return (today + timedelta(days=days)).isoformat()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(
            "Couldn't read that date. Use `YYYY-MM-DD`, `5d`, `2w`, `today`, or `tomorrow`."
        )


def due_label(due: str | None) -> str:
    if not due:
        return ""
    d = date.fromisoformat(due)
    delta = (d - date.today()).days
    if delta < 0:
        return f" · ⚠️ overdue {abs(delta)}d"
    if delta == 0:
        return " · due today"
    if delta <= 7:
        return f" · due in {delta}d"
    return f" · due {due}"


def task_line(t, show_project: bool = False) -> str:
    who = f" <@{t['assignee_id']}>" if t["assignee_id"] else ""
    proj = f" *[{t['project_name']}]*" if show_project else ""
    title = f"~~{t['title']}~~" if t["status"] == "done" else t["title"]
    return f"{STATUS_EMOJI[t['status']]} `#{t['id']}` {title}{proj}{who}{due_label(t['due_date'])}"


def project_embed(project, prog, tasks_, log) -> discord.Embed:
    colour = discord.Color.green() if prog["pct"] == 100 else discord.Color.blurple()
    if prog["blocked"]:
        colour = discord.Color.red()
    e = discord.Embed(
        title=project["name"],
        description=project["description"] or "*No description.*",
        colour=colour,
    )
    e.add_field(
        name=f"Progress — {prog['pct']}%",
        value=f"`{bar(prog['pct'])}`\n"
              f"{prog['done']} done · {prog['doing']} in progress · "
              f"{prog['blocked']} blocked · {prog['todo']} not started",
        inline=False,
    )
    if tasks_:
        shown = tasks_[:15]
        body = "\n".join(task_line(t) for t in shown)
        if len(tasks_) > 15:
            body += f"\n*…and {len(tasks_) - 15} more*"
        e.add_field(name="Tasks", value=body, inline=False)
    else:
        e.add_field(name="Tasks", value="*No tasks yet — add one with `/task add`.*", inline=False)
    if log:
        notes = "\n".join(
            f"<t:{int(datetime.fromisoformat(r['created_at']).timestamp())}:R> "
            f"<@{r['author_id']}>: {r['body']}"
            for r in log
        )
        e.add_field(name="Recent updates", value=notes, inline=False)
    e.set_footer(text=f"Owner: {project['owner_id']} · status: {project['status']}")
    return e


async def resolve(interaction: discord.Interaction, name: str):
    """Fetch a project or reply with an error. Returns None on failure."""
    p = db.get_project(interaction.guild_id, name)
    if p is None:
        await interaction.response.send_message(
            f"No project named **{name}** here. Try `/project list`.", ephemeral=True
        )
        return None
    return p


def can_manage(interaction: discord.Interaction, project) -> bool:
    return (
        interaction.user.id == project["owner_id"]
        or interaction.user.guild_permissions.manage_guild
    )


async def project_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.list_projects(interaction.guild_id, include_archived=True)
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in rows
        if current.lower() in r["name"].lower()
    ][:25]


# ---------------------------------------------------------------------------
# /project
# ---------------------------------------------------------------------------

project_group = app_commands.Group(
    name="project", description="Create and track projects", guild_only=True
)


@project_group.command(name="new", description="Start tracking a new project")
@app_commands.describe(name="Short unique name", description="What is this project?")
async def project_new(interaction: discord.Interaction, name: str, description: str = ""):
    if db.get_project(interaction.guild_id, name):
        await interaction.response.send_message(
            f"**{name}** already exists.", ephemeral=True
        )
        return
    db.create_project(interaction.guild_id, name, description, interaction.user.id)
    await interaction.response.send_message(
        f"📁 Created **{name}**. Add work with `/task add project:{name} title:…`"
    )


@project_group.command(name="list", description="All projects and where they stand")
@app_commands.describe(include_archived="Show archived projects too")
async def project_list(interaction: discord.Interaction, include_archived: bool = False):
    rows = db.list_projects(interaction.guild_id, include_archived)
    if not rows:
        await interaction.response.send_message(
            "No projects yet. Create one with `/project new`.", ephemeral=True
        )
        return
    lines = []
    for p in rows:
        prog = db.progress(p["id"])
        flag = " 🗄️" if p["status"] == "archived" else ""
        blocked = f" · 🔴 {prog['blocked']} blocked" if prog["blocked"] else ""
        lines.append(
            f"**{p['name']}**{flag}\n`{bar(prog['pct'])}` {prog['pct']}% "
            f"({prog['done']}/{prog['count']} tasks){blocked}"
        )
    e = discord.Embed(
        title="Projects", description="\n\n".join(lines), colour=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=e)


@project_group.command(name="view", description="Full detail on one project")
@app_commands.autocomplete(name=project_autocomplete)
async def project_view(interaction: discord.Interaction, name: str):
    p = await resolve(interaction, name)
    if not p:
        return
    await interaction.response.send_message(
        embed=project_embed(p, db.progress(p["id"]), db.list_tasks(p["id"]), db.recent_log(p["id"]))
    )


@project_group.command(name="log", description="Post a status update to a project")
@app_commands.autocomplete(name=project_autocomplete)
@app_commands.describe(note="What changed, what's blocked, what's next")
async def project_log(interaction: discord.Interaction, name: str, note: str):
    p = await resolve(interaction, name)
    if not p:
        return
    db.add_log(p["id"], interaction.user.id, note)
    await interaction.response.send_message(f"📝 Logged on **{p['name']}**: {note}")


@project_group.command(name="archive", description="Hide a finished project from the list")
@app_commands.autocomplete(name=project_autocomplete)
async def project_archive(interaction: discord.Interaction, name: str):
    p = await resolve(interaction, name)
    if not p:
        return
    if not can_manage(interaction, p):
        await interaction.response.send_message(
            "Only the project owner or a server manager can archive it.", ephemeral=True
        )
        return
    db.set_project_status(p["id"], "archived")
    await interaction.response.send_message(f"🗄️ Archived **{p['name']}**.")


@project_group.command(name="unarchive", description="Bring an archived project back")
@app_commands.autocomplete(name=project_autocomplete)
async def project_unarchive(interaction: discord.Interaction, name: str):
    p = await resolve(interaction, name)
    if not p:
        return
    db.set_project_status(p["id"], "active")
    await interaction.response.send_message(f"📂 **{p['name']}** is active again.")


@project_group.command(name="delete", description="Permanently delete a project and its tasks")
@app_commands.autocomplete(name=project_autocomplete)
async def project_delete(interaction: discord.Interaction, name: str):
    p = await resolve(interaction, name)
    if not p:
        return
    if not can_manage(interaction, p):
        await interaction.response.send_message(
            "Only the project owner or a server manager can delete it.", ephemeral=True
        )
        return
    db.delete_project(p["id"])
    await interaction.response.send_message(f"🗑️ Deleted **{p['name']}** and all its tasks.")


# ---------------------------------------------------------------------------
# /task
# ---------------------------------------------------------------------------

task_group = app_commands.Group(
    name="task", description="Work items inside a project", guild_only=True
)


@task_group.command(name="add", description="Add a task to a project")
@app_commands.autocomplete(project=project_autocomplete)
@app_commands.describe(
    title="What needs doing",
    assignee="Who owns it",
    due="YYYY-MM-DD, or 5d / 2w / today / tomorrow",
    weight="Relative effort (default 1) — bigger tasks move the bar more",
)
async def task_add(
    interaction: discord.Interaction,
    project: str,
    title: str,
    assignee: discord.Member | None = None,
    due: str | None = None,
    weight: app_commands.Range[int, 1, 20] = 1,
):
    p = await resolve(interaction, project)
    if not p:
        return
    try:
        due_iso = parse_due(due)
    except ValueError as err:
        await interaction.response.send_message(str(err), ephemeral=True)
        return
    tid = db.add_task(p["id"], title, assignee.id if assignee else None, due_iso, weight)
    prog = db.progress(p["id"])
    await interaction.response.send_message(
        f"➕ `#{tid}` **{title}** → *{p['name']}*"
        f"{f' · <@{assignee.id}>' if assignee else ''}{due_label(due_iso)}\n"
        f"`{bar(prog['pct'])}` {prog['pct']}%"
    )


@task_group.command(name="done", description="Mark a task complete")
@app_commands.describe(task_id="The number shown as #id")
async def task_done(interaction: discord.Interaction, task_id: int):
    t = db.get_task(interaction.guild_id, task_id)
    if not t:
        await interaction.response.send_message(f"No task `#{task_id}` here.", ephemeral=True)
        return
    db.set_task_status(task_id, "done")
    prog = db.progress(t["project_id"])
    msg = f"✅ **{t['title']}** done — *{t['project_name']}* now `{bar(prog['pct'])}` {prog['pct']}%"
    if prog["pct"] == 100:
        msg += "\n🎉 Every task on this project is complete."
    await interaction.response.send_message(msg)
    await push_unlocks(interaction)


@task_group.command(name="status", description="Move a task to todo / doing / blocked / done")
@app_commands.describe(task_id="The number shown as #id")
@app_commands.choices(
    new_status=[app_commands.Choice(name=s, value=s) for s in db.VALID_STATUSES]
)
async def task_status(
    interaction: discord.Interaction, task_id: int, new_status: app_commands.Choice[str]
):
    t = db.get_task(interaction.guild_id, task_id)
    if not t:
        await interaction.response.send_message(f"No task `#{task_id}` here.", ephemeral=True)
        return
    db.set_task_status(task_id, new_status.value)
    prog = db.progress(t["project_id"])
    await interaction.response.send_message(
        f"{STATUS_EMOJI[new_status.value]} `#{task_id}` **{t['title']}** → "
        f"**{new_status.value}** · *{t['project_name']}* `{bar(prog['pct'])}` {prog['pct']}%"
    )
    await push_unlocks(interaction)


@task_group.command(name="assign", description="Hand a task to someone")
async def task_assign(
    interaction: discord.Interaction, task_id: int, member: discord.Member | None = None
):
    t = db.get_task(interaction.guild_id, task_id)
    if not t:
        await interaction.response.send_message(f"No task `#{task_id}` here.", ephemeral=True)
        return
    db.assign_task(task_id, member.id if member else None)
    target = f"<@{member.id}>" if member else "nobody"
    await interaction.response.send_message(f"👤 `#{task_id}` **{t['title']}** assigned to {target}.")


@task_group.command(name="list", description="Tasks in a project, optionally filtered")
@app_commands.autocomplete(project=project_autocomplete)
@app_commands.choices(
    status=[app_commands.Choice(name=s, value=s) for s in db.VALID_STATUSES]
)
async def task_list(
    interaction: discord.Interaction,
    project: str,
    status: app_commands.Choice[str] | None = None,
    assignee: discord.Member | None = None,
):
    p = await resolve(interaction, project)
    if not p:
        return
    rows = db.list_tasks(
        p["id"], status.value if status else None, assignee.id if assignee else None
    )
    if not rows:
        await interaction.response.send_message("Nothing matches that filter.", ephemeral=True)
        return
    e = discord.Embed(
        title=f"{p['name']} — tasks",
        description="\n".join(task_line(t) for t in rows[:40]),
        colour=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=e)


@task_group.command(name="delete", description="Remove a task")
async def task_delete(interaction: discord.Interaction, task_id: int):
    t = db.get_task(interaction.guild_id, task_id)
    if not t:
        await interaction.response.send_message(f"No task `#{task_id}` here.", ephemeral=True)
        return
    db.delete_task(task_id)
    await interaction.response.send_message(f"🗑️ Removed `#{task_id}` **{t['title']}**.")


# ---------------------------------------------------------------------------
# /me and /digest
# ---------------------------------------------------------------------------

digest_group = app_commands.Group(
    name="digest", description="Weekly summary settings", guild_only=True
)


@digest_group.command(name="set", description="Post a weekly project summary to a channel")
@app_commands.describe(
    channel="Where to post", weekday="0 = Monday … 6 = Sunday", hour="Hour of day, UTC"
)
@app_commands.default_permissions(manage_guild=True)
async def digest_set(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    weekday: app_commands.Range[int, 0, 6] = 0,
    hour: app_commands.Range[int, 0, 23] = 9,
):
    db.set_digest(interaction.guild_id, channel.id, weekday, hour)
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    await interaction.response.send_message(
        f"📬 Weekly digest will post in {channel.mention} on {days[weekday]} at {hour:02d}:00 UTC."
    )


class Tracker(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        db.connect()
        self.tree.add_command(project_group)
        self.tree.add_command(task_group)
        self.tree.add_command(digest_group)
        self.tree.add_command(tree_group)
        if GUILD_ID:                      # instant, scoped to one server
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:                             # global: can take up to an hour to appear
            await self.tree.sync()
        self.digest_loop.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} · {len(self.guilds)} guild(s)")

    @tasks.loop(minutes=30)
    async def digest_loop(self):
        now = datetime.now(timezone.utc)
        for s in db.all_digest_guilds():
            if now.weekday() != s["digest_weekday"] or now.hour != s["digest_hour"]:
                continue
            if s["last_digest"]:
                last = datetime.fromisoformat(s["last_digest"])
                if (now - last).total_seconds() < 60 * 60 * 20:
                    continue
            channel = self.get_channel(s["digest_channel"])
            if channel is None:
                continue
            embed = self.build_digest(s["guild_id"])
            if embed:
                await channel.send(embed=embed)
                db.mark_digest_sent(s["guild_id"])

    @digest_loop.before_loop
    async def before_digest(self):
        await self.wait_until_ready()

    @staticmethod
    def build_digest(guild_id: int) -> discord.Embed | None:
        projects = db.list_projects(guild_id)
        if not projects:
            return None
        lines = []
        for p in projects:
            prog = db.progress(p["id"])
            note = f" · 🔴 {prog['blocked']} blocked" if prog["blocked"] else ""
            lines.append(f"**{p['name']}** `{bar(prog['pct'])}` {prog['pct']}%{note}")
        e = discord.Embed(
            title="Weekly project digest",
            description="\n".join(lines),
            colour=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        overdue = db.overdue_tasks(guild_id, date.today().isoformat())
        if overdue:
            e.add_field(
                name=f"⚠️ Overdue ({len(overdue)})",
                value="\n".join(task_line(t, show_project=True) for t in overdue[:10]),
                inline=False,
            )
        return e


bot = Tracker()


@bot.tree.command(name="me", description="Your open tasks across every project")
@app_commands.guild_only()
async def me(interaction: discord.Interaction):
    rows = db.my_tasks(interaction.guild_id, interaction.user.id)
    if not rows:
        await interaction.response.send_message("You're all clear. 🎉", ephemeral=True)
        return
    e = discord.Embed(
        title="Your open tasks",
        description="\n".join(task_line(t, show_project=True) for t in rows[:40]),
        colour=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


# ---------------------------------------------------------------------------
# tech tree
# ---------------------------------------------------------------------------

tree_group = app_commands.Group(
    name="tree", description="The milestone tech tree", guild_only=True
)


async def milestone_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.list_milestones(interaction.guild_id)
    return [
        app_commands.Choice(name=f"{r['key']} — {r['name']}"[:100], value=r["key"])
        for r in rows
        if current.lower() in (r["key"] + r["name"]).lower()
    ][:25]


def unlock_embed(m: dict, awards: dict[int, int], newly: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title=f"🔓  {m['name']}",
        description="**Milestone complete.**" + (f"\n{m['unlocks']}" if m["unlocks"] else ""),
        colour=discord.Color.gold(),
    )
    if awards:
        e.add_field(
            name=f"{m['xp']} XP earned by",
            value="\n".join(
                f"<@{uid}> — **{xp} XP**"
                for uid, xp in sorted(awards.items(), key=lambda kv: -kv[1])
            ),
            inline=False,
        )
    if newly:
        e.add_field(
            name="🆕 This unlocks",
            value="\n".join(
                f"**{n['name']}** — {n['unlocks'] or 'now available to start'}" for n in newly
            ),
            inline=False,
        )
    else:
        e.set_footer(text="Nothing new gated behind this one — but it's on the board.")
    return e


def pending_unlocks(guild_id: int) -> list[discord.Embed]:
    """Settle any milestone whose work just finished; return announcement embeds."""
    state = db.tree_state(guild_id)
    by_key = {m["key"]: m for m in state}
    out = []
    for m in state:
        if m["state"] != "complete" or m["settled"]:
            continue
        awards = db.settle_milestone(guild_id, m["id"], m["xp"])
        newly = [
            n for n in state
            if m["key"] in n["prereqs"]
            and all(by_key[p]["state"] == "complete" for p in n["prereqs"])
        ]
        out.append(unlock_embed(m, awards, newly))
    return out


async def push_unlocks(interaction: discord.Interaction) -> None:
    for e in pending_unlocks(interaction.guild_id):
        await interaction.followup.send(embed=e)


async def tree_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.list_trees(interaction.guild_id)
    return [
        app_commands.Choice(name=f"{r['name']} ({r['key']})"[:100], value=r["key"])
        for r in rows
        if current.lower() in (r["key"] + r["name"]).lower()
    ][:25]


@tree_group.command(name="show", description="Render a tech tree")
@app_commands.autocomplete(tree=tree_autocomplete)
@app_commands.describe(tree="Which tree to draw — leave blank for everything at once")
async def tree_show(interaction: discord.Interaction, tree: str | None = None):
    await interaction.response.defer()
    if tree and not db.get_tree(interaction.guild_id, tree):
        await interaction.followup.send(
            f"No tree called `{tree}`. `/tree list` shows what exists.", ephemeral=True
        )
        return
    nodes = db.tree_view(interaction.guild_id, tree)
    if not nodes:
        await interaction.followup.send(
            "That tree is empty. Add milestones with `/tree add tree:…`.", ephemeral=True
        )
        return
    t = db.get_tree(interaction.guild_id, tree) if tree else None
    title = t["name"] if t else f"{interaction.guild.name} — everything"
    buf = tree_render.render_tree(nodes, db.tree_edges(interaction.guild_id, nodes), title)
    ready = [n["name"] for n in nodes
             if n["state"] == "available" and not n.get("external_from")]
    note = ("**Ready to start:** " + ", ".join(ready)) if ready else ""
    await interaction.followup.send(
        content=note, file=discord.File(buf, filename="techtree.png")
    )


@tree_group.command(name="new", description="Create a named tree")
@app_commands.describe(key="Short slug used in commands", name="Display name")
async def tree_new(interaction: discord.Interaction, key: str, name: str,
                   description: str = ""):
    if db.get_tree(interaction.guild_id, key):
        await interaction.response.send_message(f"`{key}` already exists.", ephemeral=True)
        return
    db.create_tree(interaction.guild_id, key, name, description)
    await interaction.response.send_message(
        f"🌳 Created tree **{name}** (`{key}`).\n"
        f"Add milestones with `/tree add tree:{key} …`, or file existing ones with "
        f"`/tree include`."
    )


@tree_group.command(name="list", description="Every tree and how far along it is")
async def tree_list(interaction: discord.Interaction):
    rows = db.tree_summary(interaction.guild_id)
    unfiled = db.unfiled_milestones(interaction.guild_id)
    if not rows and not unfiled:
        await interaction.response.send_message(
            "No trees yet. Start one with `/tree new`.", ephemeral=True
        )
        return
    e = discord.Embed(title="Trees", colour=discord.Color.blurple())
    for r in rows:
        val = f"`{bar(r['pct'])}` {r['pct']}% — {r['done']}/{r['total']} milestones"
        if r["ready"]:
            val += "\n🟡 ready: " + ", ".join(r["ready"][:3])
        if r["description"]:
            val += f"\n*{r['description']}*"
        e.add_field(name=f"{r['name']}  (`{r['key']}`)", value=val, inline=False)
    if unfiled:
        e.add_field(
            name="Unfiled",
            value=f"{len(unfiled)} milestone(s) not in any tree — "
                  f"they only appear in `/tree show` with no argument.",
            inline=False,
        )
    await interaction.response.send_message(embed=e)


@tree_group.command(name="include", description="Put an existing milestone into a tree")
@app_commands.autocomplete(key=milestone_autocomplete, tree=tree_autocomplete)
async def tree_include(interaction: discord.Interaction, key: str, tree: str):
    m = db.get_milestone(interaction.guild_id, key)
    t = db.get_tree(interaction.guild_id, tree)
    if not m or not t:
        await interaction.response.send_message(
            "Check the milestone key and tree key — one of them doesn't exist.",
            ephemeral=True,
        )
        return
    db.add_to_tree(t["id"], m["id"])
    others = [x["name"] for x in db.trees_for_milestone(m["id"]) if x["id"] != t["id"]]
    msg = f"🌳 **{m['name']}** now appears in **{t['name']}**."
    if others:
        msg += f" It also sits in {', '.join(others)} — shared gates are fine."
    await interaction.response.send_message(msg)


@tree_group.command(name="exclude", description="Take a milestone out of a tree")
@app_commands.autocomplete(key=milestone_autocomplete, tree=tree_autocomplete)
async def tree_exclude(interaction: discord.Interaction, key: str, tree: str):
    m = db.get_milestone(interaction.guild_id, key)
    t = db.get_tree(interaction.guild_id, tree)
    if not m or not t:
        await interaction.response.send_message("Unknown milestone or tree.", ephemeral=True)
        return
    db.remove_from_tree(t["id"], m["id"])
    await interaction.response.send_message(
        f"Removed **{m['name']}** from **{t['name']}**. The milestone itself is untouched."
    )


@tree_group.command(name="drop", description="Delete a tree (its milestones survive)")
@app_commands.autocomplete(tree=tree_autocomplete)
@app_commands.default_permissions(manage_guild=True)
async def tree_drop(interaction: discord.Interaction, tree: str):
    t = db.get_tree(interaction.guild_id, tree)
    if not t:
        await interaction.response.send_message(f"No tree `{tree}`.", ephemeral=True)
        return
    db.delete_tree(t["id"])
    await interaction.response.send_message(
        f"🗑️ Dropped the **{t['name']}** view. Its milestones are now unfiled, not deleted."
    )


@tree_group.command(name="add", description="Add a milestone to the tech tree")
@app_commands.describe(
    key="Short slug, e.g. `permits`",
    name="Milestone name",
    unlocks="What becomes possible once this lands",
    requires="Comma-separated keys that must finish first",
    xp="XP awarded to contributors when it completes",
    tree="Which tree to file it under",
)
@app_commands.autocomplete(tree=tree_autocomplete)
async def tree_add(
    interaction: discord.Interaction,
    key: str,
    name: str,
    unlocks: str = "",
    requires: str = "",
    xp: app_commands.Range[int, 0, 5000] = 100,
    tree: str | None = None,
):
    if db.get_milestone(interaction.guild_id, key):
        await interaction.response.send_message(f"`{key}` already exists.", ephemeral=True)
        return
    mid = db.create_milestone(interaction.guild_id, key, name, unlocks, xp)
    missing = []
    for raw in filter(None, (r.strip() for r in requires.split(","))):
        pre = db.get_milestone(interaction.guild_id, raw)
        if pre:
            db.add_dep(mid, pre["id"])
        else:
            missing.append(raw)
    filed = ""
    if tree:
        t = db.get_tree(interaction.guild_id, tree)
        if t:
            db.add_to_tree(t["id"], mid)
            filed = f" in **{t['name']}**"
        else:
            filed = f" — ⚠️ no tree `{tree}`, left unfiled"
    msg = f"🌲 Added **{name}** (`{key}`, {xp} XP){filed}."
    if missing:
        msg += f"\n⚠️ Unknown prerequisite(s): {', '.join(missing)}"
    msg += f"\nLink the work with `/tree link key:{key} project:…`"
    await interaction.response.send_message(msg)


@tree_group.command(name="edit", description="Change a milestone's name, payoff, or XP")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.describe(
    name="New display name",
    unlocks="New description of what this makes possible",
    xp="New XP value",
)
async def tree_edit(
    interaction: discord.Interaction,
    key: str,
    name: str | None = None,
    unlocks: str | None = None,
    xp: app_commands.Range[int, 0, 5000] | None = None,
):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    if name is None and unlocks is None and xp is None:
        await interaction.response.send_message(
            f"**{m['name']}** — {m['unlocks'] or '*no payoff written*'} · {m['xp']} XP\n"
            f"Pass `name`, `unlocks`, or `xp` to change something.",
            ephemeral=True,
        )
        return
    db.update_milestone(m["id"], name=name, unlocks=unlocks, xp=xp)
    fresh = db.get_milestone(interaction.guild_id, key)
    await interaction.response.send_message(
        f"✏️ **{fresh['name']}** — {fresh['unlocks'] or '*no payoff written*'} · {fresh['xp']} XP"
    )


@tree_group.command(name="requires", description="Make one milestone depend on another")
@app_commands.autocomplete(key=milestone_autocomplete, prerequisite=milestone_autocomplete)
async def tree_requires(interaction: discord.Interaction, key: str, prerequisite: str):
    m = db.get_milestone(interaction.guild_id, key)
    p = db.get_milestone(interaction.guild_id, prerequisite)
    if not m or not p:
        await interaction.response.send_message("One of those keys doesn't exist.", ephemeral=True)
        return
    if m["id"] == p["id"]:
        await interaction.response.send_message("A milestone can't gate itself.", ephemeral=True)
        return
    db.add_dep(m["id"], p["id"])
    await interaction.response.send_message(
        f"🔗 **{m['name']}** now stays locked until **{p['name']}** is done."
    )


@tree_group.command(name="link", description="Attach a project to a milestone")
@app_commands.autocomplete(key=milestone_autocomplete, project=project_autocomplete)
async def tree_link(interaction: discord.Interaction, key: str, project: str):
    m = db.get_milestone(interaction.guild_id, key)
    p = await resolve(interaction, project)
    if not m or not p:
        if m:
            return
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    db.link_project(m["id"], p["id"])
    prog = db.milestone_progress(m["id"])
    await interaction.response.send_message(
        f"🔗 **{p['name']}** now counts toward **{m['name']}** — {prog['pct']}% there."
    )
    await push_unlocks(interaction)


@tree_group.command(name="complete", description="Close a milestone by hand")
@app_commands.autocomplete(key=milestone_autocomplete)
async def tree_complete(interaction: discord.Interaction, key: str):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    db.complete_milestone(m["id"])
    await interaction.response.send_message(f"✅ Marked **{m['name']}** complete.")
    await push_unlocks(interaction)


@tree_group.command(name="remove", description="Delete a milestone")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.default_permissions(manage_guild=True)
async def tree_remove(interaction: discord.Interaction, key: str):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    db.delete_milestone(m["id"])
    await interaction.response.send_message(f"🗑️ Removed **{m['name']}**.")


@bot.tree.command(name="next", description="What's closest to unlocking, and what's in the way")
@app_commands.guild_only()
@app_commands.describe(tree="Limit to one tree")
@app_commands.autocomplete(tree=tree_autocomplete)
async def next_up(interaction: discord.Interaction, tree: str | None = None):
    nodes = db.tree_view(interaction.guild_id, tree)
    if not nodes:
        await interaction.response.send_message(
            "No tech tree yet. Build one with `/tree add`.", ephemeral=True
        )
        return
    by_key = {n["key"]: n for n in nodes}
    e = discord.Embed(title="What's next", colour=discord.Color.gold())

    ready = [n for n in nodes
             if n["state"] in ("available", "active") and not n.get("external_from")]
    if ready:
        e.add_field(
            name="🟡 Open now",
            value="\n".join(
                f"**{n['name']}** — {n['pct']}% done"
                + (f", {n['remaining']} task(s) left" if n["remaining"] else "")
                + (f"\n↳ unlocks: {n['unlocks']}" if n["unlocks"] else "")
                for n in ready[:6]
            ),
            inline=False,
        )

    locked = [n for n in nodes if n["state"] == "locked" and not n.get("external_from")]
    scored = []
    for n in locked:
        cost = sum(by_key[p]["remaining"] for p in n["blocked_by"] if p in by_key)
        scored.append((cost, n))
    scored.sort(key=lambda s: s[0])
    if scored:
        lines = []
        for cost, n in scored[:5]:
            gates = ", ".join(by_key[p]["name"] for p in n["blocked_by"] if p in by_key)
            lines.append(
                f"🔒 **{n['name']}** — {cost} task(s) away\n↳ needs: {gates}"
            )
        e.add_field(name="Closest unlocks", value="\n".join(lines), inline=False)

    own = [n for n in nodes if not n.get("external_from")]
    done = sum(1 for n in own if n["state"] == "complete")
    e.set_footer(text=f"{done} of {len(own)} milestones unlocked")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="leaderboard", description="XP earned from unlocked milestones")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = db.leaderboard(interaction.guild_id)
    if not rows:
        await interaction.response.send_message(
            "No XP yet — it's minted when a milestone unlocks, not when a task closes.",
            ephemeral=True,
        )
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{r['xp']} XP** "
        f"· {r['unlocks']} milestone(s)"
        for i, r in enumerate(rows)
    ]
    e = discord.Embed(
        title="Leaderboard",
        description="\n".join(lines),
        colour=discord.Color.gold(),
    )
    e.set_footer(text="XP is split across everyone whose tasks fed the milestone.")
    await interaction.response.send_message(embed=e)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment first.")
    bot.run(TOKEN)
