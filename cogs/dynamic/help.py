"""Interactive embed-based help for literallybot.

Replaces the stock DefaultHelpCommand with an embed + category-select view
covering both prefix commands and slash commands, and registers a matching
ephemeral /help slash command.
"""
import discord
from discord import app_commands
from discord.ext import commands

from core.utils import is_admin

# Friendly category names keyed by cog qualified_name. Unmapped cogs fall
# back to their qualified_name as the category.
CATEGORY_MAP = {
    "Gpt": "AI Chat",
    "AiAdmin": "AI Chat",
    "Admin": "Admin",
    "Dev": "Dev",
    "ErrorLoggingAdmin": "Error Logging",
    "ChannelMigrator": "Channel Tools",
    "Danbooru": "Images",
    "Interrogative": "Fun",
    "Meme": "Fun",
    "RNG": "Fun",
    "AutoResponse": "Fun",
    "Media": "Media",
    "Reminders": "Utilities",
    "Tools": "Utilities",
    "Cleanup": "Utilities",
    "Signal": "Utilities",
    "SetRole": "Roles",
    "Help": "Utilities",
}

# Categories only shown to bot admins (per core.utils.is_admin).
ADMIN_CATEGORIES = {"Admin", "Dev", "Error Logging"}

# Preferred display order; anything unmapped sorts alphabetically after these.
CATEGORY_ORDER = [
    "AI Chat", "Fun", "Images", "Media", "Roles", "Utilities",
    "Channel Tools", "Admin", "Error Logging", "Dev",
]

MEDIA_NOTE = "Any file in the media library can be posted with !<name> — see !listmedia"

EMBED_COLOUR = discord.Colour.blurple()


def _category_sort_key(name):
    try:
        return (0, CATEGORY_ORDER.index(name))
    except ValueError:
        return (1, name)


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _slash_entries(cmd):
    """Yield (token, description) for a tree command, expanding Groups one level."""
    if isinstance(cmd, app_commands.Group):
        for child in cmd.commands:
            yield (f"/{cmd.name} {child.name}", child.description or "")
    else:
        yield (f"/{cmd.name}", cmd.description or "")


def _build_overview_embed(categories):
    embed = discord.Embed(
        title="Command Help",
        description=(
            "Pick a category from the menu below to see its commands.\n"
            "Use `!help <command>` for details on a specific command."
        ),
        colour=EMBED_COLOUR,
    )
    for name in sorted(categories, key=_category_sort_key):
        entries = categories[name]
        tokens = ", ".join(f"`{token}`" for token, _ in entries)
        if name == "Media":
            tokens = MEDIA_NOTE + "\n" + tokens
        embed.add_field(name=name, value=_truncate(tokens, 1024), inline=False)
    return embed


def _build_category_embed(name, entries):
    lines = [f"`{token}` — {desc}" if desc else f"`{token}`" for token, desc in entries]
    description = "\n".join(lines)
    if name == "Media":
        description = MEDIA_NOTE + "\n\n" + description
    return discord.Embed(
        title=name,
        description=_truncate(description, 4096),
        colour=EMBED_COLOUR,
    )


class HelpView(discord.ui.View):
    """Category picker for the help overview. Invoker-only, 3-minute expiry."""

    def __init__(self, invoker_id, overview_embed, category_embeds):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.message = None
        self._overview = overview_embed
        self._categories = category_embeds
        options = [discord.SelectOption(label="Overview", value="__overview__")]
        options += [
            discord.SelectOption(label=name, value=name)
            for name in sorted(category_embeds, key=_category_sort_key)
        ]
        select = discord.ui.Select(placeholder="Pick a category…", options=options[:25])
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        embed = self._overview if value == "__overview__" else self._categories[value]
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This help menu isn't yours — run `/help` to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LiterallyHelpCommand(commands.HelpCommand):
    """Embed-based help with a category select covering prefix + slash commands."""

    def __init__(self, **kwargs):
        kwargs.setdefault("command_attrs", {
            "help": "Show an interactive overview of the bot's commands.",
        })
        super().__init__(**kwargs)

    # --- data assembly ---------------------------------------------------

    async def build_categories(self):
        """Return {category: [(token, description), ...]} visible to the invoker."""
        bot = self.context.bot
        invoker_is_admin = is_admin(self.context)
        categories = {}

        def category_for(cog_name):
            if cog_name is None:
                return "Other"
            return CATEGORY_MAP.get(cog_name, cog_name)

        # Prefix commands, grouped by cog (plus any cog-less commands).
        buckets = [(cog.qualified_name, cog.get_commands()) for cog in bot.cogs.values()]
        loose = [c for c in bot.commands if c.cog is None]
        if loose:
            buckets.append((None, loose))
        for cog_name, cmds in buckets:
            cat = category_for(cog_name)
            if cat in ADMIN_CATEGORIES and not invoker_is_admin:
                continue
            filtered = await self.filter_commands(cmds, sort=True)
            for cmd in filtered:
                categories.setdefault(cat, []).append((f"!{cmd.name}", cmd.short_doc))

        # Slash commands from the tree; ownership resolved via each cog's
        # registered app commands (do NOT hardcode group children — other
        # agents are reshaping them).
        owners = {}
        for cog in bot.cogs.values():
            for ac in cog.get_app_commands():
                owners[ac.name] = cog.qualified_name
        for cmd in bot.tree.get_commands():
            if isinstance(cmd, app_commands.ContextMenu):
                continue
            cat = category_for(owners.get(cmd.name))
            if cat in ADMIN_CATEGORIES and not invoker_is_admin:
                continue
            entries = sorted(_slash_entries(cmd))
            if entries:
                categories.setdefault(cat, []).extend(entries)

        # Multiple cogs can feed one category — order prefix commands first,
        # then slash commands, each alphabetically.
        return {
            name: sorted(entries, key=lambda e: (e[0].startswith("/"), e[0]))
            for name, entries in categories.items() if entries
        }

    async def build_overview(self):
        """Return (overview_embed, HelpView) for the invoker."""
        categories = await self.build_categories()
        overview = _build_overview_embed(categories)
        category_embeds = {
            name: _build_category_embed(name, entries)
            for name, entries in categories.items()
        }
        author = getattr(self.context, "author", None)
        view = HelpView(author.id, overview, category_embeds)
        return overview, view

    # --- HelpCommand surface ----------------------------------------------

    async def send_bot_help(self, mapping):
        overview, view = await self.build_overview()
        view.message = await self.get_destination().send(embed=overview, view=view)

    async def send_command_help(self, command):
        embed = discord.Embed(
            title=self.get_command_signature(command),
            description=command.help or command.short_doc or "No description available.",
            colour=EMBED_COLOUR,
        )
        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`!{alias}`" for alias in command.aliases),
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group):
        embed = discord.Embed(
            title=self.get_command_signature(group),
            description=group.help or group.short_doc or "No description available.",
            colour=EMBED_COLOUR,
        )
        if group.aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`!{alias}`" for alias in group.aliases),
                inline=False,
            )
        subcommands = await self.filter_commands(group.commands, sort=True)
        if subcommands:
            embed.add_field(
                name="Subcommands",
                value=_truncate(
                    "\n".join(f"`!{c.qualified_name}` — {c.short_doc}" for c in subcommands),
                    1024,
                ),
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_cog_help(self, cog):
        cat = CATEGORY_MAP.get(cog.qualified_name, cog.qualified_name)
        if cat in ADMIN_CATEGORIES and not is_admin(self.context):
            await self.send_error_message(self.command_not_found(cog.qualified_name))
            return
        filtered = await self.filter_commands(cog.get_commands(), sort=True)
        description = cog.description or ""
        if filtered:
            listing = "\n".join(f"`!{c.name}` — {c.short_doc}" for c in filtered)
            description = f"{description}\n\n{listing}" if description else listing
        if cog.qualified_name == "Media":
            description = MEDIA_NOTE + "\n\n" + description
        embed = discord.Embed(
            title=cat,
            description=_truncate(description or "No commands available.", 4096),
            colour=EMBED_COLOUR,
        )
        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error):
        await self.get_destination().send(error)


class Help(commands.Cog):
    """Interactive help: !help and /help."""

    def __init__(self, bot):
        self.bot = bot
        self._original_help_command = None

    async def cog_load(self):
        self._original_help_command = self.bot.help_command
        self.bot.help_command = LiterallyHelpCommand()
        self.bot.help_command.cog = self

    async def cog_unload(self):
        self.bot.help_command = self._original_help_command

    @app_commands.command(name="help", description="Interactive overview of the bot's commands")
    async def slash_help(self, interaction: discord.Interaction):
        # Reuse the prefix HelpCommand machinery (filter_commands needs a
        # Context) so both surfaces render the identical landing embed.
        ctx = await commands.Context.from_interaction(interaction)
        help_command = LiterallyHelpCommand()
        help_command.context = ctx
        overview, view = await help_command.build_overview()
        await interaction.response.send_message(embed=overview, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Help(bot))
