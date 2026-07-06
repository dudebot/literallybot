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
import re
from typing import List, Union, Any
import discord


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
    moved here from cogs/dynamic/gpt.py (seam-machine claim 3)."""
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
