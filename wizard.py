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
        stubbed = []
        for term in filter(None, (t.strip() for t in str(self.m_requires).split(","))):
            rid, created = db.find_or_stub(gid, term)
            if rid == mid:
                continue
            db.add_dep(mid, rid)
            if created:
                if tree:
                    db.add_to_tree(tree["id"], rid)
                stubbed.append(term)

        line = f"**{name}** {verb}."
        if stubbed:
            line += (f"\n🌱 Created placeholder(s) for {', '.join(stubbed)} — "
                     f"press **Add milestone** again to fill them in.")
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
