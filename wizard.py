"""Guided setup: pop-up forms instead of remembering command parameters.

Discord modals allow five inputs, which is exactly enough to describe a milestone
in one screen — name, payoff, prerequisites, its steps, and its XP. The flow is:

    /start  ->  name the tree  ->  [Add milestone] x4  ->  Done

Prerequisites you name that don't exist yet become **stubs** — placeholder nodes
you can fill in on a later pass. That lets you sketch a tree top-down instead of
having to enter it in dependency order.
"""

from __future__ import annotations

import re

import discord

import db

MAX_WIZARD_NODES = 4


def slugify(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:20] or "node"
    key, n = base, 2
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    return key


def parse_steps(raw: str) -> list[tuple[str, int]]:
    """One step per line. A trailing `x3` sets its weight."""
    steps = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        if not line:
            continue
        weight = 1
        if m := re.search(r"\s+x(\d{1,2})$", line):
            weight = max(1, min(20, int(m.group(1))))
            line = line[: m.start()].strip()
        if line:
            steps.append((line[:180], weight))
    return steps


def resolve_requires(guild_id: int, raw: str, stub: bool = True) -> tuple[list[int], list[str]]:
    """Match comma-separated text against milestone keys or names.

    With `stub` on, anything unmatched becomes a placeholder milestone instead of
    an error. Returns (ids, names_that_were_stubbed).
    """
    found, created = [], []
    for term in filter(None, (t.strip() for t in (raw or "").split(","))):
        if stub:
            mid, was_new = db.find_or_stub(guild_id, term)
            found.append(mid)
            if was_new:
                created.append(term)
        else:
            existing = db.list_milestones(guild_id)
            low = term.lower()
            hit = next((m for m in existing if m["key"] == low), None) \
                or next((m for m in existing if m["name"].lower() == low), None) \
                or next((m for m in existing if low in m["name"].lower()), None)
            (found.append(hit["id"]) if hit else created.append(term))
    return found, created


class MilestoneModal(discord.ui.Modal):
    """One screen that fully describes a node, steps included."""

    def __init__(self, tree_key: str, on_done):
        super().__init__(title="Add a milestone")
        self.tree_key = tree_key
        self.on_done = on_done

        self.m_name = discord.ui.TextInput(
            label="Milestone name",
            placeholder="Venue booked",
            max_length=80,
        )
        self.m_unlocks = discord.ui.TextInput(
            label="What does finishing it make possible?",
            placeholder="the date becomes announceable",
            required=False,
            max_length=120,
        )
        self.m_desc = discord.ui.TextInput(
            label="What is it? (steps, detail, whatever helps)",
            style=discord.TextStyle.paragraph,
            placeholder="Call three venues, compare quotes, sign, pay the deposit",
            required=False,
            max_length=400,
        )
        self.m_requires = discord.ui.TextInput(
            label="Must come after… (comma separated, optional)",
            placeholder="Funding secured, Scope locked",
            required=False,
            max_length=200,
        )
        self.m_xp = discord.ui.TextInput(
            label="XP when it unlocks", default="100", required=False, max_length=5
        )
        for item in (self.m_name, self.m_desc, self.m_unlocks,
                     self.m_requires, self.m_xp):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        name = str(self.m_name).strip()
        taken = {m["key"] for m in db.list_milestones(gid)}
        key = slugify(name, taken)

        try:
            xp = max(0, min(5000, int(str(self.m_xp).strip() or 100)))
        except ValueError:
            xp = 100

        existing = db.get_milestone(gid, key)
        if existing and existing["is_stub"]:
            # someone already named this as a dependency — fill it in rather than
            # creating a duplicate
            mid = existing["id"]
            db.update_milestone(mid, name=name, description=str(self.m_desc).strip(),
                                unlocks=str(self.m_unlocks).strip(), xp=xp)
            db.clear_stub(mid)
            verb = "defined"
        else:
            mid = db.create_milestone(gid, key, name, str(self.m_unlocks).strip(),
                                      xp, str(self.m_desc).strip())
            verb = "added"

        tree = db.get_tree(gid, self.tree_key)
        if tree:
            db.add_to_tree(tree["id"], mid)

        # unknown prerequisites become stubs rather than errors, so a tree can be
        # built top-down: name the gate now, describe it later
        stubbed, looped = [], []
        for term in filter(None, (t.strip() for t in str(self.m_requires).split(","))):
            rid, created = db.find_or_stub(gid, term)
            if rid == mid:
                continue
            if not db.add_dep(mid, rid):
                looped.append(term)
                continue
            if created:
                if tree:
                    db.add_to_tree(tree["id"], rid)
                stubbed.append(term)

        line = f"**{name}** {verb}."
        if stubbed:
            line += (f"\n🌱 Created placeholder(s) for {', '.join(stubbed)} — "
                     f"press **Add milestone** again to fill them in.")
        if looped:
            line += (f"\n⚠️ Skipped {', '.join(looped)} — those already wait on this "
                     f"milestone, so the dependency would be circular.")
        line += ("\n💡 Close it with `/tree confirm` when it's done, or attach "
                 "trackable work with `/task add`.")
        await self.on_done(interaction, line)


class BuilderView(discord.ui.View):
    """Buttons that live under the setup message."""

    def __init__(self, tree_key: str, tree_name: str, author_id: int):
        super().__init__(timeout=900)
        self.tree_key = tree_key
        self.tree_name = tree_name
        self.author_id = author_id
        self.count = 0
        self.log: list[str] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Whoever started this setup needs to finish it — "
                "run `/start` for your own.", ephemeral=True
            )
            return False
        return True

    def embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"Building: {self.tree_name}",
            description=(
                "\n".join(f"✅ {line.splitlines()[0]}" for line in self.log)
                or "Press **Add milestone** to describe the first one."
            ),
            colour=discord.Color.blurple(),
        )
        e.set_footer(
            text=f"{self.count}/{MAX_WIZARD_NODES} milestones · "
                 f"more can be added later with /tree add"
        )
        return e

    async def record(self, interaction: discord.Interaction, line: str):
        self.count += 1
        self.log.append(line)
        if self.count >= MAX_WIZARD_NODES:
            self.add_btn.disabled = True
            self.add_btn.label = "Limit reached — use /tree add"
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Add milestone", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            MilestoneModal(self.tree_key, self.record)
        )

    @discord.ui.button(label="Done — show the tree", style=discord.ButtonStyle.success, emoji="🌲")
    async def finish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Setup complete. Run `/tree show tree:{self.tree_key}` to see it, "
            f"or `/next` for what to work on first."
        )
        self.stop()


class TreeModal(discord.ui.Modal, title="Start a new tree"):
    t_name = discord.ui.TextInput(
        label="Tree name", placeholder="Candidate forum", max_length=80
    )
    t_desc = discord.ui.TextInput(
        label="What is it for?",
        placeholder="Everything that has to happen before the August event",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        name = str(self.t_name).strip()
        taken = {t["key"] for t in db.list_trees(gid)}
        key = slugify(name, taken)
        db.create_tree(gid, key, name, str(self.t_desc).strip())

        view = BuilderView(key, name, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view)


class ImportConfirm(discord.ui.View):
    """Nothing is written until someone reads the summary and presses Apply."""

    def __init__(self, doc: dict, author_id: int, filename: str):
        super().__init__(timeout=300)
        self.doc = doc
        self.author_id = author_id
        self.filename = filename

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only whoever uploaded the file can apply it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, emoji="✅")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        import seed
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        log = seed.apply_doc(self.doc, interaction.guild_id, interaction.user.id)
        gates = sum(1 for line in log if line.strip().startswith("gate"))
        nodes = sum(1 for line in log if line.strip().startswith("node"))
        await interaction.followup.send(
            f"📥 Loaded **{self.filename}** — {nodes} milestone(s), {gates} dependency link(s).\n"
            f"`/tree show` to see it, `/next` for where to start."
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Import cancelled — nothing was changed.", embed=None, view=self
        )
        self.stop()


def preview_embed(pv: dict, filename: str) -> discord.Embed:
    e = discord.Embed(
        title=f"Preview: {filename}",
        description="Nothing has been saved yet. Check this over, then press **Apply**.",
        colour=discord.Color.blurple(),
    )
    if pv["problems"]:
        e.colour = discord.Color.red()
        e.add_field(name="⚠️ Problems", value="\n".join(pv["problems"][:5]), inline=False)
    for label, items, note in (
        ("🌳 New trees", pv["new_trees"], ""),
        ("🌳 Existing trees (will be updated)", pv["known_trees"], ""),
        ("➕ New milestones", pv["created"], ""),
        ("✏️ Existing milestones (will be overwritten)", pv["updated"],
         "Descriptions, payoffs and XP get replaced. Progress and closures are kept."),
        ("🌱 Stubs it will create", pv["stubs"],
         "Named as prerequisites but not defined in the file."),
    ):
        if not items:
            continue
        body = ", ".join(items[:12]) + (f" +{len(items) - 12} more" if len(items) > 12 else "")
        e.add_field(name=f"{label} ({len(items)})",
                    value=body + (f"\n*{note}*" if note else ""), inline=False)
    if not any((pv["new_trees"], pv["known_trees"], pv["created"], pv["updated"])):
        e.description = "Nothing usable found in that file. Check the header row."
        e.colour = discord.Color.red()
    return e


class DangerConfirm(discord.ui.View):
    """Two-step gate for anything that destroys data."""

    def __init__(self, author_id: int, on_confirm, label: str = "Delete"):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.on_confirm = on_confirm
        self.go.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only whoever ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled — nothing was removed.", embed=None, view=self
        )
        self.stop()


def danger_embed(title: str, what_goes: list[str], note: str = "") -> discord.Embed:
    e = discord.Embed(
        title=title,
        description="**This cannot be undone.**" + (f"\n{note}" if note else ""),
        colour=discord.Color.red(),
    )
    e.add_field(name="Will be destroyed", value="\n".join(f"• {x}" for x in what_goes),
                inline=False)
    return e


# ==========================================================================
# STAGE 2: project-first guided setup (Project -> Tree -> Milestone)
# ==========================================================================
# Discord modals cannot hold a dropdown, so milestone-linking is done with a
# select-menu on a follow-up message. Dropdowns are filtered by group/region/
# team: you only see and pick milestones from your own scope, which stops
# cross-group edits by accident. Cross-group links remain possible via the typed
# /tree requires command, which announces them.

DIFFICULTY_OPTIONS = [
    discord.SelectOption(label=f"{d:g}", value=str(d))
    for d in [1, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 9, 10]
]


def scope_of(row) -> tuple[str, str, str]:
    return row["grp"], row["region"], row["team"]


class StartFlow:
    """Carries selections across the Project -> Tree -> Milestone steps."""

    def __init__(self, author_id: int):
        self.author_id = author_id
        self.project_id = None
        self.tree_id = None
        self.tree_key = None
        self.scope = ("Universal", "Universal", "Universal")
        self.count = 0
        self.log: list[str] = []


class ProjectModal(discord.ui.Modal, title="New project"):
    p_name = discord.ui.TextInput(label="Project name", placeholder="Southern Tier Skyway",
                                  max_length=80)
    p_desc = discord.ui.TextInput(label="What is it?", required=False, max_length=200,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, flow: StartFlow):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.p_name).strip()
        if db.get_project(interaction.guild_id, name):
            self.flow.project_id = db.get_project(interaction.guild_id, name)["id"]
        else:
            self.flow.project_id = db.create_project(
                interaction.guild_id, name, str(self.p_desc).strip(), interaction.user.id)
        # scope is chosen next, on a select-menu message
        await interaction.response.send_message(
            f"📁 **{name}** ready. Now pick its group, region and team — "
            f"or skip for Universal.",
            view=ScopePicker(self.flow, name), ephemeral=True)


class ScopePicker(discord.ui.View):
    """Three select-menus for group / region / team, then a continue button."""

    def __init__(self, flow: StartFlow, project_name: str):
        super().__init__(timeout=600)
        self.flow = flow
        self.project_name = project_name
        self.chosen = {"grp": "Universal", "region": "Universal", "team": "Universal"}
        for kind in db.TAXONOMIES:
            self.add_item(self._menu(kind))

    def _menu(self, kind: str):
        gid = self.flow.author_id  # placeholder; real guild filled at runtime via interaction
        label = db.TAXONOMY_LABEL[kind]
        menu = discord.ui.Select(
            placeholder=f"{label.title()} (Universal)",
            options=[discord.SelectOption(label="Universal", value="Universal")],
            min_values=1, max_values=1, custom_id=kind,
        )
        menu.callback = self._make_cb(kind, menu)
        return menu

    async def _populate(self, interaction: discord.Interaction):
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.custom_id in db.TAXONOMIES:
                vals = db.list_taxonomy(interaction.guild_id, item.custom_id)
                item.options = [discord.SelectOption(label=v, value=v,
                                default=(v == self.chosen[item.custom_id]))
                                for v in vals][:25]

    def _make_cb(self, kind: str, menu: discord.ui.Select):
        async def cb(interaction: discord.Interaction):
            val = menu.values[0]
            if val == "Universal" and self.chosen[kind] != "Universal":
                pass
            if val != "Universal" and menu.values[0] == "Universal":
                pass
            # setting Universal explicitly is unrestricted here (it's the default);
            # the privileged path is *changing away from* a real group to Universal,
            # which the edit commands guard. Creation defaults are fine.
            self.chosen[kind] = val
            await interaction.response.defer()
        return cb

    @discord.ui.button(label="Continue → add a tree", style=discord.ButtonStyle.primary, row=3)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.flow.scope = (self.chosen["grp"], self.chosen["region"], self.chosen["team"])
        db.set_project_tags(self.flow.project_id, grp=self.chosen["grp"],
                            region=self.chosen["region"], team=self.chosen["team"])
        await interaction.response.edit_message(
            content=f"📁 **{self.project_name}** — "
                    + " · ".join(v for v in self.flow.scope if v != "Universal") or
                    f"📁 **{self.project_name}** — Universal",
            view=None)
        await interaction.followup.send(
            "Now the tree. Name it:", view=NewTreeButton(self.flow), ephemeral=True)


class NewTreeButton(discord.ui.View):
    def __init__(self, flow: StartFlow):
        super().__init__(timeout=600)
        self.flow = flow

    @discord.ui.button(label="Name the tree", style=discord.ButtonStyle.primary, emoji="🌳")
    async def name_tree(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TreeStepModal(self.flow))


class TreeStepModal(discord.ui.Modal, title="New tree"):
    t_name = discord.ui.TextInput(label="Tree name", placeholder="Candidate forum",
                                  max_length=80)
    t_desc = discord.ui.TextInput(label="What is it for?", required=False, max_length=200,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, flow: StartFlow):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.t_name).strip()
        key = slugify(name, {t["key"] for t in db.list_trees(interaction.guild_id)})
        tid = db.create_tree(interaction.guild_id, key, name, str(self.t_desc).strip())
        db.set_tree_tags(tid, grp=self.flow.scope[0], region=self.flow.scope[1],
                         team=self.flow.scope[2])
        if self.flow.project_id:
            db.link_tree_project(tid, self.flow.project_id)
        self.flow.tree_id = tid
        self.flow.tree_key = key
        view = MilestoneBuilder(self.flow, name)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class MilestoneBuilder(discord.ui.View):
    """Add milestones one at a time; each opens a modal then an optional
    prerequisite picker."""

    def __init__(self, flow: StartFlow, tree_name: str):
        super().__init__(timeout=1200)
        self.flow = flow
        self.tree_name = tree_name

    def embed(self) -> discord.Embed:
        scope = " · ".join(v for v in self.flow.scope if v != "Universal") or "Universal"
        e = discord.Embed(
            title=f"Building: {self.tree_name}",
            description="\n".join(f"✅ {l}" for l in self.flow.log)
                        or "Press **Add milestone** to describe the first one.",
            colour=discord.Color.blurple())
        e.set_footer(text=f"{scope} · {self.flow.count}/{MAX_WIZARD_NODES} milestones · "
                          f"more later with /tree add")
        return e

    @discord.ui.button(label="Add milestone", style=discord.ButtonStyle.primary, emoji="➕")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.flow.count >= MAX_WIZARD_NODES:
            await interaction.response.send_message(
                "That's the wizard's limit — add more with `/tree add`.", ephemeral=True)
            return
        await interaction.response.send_modal(ScopedMilestoneModal(self.flow, self))

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="🌲")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Setup complete. `/tree show tree:{self.flow.tree_key}` to see it, "
            f"`/next` for where to start.", ephemeral=True)
        self.stop()


class ScopedMilestoneModal(discord.ui.Modal):
    """Milestone text fields including difficulty. Prerequisites are picked
    afterward on a scope-filtered select-menu, since modals can't hold one."""

    def __init__(self, flow: StartFlow, builder: MilestoneBuilder):
        super().__init__(title="Add a milestone")
        self.flow = flow
        self.builder = builder
        self.m_name = discord.ui.TextInput(label="Milestone name",
                                           placeholder="Venue booked", max_length=80)
        self.m_desc = discord.ui.TextInput(label="What is it?", required=False,
                                           max_length=400, style=discord.TextStyle.paragraph)
        self.m_unlocks = discord.ui.TextInput(
            label="What does finishing it make possible?", required=False, max_length=120)
        self.m_diff = discord.ui.TextInput(label="Difficulty 1-10 (half steps ok)",
                                           default="1", required=False, max_length=4)
        self.m_xp = discord.ui.TextInput(label="XP when it unlocks", default="100",
                                         required=False, max_length=5)
        for i in (self.m_name, self.m_desc, self.m_unlocks, self.m_diff, self.m_xp):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        name = str(self.m_name).strip()
        key = slugify(name, {m["key"] for m in db.list_milestones(gid)})
        try:
            xp = max(0, min(5000, int(str(self.m_xp).strip() or 100)))
        except ValueError:
            xp = 100
        diff = db.clamp_difficulty(str(self.m_diff).strip() or 1)

        existing = db.get_milestone(gid, key)
        if existing and existing["is_stub"]:
            mid = existing["id"]
            db.update_milestone(mid, name=name, description=str(self.m_desc).strip(),
                                unlocks=str(self.m_unlocks).strip(), xp=xp)
            db.set_difficulty(mid, diff)
            db.clear_stub(mid)
        else:
            mid = db.create_milestone(
                gid, key, name, str(self.m_unlocks).strip(), xp, str(self.m_desc).strip(),
                True, diff, False, *self.flow.scope)
        db.add_to_tree(self.flow.tree_id, mid)
        self.flow.count += 1
        self.flow.log.append(f"{name} (difficulty {diff:g})")

        # offer a prerequisite picker scoped to this group
        candidates = db.milestones_in_scope(gid, *self.flow.scope, exclude_id=mid)
        if candidates:
            await interaction.response.send_message(
                f"**{name}** added. Does it depend on anything already here? "
                f"Pick any that must finish first, or skip.",
                view=PrereqPicker(self.flow, self.builder, mid, name, candidates),
                ephemeral=True)
        else:
            await interaction.response.edit_message(
                embed=self.builder.embed(), view=self.builder)


class PrereqPicker(discord.ui.View):
    def __init__(self, flow, builder, mid, name, candidates):
        super().__init__(timeout=300)
        self.flow, self.builder, self.mid, self.name = flow, builder, mid, name
        menu = discord.ui.Select(
            placeholder="Must come after… (optional)",
            min_values=0, max_values=min(len(candidates), 25),
            options=[discord.SelectOption(label=c["name"][:100], value=str(c["id"]))
                     for c in candidates[:25]])
        menu.callback = self._picked(menu)
        self.add_item(menu)

    def _picked(self, menu):
        async def cb(interaction: discord.Interaction):
            linked = []
            for cid in menu.values:
                if db.add_dep(self.mid, int(cid)):
                    linked.append(int(cid))
            note = (f" — after {len(linked)} milestone(s)" if linked else "")
            await interaction.response.edit_message(
                content=f"**{self.name}** added{note}.", view=None)
            await interaction.followup.send(embed=self.builder.embed(),
                                            view=self.builder, ephemeral=True)
            self.stop()
        return cb

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"**{self.name}** added.", view=None)
        await interaction.followup.send(embed=self.builder.embed(),
                                        view=self.builder, ephemeral=True)
        self.stop()


class ProjectStart(discord.ui.View):
    """Entry point: /start posts this, the button opens the project modal."""

    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, emoji="🚀")
    async def begin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProjectModal(StartFlow(self.author_id)))
