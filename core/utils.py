"""Shared helper utilities for permission checks and config access.

Normalization goals:
- Always store global "superadmins" as a list of ints.
- Provide backward-compatible helpers usable as either:
  - is_superadmin(config, user_id)
  - is_superadmin(ctx)
  - is_admin(config, ctx)
  - is_admin(ctx)
- Both `ctx` forms above accept a prefix commands.Context OR a
  discord.Interaction (slash commands) interchangeably. This is the single
  auth gate for both command surfaces - see _actor()/_bot_of() below.
"""
import os
import re
from typing import List, Union, Any
import discord

# The cogs/ groups, in load order. cogs/core/ is the recovery surface and is
# never filtered by disabled_cogs; every other group is a deployment choice.
# One owner for these names so the loader and the filter can never disagree.
CORE_COG_GROUP = "core"
COG_GROUPS = (CORE_COG_GROUP, "optional")


def _actor(ctx_or_interaction: Any) -> Any:
    """Return the invoking user/member for either a Context or an Interaction.

    commands.Context exposes `.author`; discord.Interaction exposes `.user`.
    """
    author = getattr(ctx_or_interaction, "author", None)
    if author is not None:
        return author
    return getattr(ctx_or_interaction, "user", None)


def _bot_of(ctx_or_interaction: Any) -> Any:
    """Return the bot/client for either a Context or an Interaction.

    commands.Context exposes `.bot`; discord.Interaction exposes `.client`.
    """
    bot = getattr(ctx_or_interaction, "bot", None)
    if bot is not None:
        return bot
    return getattr(ctx_or_interaction, "client", None)


def _normalize_superadmins_list(config) -> List[int]:
    superadmins = config.get(None, "superadmins", scope="global") or []
    changed = False
    if not isinstance(superadmins, list):
        superadmins = [superadmins]
        changed = True
    norm = []
    for item in superadmins:
        try:
            norm.append(int(item))
        except Exception:
            continue
    if changed or norm != superadmins:
        config.set(None, "superadmins", norm, scope="global")
    return norm


def get_superadmins(config) -> List[int]:
    """Return the list of global superadmins (normalized to list[int])."""
    return _normalize_superadmins_list(config)


def is_superadmin(config_or_ctx: Any, user_id: Union[int, None] = None) -> bool:
    """Check global superadmin membership.

    Supports both call styles:
    - is_superadmin(config, user_id)
    - is_superadmin(ctx)  # ctx may be a prefix Context or a slash Interaction
    """
    if user_id is None:
        # Treat first arg as ctx/interaction
        ctx = config_or_ctx
        bot = _bot_of(ctx)
        if bot is None:
            return False
        config = getattr(bot, "config", None)
        if config is None:
            return False
        actor = _actor(ctx)
        if actor is None:
            return False
        return actor.id in get_superadmins(config)
    else:
        # First arg is config, second is user id
        config = config_or_ctx
        return int(user_id) in get_superadmins(config)


def is_admin(config_or_ctx: Any, maybe_ctx: Any = None) -> bool:
    """Determine if invoking context has bot admin privileges.

    Supports both call styles:
    - is_admin(config, ctx)
    - is_admin(ctx)  # ctx may be a prefix Context or a slash Interaction
    """
    if maybe_ctx is None:
        ctx = config_or_ctx
        bot = _bot_of(ctx)
        if bot is None:
            return False
        config = getattr(bot, "config", None)
        if config is None:
            return False
    else:
        config = config_or_ctx
        ctx = maybe_ctx

    actor = _actor(ctx)
    if actor is None:
        return False

    # The bot's own account is never bot-admin. Its Discord Administrator role
    # would otherwise pass the guild_permissions check below, letting a
    # self-authored command (agent/MCP driving this bot) escalate.
    bot = _bot_of(ctx)
    bot_user = getattr(bot, "user", None)
    if bot_user is not None and actor.id == bot_user.id:
        return False

    if is_superadmin(config, actor.id):
        return True

    if ctx.guild is None:
        return False

    admins = config.get(ctx, "admins", [])
    if actor.id in (admins or []):
        return True

    # actor may be a bare id-holder or discord.User (no guild_permissions)
    # when called from a non-cog frontend (ops registry / MCP server).
    if getattr(getattr(actor, "guild_permissions", None), "administrator", False):
        return True

    if actor == getattr(ctx.guild, "owner", None):
        return True

    return False


# Gate tiers, stamped onto check predicates by the factories below so a
# listing can ask "what does this command require?" without running it.
GATE_ADMIN = "admin"
GATE_SUPERADMIN = "superadmin"


def gate_of(check) -> Union[str, None]:
    """The tier a check predicate declares, or None if it doesn't say.

    Reading this off the decorator is what lets !help — which has a Context
    and so cannot evaluate an app-command predicate typed for Interaction —
    still render the right listing. An untagged check is treated as gated
    but of unknown tier by callers, never as public."""
    return getattr(check, "__gate__", None)


def app_is_admin():
    """`@app_commands.check` form of is_admin, tagged with its tier.

    Slash commands whose gate is this bot's admin list (not Discord's
    permissions) must NOT use @app_commands.default_permissions: that field
    means something different, and a listing reading it would either hide
    the command from people who can run it or show it to people who can't.
    Declaring the real gate here keeps enforcement and visibility on the
    same decorator, so they cannot drift."""
    async def predicate(interaction) -> bool:
        return is_admin(interaction)
    predicate.__gate__ = GATE_ADMIN
    return discord.app_commands.check(predicate)


def app_is_superadmin():
    """`@app_commands.check` form of is_superadmin, tagged with its tier."""
    async def predicate(interaction) -> bool:
        return is_superadmin(interaction)
    predicate.__gate__ = GATE_SUPERADMIN
    return discord.app_commands.check(predicate)


def list_cog_modules(group: str, config=None) -> List[str]:
    """Loadable cog modules for a cogs/ group, as dotted module paths
    (['cogs.optional.gpt', ...]). THE one owner of the loadable-cog rule
    (*.py, not underscore/dunder-prefixed) — startup load, the !cogs panel,
    and !list_cogs must all resolve the cog set through this so they can
    never disagree about what counts as loadable. Missing directory
    yields [] (mirrors the startup skip).

    When a Config is passed, cogs named in the global `disabled_cogs` list
    (bare lowercase names, e.g. "gpt") are excluded — the deployment-level
    off switch that lets downstream forks carry upstream cogs on disk
    without running them. Omitting config yields the full on-disk set
    (what !list_cogs uses to show disabled entries).

    cogs/core/ is the ONE exception and is never filtered: it holds the
    recovery surface (control.py's enable/disable/restart and the !config
    editor, plus admin.py's claimsuper bootstrap). Disabling
    those removes the means of re-enabling anything from Discord, leaving
    shell access as the only way back. Everything else — including error
    handling, which is wired in bot.py from core/error_handler.py and does
    not need its cog loaded — is a deployment choice.

    The test is deliberately `group != CORE_COG_GROUP` rather than an
    allow-list of filterable groups, so a future third folder fails CLOSED
    (disableable) instead of silently inheriting core's protection."""
    dir_path = f"./cogs/{group}"
    if not os.path.isdir(dir_path):
        return []
    disabled = set()
    if config is not None and group != CORE_COG_GROUP:
        disabled = {str(name).lower()
                    for name in (config.get_global("disabled_cogs", []) or [])}
    return [f"cogs.{group}.{filename[:-3]}"
            for filename in os.listdir(dir_path)
            if filename.endswith('.py') and not filename.startswith('_')
            and filename[:-3].lower() not in disabled]


class InvokerOnlyView:
    """Mixin giving an interactive panel its single-invoker lifecycle.

    Mix in BEFORE the discord view base so these hooks win the MRO:

        class MyView(InvokerOnlyView, discord.ui.View):
            panel_command = "!mypanel"

    Supplies the two halves every panel in this repo hand-rolled
    identically: `interaction_check` rejecting non-invokers ephemerally,
    and an `on_timeout` that disables every child and best-effort-edits
    the source message (swallowing HTTPException, since an expired panel
    whose message was deleted must not raise).

    It is a mixin rather than a base class because panels sit on two
    different discord bases — `discord.ui.View` and Components-V2
    `discord.ui.LayoutView` — and child enumeration differs between them
    (`walk_children` when present, else `children`).

    Subclasses set `panel_command` (used to build both the rejection and
    expiry text) or override `rejection_text` / `expiry_text` outright;
    an `expiry_text` of None leaves the message content untouched and
    only disables the controls.

    Requires `self.invoker_id`; `self.message` defaults to None so a panel
    that never stored its message still times out cleanly.
    """

    panel_command = None
    message = None
    invoker_id = None

    @property
    def rejection_text(self) -> str:
        if self.panel_command:
            return (f"This panel isn't yours — run {self.panel_command} "
                    f"to open your own.")
        return "This panel isn't yours."

    @property
    def expiry_text(self):
        if self.panel_command:
            return f"Panel expired — run {self.panel_command} again."
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                self.rejection_text, ephemeral=True)
            return False
        return True

    def _all_children(self):
        """Every component, across both view bases.

        LayoutView nests its components inside containers, so `children`
        alone would leave the inner controls live after expiry.
        """
        walk = getattr(self, "walk_children", None)
        return walk() if callable(walk) else self.children

    async def on_timeout(self):
        for child in self._all_children():
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is None:
            return
        try:
            text = self.expiry_text
            if text is None:
                await self.message.edit(view=self)
            else:
                await self.message.edit(content=text, view=self)
        except discord.HTTPException:
            # Message deleted or otherwise unreachable — expiry is
            # best-effort; the controls are dead either way.
            pass


def smart_split(options):
    """Split a user-supplied options string on 'or', commas, or whitespace.

    Moved here from the root-level utils.py so all shared helpers live in
    core.utils.
    """
    if re.search(r',? ?\bor\b ?|, ?', options.lower()) is not None:
        values = re.split(r',? ?\bor\b ?|, ?', options.lower())
    elif ' ' in options:
        values = options.split(' ')
    else:
        values = re.split(r'\W', options)
    return [value for value in values if value != ""]


async def safe_delete(ctx, logger=None):
    """Safely attempt to delete a command message without raising exceptions.

    Args:
        ctx: Discord command context
        logger: Optional logger instance for warnings

    Returns:
        bool: True if deletion succeeded, False otherwise
    """
    try:
        await ctx.message.delete()
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        if logger:
            channel_name = getattr(ctx.channel, "name", ctx.channel.id)
            logger.warning(f"Unable to delete command message in {channel_name}: {exc}")
        return False


def recursive_split(text, max_size=2000):
    """Split text into Discord-size chunks, preferring newline/sentence/space
    boundaries and keeping fenced/inline code blocks intact across chunks.
    The single shared implementation of Discord's 2000-char message limit —
    moved here from cogs/optional/gpt.py (seam-machine claim 3)."""
    if len(text) <= max_size:
        return [text]
    mid = len(text) // 2

    # Updated pattern with optional language capture for code blocks.
    code_block_pattern = r'(`{3,})(\w+)?\n[\s\S]*?\n\1'
    inline_code_pattern = r'(`+)[^`]+?\1'

    code_blocks = list(re.finditer(code_block_pattern, text))
    inline_codes = list(re.finditer(inline_code_pattern, text))

    for pattern in [r'\n+', r'\.\s+', r'\s+']:
        matches = list(re.finditer(pattern, text))
        if matches:
            best_match = min(matches, key=lambda m: abs(m.start() - mid))
            split_index = best_match.end()
            if split_index <= 0 or split_index >= len(text):
                continue

            inside_code_block = False
            code_delimiter = None
            code_lang = ""
            for block in code_blocks:
                if block.start() < split_index < block.end():
                    inside_code_block = True
                    code_delimiter = block.group(1)  # e.g. "```"
                    code_lang = block.group(2) if block.group(2) else ""
                    break

            inside_inline_code = any(code.start() < split_index < code.end() for code in inline_codes)

            left = text[:split_index].rstrip()
            right = text[split_index:].lstrip()

            if inside_code_block and code_delimiter:
                header = code_delimiter + code_lang  # Preserve language specifier.
                if not left.endswith(header):
                    left = left + "\n" + "```"
                if not right.startswith(header):
                    right = header + "\n" + right

            if inside_inline_code:
                if not left.endswith("`"):
                    left = left.rstrip("`") + "`"
                if not right.startswith("`"):
                    right = "`" + right.lstrip("`")

            # Progress guard: fence repair can grow a side back to (or past)
            # the original length when a single fenced block exceeds max_size,
            # which would recurse forever — fall through to the hard cut,
            # which always shrinks the input.
            if len(left) >= len(text) or len(right) >= len(text):
                continue

            return recursive_split(left, max_size) + recursive_split(right, max_size)

    left = text[:max_size].rstrip()
    right = text[max_size:].lstrip()
    inside_code_block = False
    code_delimiter = None
    code_lang = ""
    for block in code_blocks:
        if block.start() < max_size < block.end():
            inside_code_block = True
            code_delimiter = block.group(1)
            code_lang = block.group(2) if block.group(2) else ""
            break
    inside_inline_code = any(code.start() < max_size < code.end() for code in inline_codes)
    if inside_code_block and code_delimiter:
        header = code_delimiter + code_lang
        if not left.endswith(header):
            left = left + "\n" + header
        if not right.startswith(header):
            right = header + "\n" + right
    elif inside_inline_code:
        if not left.endswith("`"):
            left = left.rstrip("`") + "`"
        if not right.startswith("`"):
            right = "`" + right.lstrip("`")
    return [left] + recursive_split(right, max_size)
