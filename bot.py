"""Discord project progress tracker.

Slash commands:
    /project new | list | view | log | archive | unarchive | delete
    /task add | done | status | assign | list | delete
    /me                     -> your open tasks across all projects
    /digest set             -> weekly summary posted to a channel

Run with:  DISCORD_TOKEN=... python bot.py
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import seed
import tree_render
import wizard

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # set for instant command sync on one server
STATUS_EMOJI = {"todo": "⬜", "doing": "🔵", "blocked": "🔴", "done": "✅"}
STATE_EMOJI = {"locked": "🔒", "available": "🟡", "active": "🔵",
               "pending": "🟣", "complete": "✅"}
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
@app_commands.describe(name="Short unique name", description="What is this project?",
                       group="Group it belongs to", region="Region", team="Team")
async def project_new(interaction: discord.Interaction, name: str, description: str = "",
                      group: str = "Universal", region: str = "Universal",
                      team: str = "Universal"):
    if db.get_project(interaction.guild_id, name):
        await interaction.response.send_message(
            f"**{name}** already exists.", ephemeral=True
        )
        return
    for val in (group, region, team):
        if val.strip().lower() == "universal" and val != "Universal":
            pass
    pid = db.create_project(interaction.guild_id, name, description, interaction.user.id)
    db.set_project_tags(pid, grp=group, region=region, team=team)
    tags = " · ".join(v for v in (group, region, team) if v != "Universal")
    await interaction.response.send_message(
        f"📁 Created **{name}**" + (f" ({tags})" if tags else "") +
        f". Add a tree with `/tree new` or work with `/task add project:{name} title:…`"
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
    impact = db.project_delete_impact(p["id"])
    lines = [f"The project **{p['name']}**", f"{impact['tasks']} task(s) inside it"]
    note = ""
    if impact["milestones"]:
        note = ("Milestones fed by this project will drop to 0% and may reopen: "
                + ", ".join(f"**{x}**" for x in impact["milestones"]))

    async def do_delete(i: discord.Interaction):
        db.delete_project(p["id"])
        await i.followup.send(f"🗑️ Deleted **{p['name']}** and its {impact['tasks']} task(s).")

    await interaction.response.send_message(
        embed=wizard.danger_embed(f"Delete {p['name']}?", lines, note),
        view=wizard.DangerConfirm(interaction.user.id, do_delete),
    )


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

    feeds = db.milestones_for_project(p["id"])
    if feeds:
        where = ", ".join(f"**{m['name']}**" for m in feeds)
        tail = f"\nFeeds {where}."
    else:
        tail = ("\n⚠️ *{}* isn't attached to any milestone, so this work won't move "
                "the tree. Wire it up with `/tree link`.".format(p["name"]))

    await interaction.response.send_message(
        f"➕ `#{tid}` **{title}** → *{p['name']}*"
        f"{f' · <@{assignee.id}>' if assignee else ''}{due_label(due_iso)}\n"
        f"`{bar(prog['pct'])}` {prog['pct']}%{tail}"
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


config_group = app_commands.Group(
    name="config", description="Server settings", guild_only=True
)


test_group = app_commands.Group(
    name="test", description="Run live tracker checks", guild_only=True
)


@config_group.command(name="signoff", description="Which role may sign off milestones")
@app_commands.describe(role="Leave blank to restrict sign-off to Manage Server only")
@app_commands.default_permissions(manage_guild=True)
async def config_signoff(interaction: discord.Interaction, role: discord.Role | None = None):
    db.set_signoff_role(interaction.guild_id, role.id if role else None)
    if role:
        await interaction.response.send_message(
            f"🖊️ {role.mention} can now sign off milestones, alongside server managers."
        )
    else:
        await interaction.response.send_message(
            "🖊️ Sign-off is now limited to people with Manage Server."
        )


@config_group.command(name="layout", description="Which way the tree should read")
@app_commands.describe(orientation="Left to right suits wide trees; top to bottom suits deep ones and phones")
@app_commands.choices(orientation=[
    app_commands.Choice(name="left to right", value="lr"),
    app_commands.Choice(name="top to bottom", value="tb"),
])
@app_commands.default_permissions(manage_guild=True)
async def config_layout(interaction: discord.Interaction,
                        orientation: app_commands.Choice[str]):
    db.set_layout(interaction.guild_id, orientation.value)
    await interaction.response.send_message(
        f"🧭 Trees will now render **{orientation.name}**. "
        f"`/tree show orientation:` overrides it for a single image."
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
        self._command_cleanup_done = False

    async def setup_hook(self):
        db.connect()
        self.tree.add_command(project_group)
        self.tree.add_command(task_group)
        self.tree.add_command(digest_group)
        self.tree.add_command(tree_group)
        self.tree.add_command(config_group)
        self.tree.add_command(test_group)
        if GUILD_ID:                      # instant, scoped to one server
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        else:                             # global: can take up to an hour to appear
            await self.tree.sync()
        self.digest_loop.start()

    async def on_ready(self):
        if not GUILD_ID and not self._command_cleanup_done:
            if len(self.guilds) == 1:
                # A one-server test bot should not make us wait for global sync.
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                guild = discord.Object(id=self.guilds[0].id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                for guild in self.guilds:
                    await self.http.bulk_upsert_guild_commands(
                        self.application_id, guild.id, []
                    )
            self._command_cleanup_done = True
        print(f"Logged in as {self.user} · {len(self.guilds)} guild(s)")

    @tasks.loop(minutes=30)
    async def digest_loop(self):
        now = datetime.now(timezone.utc)
        for s in await asyncio.to_thread(db.all_digest_guilds):
            if now.weekday() != s["digest_weekday"] or now.hour != s["digest_hour"]:
                continue
            if s["last_digest"]:
                last = datetime.fromisoformat(s["last_digest"])
                if (now - last).total_seconds() < 60 * 60 * 20:
                    continue
            channel = self.get_channel(s["digest_channel"])
            if channel is None:
                continue
            embed = await asyncio.to_thread(self.build_digest, s["guild_id"])
            if embed:
                await channel.send(embed=embed)
                await asyncio.to_thread(db.mark_digest_sent, s["guild_id"])

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


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    """Without this, any unhandled exception shows the user 'The application did
    not respond' and the reason only reaches the journal."""
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down a moment — try again in {error.retry_after:.0f}s."
    elif isinstance(error, (app_commands.MissingPermissions,
                            app_commands.CheckFailure)):
        msg = "You don't have permission to do that here."
    elif isinstance(error, app_commands.TransformerError):
        msg = "One of those values wasn't in the right format."
    else:
        inner = getattr(error, "original", error)
        logging.exception("command %s failed",
                          interaction.command.qualified_name if interaction.command else "?",
                          exc_info=inner)
        msg = (f"Something went wrong running that — `{type(inner).__name__}`. "
               f"It's been logged; `journalctl -u tracker -n 50` has the detail.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.command(name="me", description="Your open tasks across every project")
@app_commands.guild_only()
async def me(interaction: discord.Interaction):
    rows = db.my_tasks(interaction.guild_id, interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You're all clear. 🎉\n"
            + standing_line(interaction.guild_id, interaction.user.id),
            ephemeral=True,
        )
        return
    e = discord.Embed(
        title="Your open tasks",
        description="\n".join(task_line(t, show_project=True) for t in rows[:40]),
        colour=discord.Color.blurple(),
    )
    e.add_field(name="Standing", value=standing_line(interaction.guild_id,
                                                     interaction.user.id), inline=False)
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


def level_up_embed(up: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"⭐  Level {up['to']['index']} — {up['to']['name']}",
        description=f"<@{up['user_id']}> moved up from **{up['from']['name']}**.",
        colour=discord.Color.gold(),
    )
    if up["to"]["perk"]:
        e.add_field(name="Comes with", value=up["to"]["perk"], inline=False)
    return e


def signoff_embed(m: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"🟣  {m['name']}",
        description="**Every task is done — but this one waits for a human.**\n"
                    "Nothing downstream unlocks and no XP is paid out until "
                    f"someone runs `/tree confirm key:{m['key']}`.",
        colour=discord.Color.purple(),
    )
    if m.get("unlocks"):
        e.add_field(name="Will unlock", value=m["unlocks"], inline=False)
    e.set_footer(text="Switch it to auto with /tree edit auto_close:True")
    return e


def pending_unlocks(guild_id: int) -> list[discord.Embed]:
    """Settle finished milestones, and flag ones waiting on a sign-off."""
    state = db.tree_state(guild_id)
    by_key = {m["key"]: m for m in state}
    out = []
    for m in state:
        if m["state"] == "pending":
            if not db.pending_notified(m["id"]):
                db.mark_pending_notified(m["id"], True)
                out.append(signoff_embed(m))
            continue
        if m["state"] != "complete":
            # work reopened — let it announce again if it comes back
            if db.pending_notified(m["id"]):
                db.mark_pending_notified(m["id"], False)
            continue
        if m["settled"]:
            continue
        awards = db.settle_milestone(guild_id, m["id"], m["xp"])
        ups = db.apply_level_ups(guild_id, awards)
        newly = [
            n for n in state
            if m["key"] in n["prereqs"]
            and all(by_key[p]["state"] == "complete" for p in n["prereqs"])
        ]
        out.append(unlock_embed(m, awards, newly))
        out += [level_up_embed(u) for u in ups]     # after the unlock that caused it
    return out


async def push_unlocks(interaction: discord.Interaction) -> None:
    embeds = await asyncio.to_thread(pending_unlocks, interaction.guild_id)
    for e in embeds:
        await interaction.followup.send(embed=e)


# keyed per guild — the same person can hold different nicknames in different
# servers — and expired so a rename shows up within the hour
_name_cache: dict[tuple[int, int], tuple[str, float]] = {}
_NAME_TTL = 3600.0


def standing_line(guild_id: int, user_id: int) -> str:
    xp = db.user_xp(guild_id, user_id)
    lv = db.level_for(guild_id, xp)
    if lv["next_at"] is None:
        return f"**{lv['name']}** · {xp} XP · top of the ladder"
    return (f"**{lv['name']}** · {xp} XP\n`{bar(lv['pct'])}` "
            f"{lv['next_at'] - xp} XP to {lv['next_name']}")


def role_ids(interaction: discord.Interaction) -> set[int]:
    return {r.id for r in getattr(interaction.user, "roles", [])}


def is_manager(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


def may_run(interaction: discord.Interaction, command: str) -> bool:
    """A command with no configured gate is open. With one, you need the role or
    Manage Server (which can never be locked out)."""
    if is_manager(interaction):
        return True
    gate = db.get_cmd_perm(interaction.guild_id, command)
    return gate is None or gate in role_ids(interaction)


async def deny(interaction: discord.Interaction, command: str) -> None:
    gate = db.get_cmd_perm(interaction.guild_id, command)
    who = f"<@&{gate}>" if gate else "a server manager"
    msg = f"`/{command.replace('_', ' ')}` is limited to {who} here."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def may_set_universal(interaction: discord.Interaction) -> bool:
    if is_manager(interaction):
        return True
    role = db.get_universal_role(interaction.guild_id)
    return role is not None and role in role_ids(interaction)


def may_sign_off(interaction: discord.Interaction) -> bool:
    """Server managers always may. Otherwise a configured role is required.

    Sign-off exists so a person with judgement agrees the thing is really done;
    leaving it open to everyone would defeat the point.
    """
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = db.get_signoff_role(interaction.guild_id)
    if role_id is None:
        return False
    return any(r.id == role_id for r in interaction.user.roles)


async def deny_signoff(interaction: discord.Interaction) -> None:
    role_id = db.get_signoff_role(interaction.guild_id)
    who = f"<@&{role_id}>" if role_id else "someone with Manage Server"
    await interaction.response.send_message(
        f"Signing off is limited to {who}. "
        f"A server manager can widen that with `/config signoff`.",
        ephemeral=True,
    )


def parse_people(guild: discord.Guild, text: str) -> tuple[list[int], list[str]]:
    """Accepts @mentions, raw IDs, or plain names. Returns (ids, unmatched)."""
    ids, missing = [], []
    for token in filter(None, (t.strip() for t in re.split(r"[,\n]| and ", text or ""))):
        if m := re.fullmatch(r"<@!?(\d+)>", token):
            ids.append(int(m.group(1)))
            continue
        if token.isdigit():
            ids.append(int(token))
            continue
        names.append(token)
    return list(dict.fromkeys(ids)), names


async def resolve_names(guild: discord.Guild, names: list[str]) -> tuple[list[int], list[str]]:
    """Plain names need a lookup. The member cache is mostly empty without the
    privileged intent, so fall back to a gateway query and give up gracefully."""
    found, missing = [], []
    for token in names:
        needle = token.lstrip("@").lower()
        hit = discord.utils.find(
            lambda mem: needle in (mem.display_name.lower(), mem.name.lower()),
            guild.members,
        )
        if hit is None:
            try:
                matches = await guild.query_members(query=token.lstrip("@"), limit=5)
                hit = next((m for m in matches
                            if needle in (m.display_name.lower(), m.name.lower())), None)
                hit = hit or (matches[0] if matches else None)
            except Exception:
                hit = None
        (found.append(hit.id) if hit else missing.append(token))
    return found, missing


async def display_names(guild: discord.Guild, ids: list[int]) -> list[str]:
    """IDs -> display names. Falls back to a REST fetch when the member isn't
    cached, so this works without the privileged members intent."""
    out = []
    now = time.monotonic()
    for uid in ids:
        hit = _name_cache.get((guild.id, uid))
        if hit and now - hit[1] < _NAME_TTL:
            out.append(hit[0])
            continue
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        label = member.display_name if member else f"user {str(uid)[-4:]}"
        _name_cache[(guild.id, uid)] = (label, now)
        out.append(label)
    return out


async def tree_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.list_trees(interaction.guild_id)
    return [
        app_commands.Choice(name=f"{r['name']} ({r['key']})"[:100], value=r["key"])
        for r in rows
        if current.lower() in (r["key"] + r["name"]).lower()
    ][:25]


class OrientationView(discord.ui.View):
    """Re-renders the same tree the other way round, in place."""

    def __init__(self, guild_id: int, tree_key: str | None, title: str, mode: str):
        super().__init__(timeout=900)
        self.guild_id, self.tree_key, self.title = guild_id, tree_key, title
        self.mode = mode
        self.flip.label = ("Show top to bottom" if mode == "lr"
                           else "Show left to right")

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🧭")
    async def flip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.mode = "tb" if self.mode == "lr" else "lr"
        button.label = ("Show top to bottom" if self.mode == "lr"
                        else "Show left to right")
        nodes = await asyncio.to_thread(db.tree_view, self.guild_id, self.tree_key)
        for n in nodes:
            n["people"] = await display_names(interaction.guild, n.get("people") or [])
            if n["state"] == "complete" and n.get("completed_at"):
                when = datetime.fromisoformat(n["completed_at"]).strftime("%d %b")
                who = (await display_names(interaction.guild, [n["completed_by"]]))[0] \
                    if n.get("completed_by") else "auto"
                n["closed_label"] = f"closed by {who} · {when}"
        edges = await asyncio.to_thread(db.tree_edges, self.guild_id, nodes)
        buf = await asyncio.to_thread(
            tree_render.render_tree, nodes, edges, self.title, self.mode)
        await interaction.edit_original_response(
            attachments=[discord.File(buf, filename="techtree.png")], view=self)


@tree_group.command(name="show", description="Render a tech tree")
@app_commands.autocomplete(tree=tree_autocomplete)
@app_commands.describe(
    tree="Which tree to draw — leave blank for everything at once",
    orientation="Override the server default for this one image",
)
@app_commands.choices(orientation=[
    app_commands.Choice(name="left to right", value="lr"),
    app_commands.Choice(name="top to bottom", value="tb"),
])
async def tree_show(interaction: discord.Interaction, tree: str | None = None,
                    orientation: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    if tree and not db.get_tree(interaction.guild_id, tree):
        await interaction.followup.send(
            f"No tree called `{tree}`. `/tree list` shows what exists.", ephemeral=True
        )
        return
    nodes = await asyncio.to_thread(db.tree_view, interaction.guild_id, tree)
    if not nodes:
        await interaction.followup.send(
            "That tree is empty. Add milestones with `/tree add tree:…`.", ephemeral=True
        )
        return
    for n in nodes:
        n["people"] = await display_names(interaction.guild, n.get("people") or [])
        if n["state"] == "complete" and n.get("completed_at"):
            when = datetime.fromisoformat(n["completed_at"]).strftime("%d %b")
            who = (await display_names(interaction.guild, [n["completed_by"]]))[0] \
                if n.get("completed_by") else "auto"
            n["closed_label"] = f"closed by {who} · {when}"
    t = db.get_tree(interaction.guild_id, tree) if tree else None
    title = t["name"] if t else f"{interaction.guild.name} — everything"
    edges = await asyncio.to_thread(db.tree_edges, interaction.guild_id, nodes)
    mode = orientation.value if orientation else \
        await asyncio.to_thread(db.get_layout, interaction.guild_id)
    # Pillow is CPU-bound; rendering a large tree inline would stall the
    # gateway heartbeat and can drop the bot's connection
    buf = await asyncio.to_thread(tree_render.render_tree, nodes, edges, title, mode)
    ready = [n["name"] for n in nodes
             if n["state"] == "available" and not n.get("external_from")]
    note = ("**Ready to start:** " + ", ".join(ready)) if ready else ""
    await interaction.followup.send(
        content=note, file=discord.File(buf, filename="techtree.png"),
        view=OrientationView(interaction.guild_id, tree, title, mode),
    )


@tree_group.command(name="new", description="Create a named tree")
@app_commands.describe(key="Short slug used in commands", name="Display name",
                       project="Project this tree belongs to (inherits its tags)",
                       group="Group (overrides the project's)", region="Region", team="Team")
@app_commands.autocomplete(project=project_autocomplete)
async def tree_new(interaction: discord.Interaction, key: str, name: str,
                   description: str = "", project: str | None = None,
                   group: str | None = None, region: str | None = None,
                   team: str | None = None):
    if db.get_tree(interaction.guild_id, key):
        await interaction.response.send_message(f"`{key}` already exists.", ephemeral=True)
        return
    tid = db.create_tree(interaction.guild_id, key, name, description)
    # inherit from the project, then let explicit args override
    proj = db.get_project(interaction.guild_id, project) if project else None
    grp = group or (proj["grp"] if proj else "Universal")
    reg = region or (proj["region"] if proj else "Universal")
    tm = team or (proj["team"] if proj else "Universal")
    db.set_tree_tags(tid, grp=grp, region=reg, team=tm)
    if proj:
        db.link_tree_project(tid, proj["id"])
    tags = " · ".join(v for v in (grp, reg, tm) if v != "Universal")
    await interaction.response.send_message(
        f"🌳 Created tree **{name}** (`{key}`)" + (f" — {tags}" if tags else "") + ".\n"
        f"Add milestones with `/tree add tree:{key} …`, or file existing ones with "
        f"`/tree include`."
    )


@tree_group.command(name="note", description="Append a timestamped note to a milestone")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.describe(key="Milestone key", note="What changed")
async def tree_note(interaction: discord.Interaction, key: str, note: str):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    # a private description can only be appended to by those who can read it
    if m["private"] and not db.can_read_description(
        interaction.guild_id, m["id"], interaction.user.id,
        role_ids(interaction), is_manager(interaction)
    ):
        await interaction.response.send_message(
            "That milestone's description is private — you're not on it.", ephemeral=True)
        return
    line = db.append_milestone_note(m["id"], interaction.user.id, note)
    await interaction.response.send_message(
        f"📝 Logged on **{m['name']}**:\n{line}")


@tree_group.command(name="list", description="Every tree and how far along it is")
async def tree_list(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await asyncio.to_thread(db.tree_summary, interaction.guild_id)
    unfiled = db.unfiled_milestones(interaction.guild_id)
    if not rows and not unfiled:
        await interaction.followup.send(
            "No trees yet. Start one with `/start`.", ephemeral=True
        )
        return
    e = discord.Embed(title="Trees", colour=discord.Color.blurple())
    FIELD_CAP = 23                      # Discord rejects embeds past 25 fields
    overflow = rows[FIELD_CAP:]
    for r in rows[:FIELD_CAP]:
        val = f"`{bar(r['pct'])}` {r['pct']}% — {r['done']}/{r['total']} milestones"
        if r["ready"]:
            val += "\n🟡 ready: " + ", ".join(r["ready"][:3])
        if r["description"]:
            val += f"\n*{r['description']}*"
        e.add_field(name=f"{r['name']}  (`{r['key']}`)", value=val, inline=False)
    if overflow:
        e.add_field(
            name=f"…and {len(overflow)} more",
            value=", ".join(f"`{r['key']}`" for r in overflow[:40]),
            inline=False,
        )
    if unfiled:
        e.add_field(
            name="Unfiled",
            value=f"{len(unfiled)} milestone(s) not in any tree — "
                  f"they only appear in `/tree show` with no argument.",
            inline=False,
        )
    await interaction.followup.send(embed=e)


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
    members = db.tree_members(t["id"])

    async def do_drop(i: discord.Interaction):
        db.delete_tree(t["id"])
        await i.followup.send(
            f"🗑️ Dropped the **{t['name']}** view. "
            f"Its {len(members)} milestone(s) are now unfiled, not deleted."
        )

    await interaction.response.send_message(
        embed=wizard.danger_embed(
            f"Drop the {t['name']} view?",
            [f"The **{t['name']}** grouping"],
            f"Its {len(members)} milestone(s) survive as unfiled and keep all progress.",
        ),
        view=wizard.DangerConfirm(interaction.user.id, do_drop, label="Drop it"),
    )


@tree_group.command(name="add", description="Add a milestone to the tech tree")
@app_commands.describe(
    key="Short slug, e.g. `permits`",
    name="Milestone name",
    description="What this milestone actually is",
    auto_close="True: completes itself at 100%. False: waits for /tree confirm.",
    unlocks="What becomes possible once this lands",
    requires="Comma-separated keys that must finish first",
    xp="XP awarded to contributors when it completes",
    tree="Which tree to file it under",
    difficulty="1-10, half steps allowed (default 1)",
    private="Hide the description from anyone but assignees and permitted roles",
)
@app_commands.autocomplete(tree=tree_autocomplete)
async def tree_add(
    interaction: discord.Interaction,
    key: str,
    name: str,
    description: str = "",
    unlocks: str = "",
    requires: str = "",
    xp: app_commands.Range[int, 0, 5000] = 100,
    tree: str | None = None,
    auto_close: bool = True,
    difficulty: app_commands.Range[float, 1.0, 10.0] = 1.0,
    private: bool = False,
):
    if not may_run(interaction, "tree_add"):
        await deny(interaction, "tree_add")
        return
    if db.get_milestone(interaction.guild_id, key):
        await interaction.response.send_message(f"`{key}` already exists.", ephemeral=True)
        return
    # inherit group/region/team from the tree it's filed under
    t = db.get_tree(interaction.guild_id, tree) if tree else None
    grp = t["grp"] if t else "Universal"
    region = t["region"] if t else "Universal"
    team = t["team"] if t else "Universal"
    mid = db.create_milestone(interaction.guild_id, key, name, unlocks, xp, description,
                              auto_close, difficulty, private, grp, region, team)
    stubbed, looped = [], []
    for raw in filter(None, (r.strip() for r in requires.split(","))):
        rid, created = db.find_or_stub(interaction.guild_id, raw)
        if rid == mid:
            continue
        if not db.add_dep(mid, rid):
            looped.append(raw)
            continue
        if created:
            stubbed.append(raw)
    filed = ""
    if tree:
        if t:
            db.add_to_tree(t["id"], mid)
            filed = f" in **{t['name']}**"
        else:
            filed = f" — ⚠️ no tree `{tree}`, left unfiled"
    gate = "closes itself at 100%" if auto_close else "waits for `/tree confirm`"
    dpips = f" · difficulty {difficulty:g}" if difficulty != 1 else ""
    lock = " · 🔒 private" if private else ""
    msg = f"🌲 Added **{name}** (`{key}`, {xp} XP){filed} — {gate}{dpips}{lock}."
    if stubbed:
        for raw in stubbed:
            sid, _ = db.find_or_stub(interaction.guild_id, raw)
            if tree and t:
                db.add_to_tree(t["id"], sid)
        msg += f"\n🌱 Stubbed in: {', '.join(stubbed)} — describe them with `/tree edit`."
    if looped:
        msg += f"\n⚠️ Skipped {', '.join(looped)} — depending on those would make a loop."
    msg += f"\nLink the work with `/tree link key:{key} project:…`"
    await interaction.response.send_message(msg)


@tree_group.command(name="edit", description="Change a milestone's name, payoff, or XP")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.describe(
    name="New display name",
    description="New description of what this milestone is",
    unlocks="New description of what this makes possible",
    xp="New XP value",
    auto_close="True: completes itself at 100%. False: waits for /tree confirm.",
    difficulty="1-10, half steps allowed",
    private="Hide the description from anyone but assignees and permitted roles",
    group="Reassign the group",
    region="Reassign the region",
    team="Reassign the team",
)
async def tree_edit(
    interaction: discord.Interaction,
    key: str,
    name: str | None = None,
    description: str | None = None,
    unlocks: str | None = None,
    xp: app_commands.Range[int, 0, 5000] | None = None,
    auto_close: bool | None = None,
    difficulty: app_commands.Range[float, 1.0, 10.0] | None = None,
    private: bool | None = None,
    group: str | None = None,
    region: str | None = None,
    team: str | None = None,
):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    nothing = all(v is None for v in (name, description, unlocks, xp, auto_close,
                                      difficulty, private, group, region, team))
    if nothing:
        gate = "closes itself at 100%" if m["auto_close"] else "waits for `/tree confirm`"
        await interaction.response.send_message(
            f"**{m['name']}** · {m['xp']} XP · {gate} · difficulty {m['difficulty']:g}"
            f"{' · 🔒 private' if m['private'] else ''}\n"
            f"tags: {m['grp']} · {m['region']} · {m['team']}\n"
            f"is: {m['description'] or '*no description*'}\n"
            f"unlocks: {m['unlocks'] or '*no payoff written*'}\n"
            f"Pass any of name, description, unlocks, xp, auto_close, difficulty, "
            f"private, group, region, team to change something.",
            ephemeral=True,
        )
        return
    # setting a tag to Universal is a privileged act
    for val in (group, region, team):
        if val and val.strip().lower() == "universal" and not may_set_universal(interaction):
            await interaction.response.send_message(
                "Setting something **Universal** is limited — see `/config universal-role`.",
                ephemeral=True)
            return
    db.update_milestone(m["id"], name=name, description=description,
                        unlocks=unlocks, xp=xp,
                        auto_close=None if auto_close is None else int(auto_close))
    if difficulty is not None:
        db.set_difficulty(m["id"], difficulty)
    if private is not None:
        db.set_private(m["id"], private)
    if any(v is not None for v in (group, region, team)):
        db.set_milestone_tags(m["id"], grp=group, region=region, team=team)
    if m["is_stub"] and (description or unlocks):
        db.clear_stub(m["id"])
    fresh = db.get_milestone(interaction.guild_id, key)
    gate = "closes itself at 100%" if fresh["auto_close"] else "waits for `/tree confirm`"
    await interaction.response.send_message(
        f"✏️ **{fresh['name']}** · {fresh['xp']} XP · {gate} · "
        f"difficulty {fresh['difficulty']:g}{' · 🔒 private' if fresh['private'] else ''}\n"
        f"tags: {fresh['grp']} · {fresh['region']} · {fresh['team']}\n"
        f"is: {fresh['description'] or '*no description*'}\n"
        f"unlocks: {fresh['unlocks'] or '*no payoff written*'}"
    )
    await push_unlocks(interaction)


@tree_group.command(name="requires", description="Make one milestone depend on another")
@app_commands.autocomplete(key=milestone_autocomplete, prerequisite=milestone_autocomplete)
async def tree_requires(interaction: discord.Interaction, key: str, prerequisite: str):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    pid, created = db.find_or_stub(interaction.guild_id, prerequisite)
    if m["id"] == pid:
        await interaction.response.send_message("A milestone can't gate itself.", ephemeral=True)
        return
    if not db.add_dep(m["id"], pid):
        await interaction.response.send_message(
            f"That would make a loop — **{m['name']}** already sits upstream of "
            f"that milestone, so each would wait on the other forever.",
            ephemeral=True,
        )
        return
    for t in db.trees_for_milestone(m["id"]):
        db.add_to_tree(t["id"], pid)
    p = db.get_milestone(interaction.guild_id, prerequisite) or \
        [x for x in db.list_milestones(interaction.guild_id) if x["id"] == pid][0]
    msg = f"🔗 **{m['name']}** now stays locked until **{p['name']}** is done."
    if created:
        msg += (f"\n🌱 **{p['name']}** didn't exist, so I stubbed it in. "
                f"Describe it with `/tree edit key:{p['key']}`.")
    # cross-group links are allowed but announced, so the other group sees it
    if p["grp"] not in ("Universal", m["grp"]) or m["grp"] not in ("Universal", p["grp"]):
        msg += (f"\n📣 Cross-group link: **{m['grp']}** now depends on "
                f"**{p['grp']}**'s milestone. Both groups can see this.")
    await interaction.response.send_message(msg)


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


@tree_group.command(name="confirm", description="Sign off a milestone that's waiting on you")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.describe(
    credit="Who to split the XP between — @mentions or names, comma separated"
)
async def tree_confirm(interaction: discord.Interaction, key: str, credit: str = ""):
    if not may_sign_off(interaction):
        await deny_signoff(interaction)
        return
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    node = next((n for n in db.tree_state(interaction.guild_id) if n["id"] == m["id"]), None)
    if node and node["state"] == "complete":
        await interaction.response.send_message(
            f"**{m['name']}** is already signed off.", ephemeral=True
        )
        return
    ids, names = parse_people(interaction.guild, credit)
    extra, missing = await resolve_names(interaction.guild, names)
    ids += extra
    db.complete_milestone(m["id"], interaction.user.id, ids or None)
    warn = ""
    if ids:
        each = m["xp"] // len(ids)
        warn += f"\nSplitting {m['xp']} XP evenly — {each} each to {len(ids)} people."
    if missing:
        warn += (f"\n⚠️ Couldn't find: {', '.join(missing)} — they got nothing. "
                 f"@mentions are matched reliably; plain names are not.")
    if node and node["pct"] < 100:
        warn = (f"\n⚠️ Only {node['pct']}% of its tasks were done — "
                f"confirming anyway, which is a legitimate override but worth saying out loud.")
    await interaction.response.send_message(
        f"🖊️ <@{interaction.user.id}> signed off **{m['name']}**.{warn}"
    )
    await push_unlocks(interaction)


@tree_group.command(name="complete", description="Close a milestone by hand")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.describe(credit="Who to split the XP between — @mentions or names")
async def tree_complete(interaction: discord.Interaction, key: str, credit: str = ""):
    if not may_sign_off(interaction):
        await deny_signoff(interaction)
        return
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    ids, names = parse_people(interaction.guild, credit)
    extra, missing = await resolve_names(interaction.guild, names)
    ids += extra
    db.complete_milestone(m["id"], interaction.user.id, ids or None)
    tail = f" Splitting XP between {len(ids)} people." if ids else ""
    if missing:
        tail += f" ⚠️ Couldn't find: {', '.join(missing)}."
    await interaction.response.send_message(
        f"✅ <@{interaction.user.id}> marked **{m['name']}** complete.{tail}"
    )
    await push_unlocks(interaction)


@tree_group.command(name="import", description="Load a plan from an attached spreadsheet")
@app_commands.describe(file="A .csv or .yaml plan — drag it straight onto the message box")
@app_commands.default_permissions(manage_guild=True)
async def tree_import(interaction: discord.Interaction, file: discord.Attachment):
    if not file.filename.lower().endswith((".csv", ".tsv", ".yaml", ".yml")):
        await interaction.response.send_message(
            "That needs to be a `.csv` or `.yaml` file. Build one at `planner.html` "
            "if you don't have one yet.", ephemeral=True,
        )
        return
    if file.size > 1_000_000:
        await interaction.response.send_message(
            "That file is too big — plans should be a few kilobytes.", ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        text = (await file.read()).decode("utf-8-sig", errors="replace")
        doc = seed.parse(text, file.filename)
    except Exception as err:
        await interaction.followup.send(
            f"Couldn't read that file: `{type(err).__name__}`. "
            f"If you exported from a spreadsheet, choose **CSV** rather than the "
            f"native format.", ephemeral=True,
        )
        return

    pv = seed.preview(doc, interaction.guild_id)
    embed = wizard.preview_embed(pv, file.filename)
    if embed.colour == discord.Color.red() and not pv["created"] and not pv["updated"]:
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    await interaction.followup.send(
        embed=embed, view=wizard.ImportConfirm(doc, interaction.user.id, file.filename)
    )


@tree_group.command(name="history",
                    description="Closures across a tree, or one milestone's full timeline")
@app_commands.autocomplete(tree=tree_autocomplete, key=milestone_autocomplete)
@app_commands.describe(tree="Limit to one tree (whole-server if blank)",
                       key="A milestone key — shows its closures and notes together")
async def tree_history(interaction: discord.Interaction, tree: str | None = None,
                       key: str | None = None):
    # with a key, show that one milestone's full story: closure + note trail
    if key:
        m = db.get_milestone(interaction.guild_id, key)
        if not m:
            await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
            return
        if m["private"] and not db.can_read_description(
            interaction.guild_id, m["id"], interaction.user.id,
            role_ids(interaction), is_manager(interaction)
        ):
            await interaction.response.send_message(
                "That milestone's log is private — you're not on it.", ephemeral=True)
            return
        events = []
        if m["completed_at"]:
            who = f"<@{m['completed_by']}>" if m["completed_by"] else "auto"
            events.append((m["completed_at"], f"✅ **closed** by {who}"))
        for r in db.milestone_audit(m["id"], limit=50):
            events.append((r["created_at"], f"📝 <@{r['author_id']}>: {r['body']}"))
        events.sort(key=lambda e: e[0])
        if not events:
            await interaction.response.send_message(
                f"Nothing logged on **{m['name']}** yet. Add a note with "
                f"`/tree note key:{key}`.", ephemeral=True)
            return
        lines = []
        for ts, text in events:
            stamp = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"`{stamp}` {text}")
        e = discord.Embed(
            title=f"{m['name']} — timeline",
            description="\n".join(lines)[:4000],
            colour=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=e, ephemeral=True)
        return

    # no key: the closure log for the tree (or the whole server)
    rows = db.closure_history(interaction.guild_id, tree)
    if not rows:
        await interaction.response.send_message(
            "Nothing has been closed yet. Pass a `key` to see one milestone's notes.",
            ephemeral=True,
        )
        return
    lines = []
    for r in rows[:20]:
        when = int(datetime.fromisoformat(r["completed_at"]).timestamp())
        who = f"<@{r['completed_by']}>" if r["completed_by"] else "auto"
        credited = [c for c in (r["credited"] or "").split(",") if c]
        share = ""
        if credited:
            share = " · credited " + ", ".join(f"<@{c}>" for c in credited[:4])
            if len(credited) > 4:
                share += f" +{len(credited) - 4}"
        lines.append(f"**{r['name']}** — <t:{when}:D> by {who}{share}")
    e = discord.Embed(
        title="Closure history" + (f" — {tree}" if tree else ""),
        description="\n".join(lines),
        colour=discord.Color.green(),
    )
    e.set_footer(text=f"{len(rows)} milestone(s) closed · pass a key for one milestone's notes")
    await interaction.response.send_message(embed=e)


@tree_group.command(name="remove", description="Delete a milestone")
@app_commands.autocomplete(key=milestone_autocomplete)
@app_commands.default_permissions(manage_guild=True)
async def tree_remove(interaction: discord.Interaction, key: str):
    m = db.get_milestone(interaction.guild_id, key)
    if not m:
        await interaction.response.send_message(f"No milestone `{key}`.", ephemeral=True)
        return
    node = next((n for n in db.tree_state(interaction.guild_id) if n["id"] == m["id"]), None)
    dependents = [n["name"] for n in db.tree_state(interaction.guild_id)
                  if m["key"] in n["prereqs"]]
    lines = [f"The milestone **{m['name']}**", "its dependency links"]
    if node and node["state"] == "complete":
        lines.append("its closure record and credited XP")
    note = ("Currently gating: " + ", ".join(f"**{x}**" for x in dependents)
            + " — those will unlock immediately.") if dependents else ""

    async def do_remove(i: discord.Interaction):
        db.delete_milestone(m["id"])
        await i.followup.send(f"🗑️ Removed **{m['name']}**.")

    await interaction.response.send_message(
        embed=wizard.danger_embed(f"Remove {m['name']}?", lines, note),
        view=wizard.DangerConfirm(interaction.user.id, do_remove, label="Remove"),
    )


@test_group.command(name="smoke", description="Run a temporary end-to-end tracker check")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(visible="Post each test step and the tree image in this channel")
async def test_smoke(interaction: discord.Interaction, visible: bool = True):
    """Exercise the core tracker flow without leaving test data behind."""
    if not may_run(interaction, "test_smoke"):
        await deny(interaction, "test_smoke")
        return

    await interaction.response.defer()
    guild_id = interaction.guild_id
    suffix = secrets.token_hex(3)
    project_id = tree_id = gate_id = next_id = None
    checks: list[str] = []
    try:
        project_id = db.create_project(guild_id, f"Smoke Test {suffix}", "temporary", interaction.user.id)
        task_id = db.add_task(project_id, "Temporary test task", interaction.user.id)
        tree_id = db.create_tree(guild_id, f"smoke-{suffix}", "Temporary Smoke Test")
        gate_id = db.create_milestone(guild_id, f"smoke-gate-{suffix}", "Smoke gate", xp=100)
        next_id = db.create_milestone(guild_id, f"smoke-next-{suffix}", "Smoke unlock")
        db.add_to_tree(tree_id, gate_id)
        db.add_to_tree(tree_id, next_id)
        if not db.add_dep(next_id, gate_id):
            raise AssertionError("dependency link was rejected")
        db.link_project(gate_id, project_id)

        if db.progress(project_id)["pct"] != 0:
            raise AssertionError("new project did not start at 0%")
        checks.append("project/task creation")
        if visible:
            await interaction.followup.send(
                f"📁 Created **Smoke Test {suffix}**.\n"
                f"➕ Added **Temporary test task** → *Smoke Test {suffix}*"
            )
        before = {n["key"]: n for n in db.tree_view(guild_id, f"smoke-{suffix}")}
        if before[f"smoke-next-{suffix}"]["state"] != "locked":
            raise AssertionError("dependency did not lock the next milestone")
        checks.append("dependency locking")
        if visible:
            await interaction.followup.send(
                f"🌳 Created **Temporary Smoke Test**.\n"
                f"🔒 **Smoke unlock** is locked until **Smoke gate** is complete."
            )

        db.set_task_status(task_id, "done")
        after = {n["key"]: n for n in db.tree_view(guild_id, f"smoke-{suffix}")}
        if after[f"smoke-gate-{suffix}"]["state"] != "complete":
            raise AssertionError("completed task did not complete the milestone")
        if after[f"smoke-next-{suffix}"]["state"] != "available":
            raise AssertionError("completed milestone did not unlock the next one")
        checks.append("progress and unlock")
        if visible:
            await interaction.followup.send(
                "✅ **Temporary test task** done — **Smoke gate** now 100%.\n"
                "🔓 **Smoke unlock** is now available."
            )

        awards = db.settle_milestone(guild_id, gate_id, 100)
        if awards.get(interaction.user.id) != 100:
            raise AssertionError("XP was not credited to the test user")
        checks.append("XP credit")
        if visible:
            await interaction.followup.send(
                f"🎉 **Smoke gate** complete — **{awards[interaction.user.id]} XP earned**."
            )

        nodes = db.tree_view(guild_id, f"smoke-{suffix}")
        edges = db.tree_edges(guild_id, nodes)
        image = await asyncio.to_thread(
            tree_render.render_tree, nodes, edges, "Temporary Smoke Test", "lr"
        )
        if image.getbuffer().nbytes < 100:
            raise AssertionError("tree image was empty")
        checks.append("PNG rendering")
        if visible:
            await interaction.followup.send(
                "🖼️ **Tree image generated:**", file=discord.File(image, filename="smoke-tree.png")
            )
    except Exception as error:
        checks.append(f"FAILED: {error}")
    finally:
        if tree_id is not None:
            db.delete_tree(tree_id)
        for milestone_id in (next_id, gate_id):
            if milestone_id is not None:
                db.delete_milestone(milestone_id)
        if project_id is not None:
            db.delete_project(project_id)

    result = "\n".join(f"✅ {check}" for check in checks)
    await interaction.followup.send(
        f"🧪 **Tracker smoke test**\n{result}\n\n"
        f"Temporary test data was removed; the messages remain in this channel."
    )


@test_group.command(name="suite", description="Run broader tracker behavior checks")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(visual="Post every stage and both tree images in this channel")
async def test_suite(interaction: discord.Interaction, visual: bool = False):
    if not may_run(interaction, "test_suite"):
        await deny(interaction, "test_suite")
        return

    await interaction.response.defer()
    guild_id = interaction.guild_id
    suffix = secrets.token_hex(3)
    project_id = tree_id = gate_id = next_id = None
    checks: list[str] = []

    async def show(message: str) -> None:
        if visual:
            await interaction.followup.send(message)

    try:
        project_id = db.create_project(
            guild_id, f"Suite Test {suffix}", "temporary", interaction.user.id
        )
        heavy = db.add_task(project_id, "Heavy task", interaction.user.id, weight=3)
        light = db.add_task(project_id, "Light task", interaction.user.id, weight=1)
        if db.progress(project_id)["pct"] != 0:
            raise AssertionError("weighted project did not start at 0%")
        db.set_task_status(heavy, "doing")
        if db.progress(project_id)["doing"] != 1:
            raise AssertionError("doing status was not recorded")
        db.set_task_status(heavy, "done")
        if db.progress(project_id)["pct"] != 75:
            raise AssertionError("weighted progress was not 75%")
        checks.append("weighted progress and task statuses")
        await show("📊 Weighted progress: **75%** after the heavy task; status changes work.")

        db.add_log(project_id, interaction.user.id, "Suite test note")
        if not db.recent_log(project_id) or db.recent_log(project_id)[0]["body"] != "Suite test note":
            raise AssertionError("project note was not recorded")
        checks.append("project notes and history")
        await show("📝 Project note recorded and visible in history.")

        tree_key = f"suite-{suffix}"
        tree_id = db.create_tree(guild_id, tree_key, "Temporary Suite Test")
        gate_key = f"suite-gate-{suffix}"
        next_key = f"suite-next-{suffix}"
        gate_id = db.create_milestone(guild_id, gate_key, "Suite gate", xp=100)
        next_id = db.create_milestone(guild_id, next_key, "Suite unlock")
        db.add_to_tree(tree_id, gate_id)
        db.add_to_tree(tree_id, next_id)
        if not db.add_dep(next_id, gate_id):
            raise AssertionError("dependency link was rejected")
        db.link_project(gate_id, project_id)
        before = {n["key"]: n for n in db.tree_view(guild_id, tree_key)}
        if before[next_key]["state"] != "locked":
            raise AssertionError("dependent milestone was not locked")
        checks.append("dependency locking")
        await show("🔒 **Suite unlock** is locked behind **Suite gate**.")

        db.set_task_status(light, "done")
        after = {n["key"]: n for n in db.tree_view(guild_id, tree_key)}
        if after[gate_key]["state"] != "complete" or after[next_key]["state"] != "available":
            raise AssertionError("completion did not unlock the dependent milestone")
        awards = db.settle_milestone(guild_id, gate_id, 100)
        if awards.get(interaction.user.id) != 100:
            raise AssertionError("XP was not awarded")
        checks.append("unlock and XP settlement")
        await show("✅ **Suite gate** reached 100%; **Suite unlock** is available; 100 XP awarded.")

        nodes = db.tree_view(guild_id, tree_key)
        edges = db.tree_edges(guild_id, nodes)
        left_to_right = await asyncio.to_thread(
            tree_render.render_tree, nodes, edges, "Temporary Suite Test", "lr"
        )
        top_to_bottom = await asyncio.to_thread(
            tree_render.render_tree, nodes, edges, "Temporary Suite Test", "tb"
        )
        if left_to_right.getbuffer().nbytes < 100 or top_to_bottom.getbuffer().nbytes < 100:
            raise AssertionError("one of the tree images was empty")
        checks.append("left-to-right and top-to-bottom rendering")
        if visual:
            await interaction.followup.send(
                "🖼️ **Left to right tree:**", file=discord.File(left_to_right, filename="suite-lr.png")
            )
            await interaction.followup.send(
                "🖼️ **Top to bottom tree:**", file=discord.File(top_to_bottom, filename="suite-tb.png")
            )
    except Exception as error:
        checks.append(f"FAILED: {error}")
    finally:
        if tree_id is not None:
            db.delete_tree(tree_id)
        for milestone_id in (next_id, gate_id):
            if milestone_id is not None:
                db.delete_milestone(milestone_id)
        if project_id is not None:
            db.delete_project(project_id)

    result = "\n".join(f"✅ {check}" if not check.startswith("FAILED:") else f"❌ {check}" for check in checks)
    await interaction.followup.send(
        f"🧪 **Tracker comprehensive test suite**\n{result}\n\n"
        "Temporary test data was removed; any visual messages remain in this channel."
    )


@test_group.command(name="config", description="Test config export, preview, apply, and restore")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(visual="Post every config stage in this channel")
async def test_config(interaction: discord.Interaction, visual: bool = False):
    if not may_run(interaction, "test_config"):
        await deny(interaction, "test_config")
        return

    await interaction.response.defer()
    guild_id = interaction.guild_id
    suffix = secrets.token_hex(3)
    original = db.export_config(guild_id)
    valid_roles = {role.id for role in interaction.guild.roles}
    checks: list[str] = []

    async def show(message: str) -> None:
        if visual:
            await interaction.followup.send(message)

    try:
        if original.get("version") != 1:
            raise AssertionError("export version is missing or unexpected")
        checks.append("configuration export")
        await show("📤 Exported the current server configuration.")

        temporary = copy.deepcopy(original)
        temporary["taxonomy"]["grp"].append(f"Smoke Group {suffix}")
        temporary["taxonomy"]["region"].append(f"Smoke Region {suffix}")
        temporary["levels"].append({"xp": 9000, "name": f"Smoke Level {suffix}", "perk": "test"})
        preview = db.diff_config(guild_id, temporary, valid_roles)
        if not preview["tax_add"] or not preview["level_set"]:
            raise AssertionError("import preview did not detect the proposed changes")
        checks.append("import preview")
        await show("🔍 Import preview detected the temporary taxonomy and level changes.")

        db.apply_config(guild_id, temporary, valid_roles)
        applied = db.export_config(guild_id)
        if applied["taxonomy"] != temporary["taxonomy"] or applied["levels"] != temporary["levels"]:
            raise AssertionError("temporary configuration was not applied")
        checks.append("configuration apply")
        await show("✅ Applied the temporary configuration successfully.")

        edited = copy.deepcopy(temporary)
        edited["taxonomy"]["grp"].remove(f"Smoke Group {suffix}")
        edited["levels"][-1]["name"] = f"Edited Smoke Level {suffix}"
        changed = db.diff_config(guild_id, edited, valid_roles)
        if not changed["tax_remove"] or not changed["level_set"]:
            raise AssertionError("preview did not detect the edit/removal")
        checks.append("replacement diff")
        await show("✏️ A second preview detected an edit and a removal.")

        db.apply_config(guild_id, original, valid_roles)
        if db.export_config(guild_id) != original:
            raise AssertionError("original configuration was not restored")
        checks.append("safe restore")
        await show("↩️ Restored the original server configuration.")
    except Exception as error:
        checks.append(f"FAILED: {error}")
        db.apply_config(guild_id, original, valid_roles)

    result = "\n".join(
        f"✅ {check}" if not check.startswith("FAILED:") else f"❌ {check}"
        for check in checks
    )
    await interaction.followup.send(
        f"🧪 **Config export/import test**\n{result}\n\n"
        "The original server configuration was restored."
    )


@bot.tree.command(name="start",
                  description="Guided setup — project, then a tree, then milestones")
@app_commands.guild_only()
async def start(interaction: discord.Interaction):
    if not may_run(interaction, "start"):
        await deny(interaction, "start")
        return
    await interaction.response.send_message(
        "Let's set up a project. This walks you through it — press **Start**.",
        view=wizard.ProjectStart(interaction.user.id), ephemeral=True)


@bot.tree.command(name="help", description="How the pieces fit together")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(
        title="How this works",
        description=(
            "There are only two ideas.\n\n"
            "**Milestones** are the boxes on the tree. They have prerequisites "
            "(what must finish first) and a payoff (what they unlock).\n\n"
            "**Tasks** are the small steps inside one milestone. They have no "
            "prerequisites of their own — they just tick a milestone toward 100%.\n\n"
            "When every task under a milestone is done, the milestone unlocks "
            "whatever was waiting on it."
        ),
        colour=discord.Color.blurple(),
    )
    e.add_field(
        name="Easiest way in",
        value="**`/start`** — a guided walk-through. It sets up a project, then a "
              "tree, then its milestones with fill-in-the-blank forms, wiring the "
              "prerequisites together as you go.",
        inline=False,
    )
    e.add_field(
        name="Day to day",
        value="`/next` — what to work on\n"
              "`/tree show` — the picture\n"
              "`/task done` — tick something off\n"
              "`/me` — your open steps",
        inline=False,
    )
    e.add_field(
        name="Adding more later",
        value="`/tree add` — another milestone\n"
              "`/task add` — another step\n"
              "`/tree requires` — connect two milestones\n"
              "`/tree import` — bulk-load a whole tree from a spreadsheet",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="next", description="What's closest to unlocking, and what's in the way")
@app_commands.guild_only()
@app_commands.describe(tree="Limit to one tree")
@app_commands.autocomplete(tree=tree_autocomplete)
async def next_up(interaction: discord.Interaction, tree: str | None = None):
    nodes = await asyncio.to_thread(db.tree_view, interaction.guild_id, tree)
    if not nodes:
        await interaction.followup.send(
            "No tech tree yet. Build one with `/start`.", ephemeral=True
        )
        return
    by_key = {n["key"]: n for n in nodes}
    e = discord.Embed(title="What's next", colour=discord.Color.gold())

    stubs = [n for n in nodes if n.get("is_stub") and not n.get("external_from")]
    if stubs:
        e.add_field(
            name="🌱 Undefined placeholders",
            value="\n".join(
                f"**{n['name']}** — `/tree edit key:{n['key']}`" for n in stubs[:5]
            ) + "\nSomething depends on these, but nobody has said what they are.",
            inline=False,
        )

    waiting = [n for n in nodes
               if n["state"] == "pending" and not n.get("external_from")]
    if waiting:
        e.add_field(
            name="🟣 Waiting on a sign-off",
            value="\n".join(
                f"**{n['name']}** — work is done, run `/tree confirm key:{n['key']}`"
                for n in waiting[:5]
            ),
            inline=False,
        )

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
    await interaction.followup.send(embed=e)


@bot.tree.command(name="levels", description="The XP ladder, and where you sit on it")
@app_commands.guild_only()
async def levels(interaction: discord.Interaction):
    ladder = db.list_levels(interaction.guild_id)
    xp = db.user_xp(interaction.guild_id, interaction.user.id)
    lines = []
    for i, r in enumerate(ladder, start=1):
        here = "**← you**" if db.level_for(interaction.guild_id, xp)["threshold"] == r["threshold"] else ""
        perk = f" — *{r['perk']}*" if r["perk"] else ""
        lines.append(f"`{i}.` **{r['name']}** · {r['threshold']} XP{perk} {here}")
    e = discord.Embed(title="Levels", description="\n".join(lines),
                      colour=discord.Color.gold())
    e.add_field(name="You", value=standing_line(interaction.guild_id,
                                                interaction.user.id), inline=False)
    e.set_footer(text="Levels are cosmetic for now. /config level edits the ladder.")
    await interaction.response.send_message(embed=e, ephemeral=True)


_KIND_CHOICES = [
    app_commands.Choice(name="group", value="grp"),
    app_commands.Choice(name="region", value="region"),
    app_commands.Choice(name="team", value="team"),
]


@config_group.command(name="tag-add", description="Add a group, region, or team value")
@app_commands.choices(kind=_KIND_CHOICES)
@app_commands.describe(value="The name to add")
@app_commands.default_permissions(manage_guild=True)
async def config_tag_add(interaction: discord.Interaction,
                         kind: app_commands.Choice[str], value: str):
    db.add_taxonomy(interaction.guild_id, kind.value, value)
    await interaction.response.send_message(
        f"Added **{value}** to {kind.name}s.")


@config_group.command(name="tag-remove", description="Remove a group, region, or team value")
@app_commands.choices(kind=_KIND_CHOICES)
@app_commands.default_permissions(manage_guild=True)
async def config_tag_remove(interaction: discord.Interaction,
                            kind: app_commands.Choice[str], value: str):
    db.remove_taxonomy(interaction.guild_id, kind.value, value)
    await interaction.response.send_message(
        f"Removed **{value}** from {kind.name}s. Existing items keep the label.")


@config_group.command(name="tags", description="List configured groups, regions and teams")
async def config_tags(interaction: discord.Interaction):
    e = discord.Embed(title="Taxonomy", colour=discord.Color.blurple())
    for kind, label in db.TAXONOMY_LABEL.items():
        vals = db.list_taxonomy(interaction.guild_id, kind)
        e.add_field(name=label.title() + "s", value=", ".join(vals), inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@config_group.command(name="permission", description="Restrict a command to a role")
@app_commands.describe(command="Command name, e.g. tree_import",
                       role="Leave blank to reopen the command to everyone")
@app_commands.default_permissions(manage_guild=True)
async def config_permission(interaction: discord.Interaction, command: str,
                            role: discord.Role | None = None):
    command = command.strip().lstrip("/").replace(" ", "_")
    db.set_cmd_perm(interaction.guild_id, command, role.id if role else None)
    if role:
        await interaction.response.send_message(
            f"`/{command.replace('_',' ')}` now needs {role.mention} (or Manage Server).")
    else:
        await interaction.response.send_message(
            f"`/{command.replace('_',' ')}` is open to everyone again.")


@config_group.command(name="permissions", description="Show which commands are role-gated")
async def config_permissions(interaction: discord.Interaction):
    rows = db.list_cmd_perms(interaction.guild_id)
    uni = db.get_universal_role(interaction.guild_id)
    lines = [f"`/{r['command'].replace('_',' ')}` → <@&{r['role_id']}>" for r in rows]
    if uni:
        lines.append(f"setting anything **Universal** → <@&{uni}>")
    e = discord.Embed(
        title="Gated commands",
        description="\n".join(lines) or "Nothing is gated — everything is open.",
        colour=discord.Color.blurple(),
    )
    e.set_footer(text="Manage Server always passes. This is separate from Discord's "
                      "own command-permission settings.")
    await interaction.response.send_message(embed=e, ephemeral=True)


@config_group.command(name="universal-role",
                      description="Which role may set things Universal")
@app_commands.describe(role="Leave blank to limit it to Manage Server")
@app_commands.default_permissions(manage_guild=True)
async def config_universal_role(interaction: discord.Interaction,
                                role: discord.Role | None = None):
    db.set_universal_role(interaction.guild_id, role.id if role else None)
    who = role.mention if role else "people with Manage Server"
    await interaction.response.send_message(f"Setting items **Universal** now needs {who}.")


@config_group.command(name="export", description="Download the current config for the panel")
@app_commands.default_permissions(manage_guild=True)
async def config_export(interaction: discord.Interaction):
    if not may_run(interaction, "config_export"):
        await deny(interaction, "config_export")
        return
    import io, json
    doc = await asyncio.to_thread(db.export_config, interaction.guild_id)
    # include the server's roles so the offline panel can show real names
    doc["_roles"] = {str(r.id): r.name for r in interaction.guild.roles if not r.is_default()}
    # include the live command list so the panel's dropdown never goes stale
    doc["_commands"] = sorted(
        c.qualified_name.replace(" ", "_")
        for c in interaction.client.tree.walk_commands()
        if not isinstance(c, app_commands.Group)
    )
    buf = io.BytesIO(json.dumps(doc, indent=2).encode())
    await interaction.response.send_message(
        "Here's the current config. Open it in `config_panel.html`, edit, and send "
        "it back with `/config import`.",
        file=discord.File(buf, filename="config.json"), ephemeral=True)


@config_group.command(name="import", description="Apply a config file from the panel")
@app_commands.describe(file="A config.json from the panel")
@app_commands.default_permissions(manage_guild=True)
async def config_import(interaction: discord.Interaction, file: discord.Attachment):
    if not may_run(interaction, "config_import"):
        await deny(interaction, "config_import")
        return
    if not file.filename.lower().endswith(".json"):
        await interaction.response.send_message(
            "That needs to be the `.json` file from `config_panel.html`.", ephemeral=True)
        return
    if file.size > 500_000:
        await interaction.response.send_message("That file is too big.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    import json
    try:
        doc = json.loads((await file.read()).decode("utf-8"))
    except Exception as err:
        await interaction.followup.send(
            f"Couldn't read that file: `{type(err).__name__}`.", ephemeral=True)
        return
    valid = {r.id for r in interaction.guild.roles}
    rep = await asyncio.to_thread(db.diff_config, interaction.guild_id, doc, valid)
    role_name = {str(r.id): r.name for r in interaction.guild.roles}
    embed = wizard.config_diff_embed(rep, role_name)
    await interaction.followup.send(
        embed=embed,
        view=wizard.ConfigImportConfirm(doc, valid, interaction.user.id),
        ephemeral=True)


@config_group.command(name="level", description="Add or edit a rung on the XP ladder")
@app_commands.describe(threshold="XP required", name="What it's called",
                       perk="What it should grant — descriptive for now")
@app_commands.default_permissions(manage_guild=True)
async def config_level(interaction: discord.Interaction, threshold: int,
                       name: str, perk: str = ""):
    db.set_level(interaction.guild_id, max(0, threshold), name, perk)
    await interaction.response.send_message(
        f"⭐ **{name}** now sits at {threshold} XP." +
        (f" Grants: {perk}" if perk else "")
    )


@config_group.command(name="unlevel", description="Remove a rung from the ladder")
@app_commands.default_permissions(manage_guild=True)
async def config_unlevel(interaction: discord.Interaction, threshold: int):
    db.remove_level(interaction.guild_id, threshold)
    await interaction.response.send_message(f"Removed the rung at {threshold} XP.")


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
    lines = []
    for i, r in enumerate(rows):
        lv = db.level_for(interaction.guild_id, r["xp"])
        lines.append(
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — "
            f"**{r['xp']} XP** · {lv['name']} · {r['unlocks']} milestone(s)"
        )
    e = discord.Embed(
        title="Leaderboard",
        description="\n".join(lines),
        colour=discord.Color.gold(),
    )
    e.set_footer(text="XP is split across everyone whose tasks fed the milestone.")
    await interaction.response.send_message(embed=e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = "".join(c for c in (TOKEN or input("Discord bot token: "))
                     if 32 <= ord(c) <= 126)
    if not token:
        raise SystemExit("A Discord bot token is required.")
    bot.run(token)
