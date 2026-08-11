"""literallybot — a general-purpose Discord bot built on discord.py.

Entry point: creates the bot, loads cogs from cogs/core (the recovery
surface, never disableable) and cogs/optional (everything a deployment can
switch off via the global `disabled_cogs` config), and wires logging, error
handling, and the optional MCP ops server. Runtime configuration lives in
configs/ as JSON.
"""
from discord.ext import commands
import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import sys
from core.config import Config
from core import bootstrap
from core.dm_log import log_dm, row_from_message
from core.ops import registry as ops_registry
from core.error_handler import (
    log_error_to_discord, ErrorCategory, ErrorSeverity,
    handle_command_error, handle_app_command_error, handle_event_error
)
# Logging setup
import logging
from logging.handlers import RotatingFileHandler
# Ensure logs directory exists
os.makedirs('logs', exist_ok=True)
# Configure logging
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s: %(message)s',
    handlers=[
        RotatingFileHandler('logs/bot.log', maxBytes=5*1024*1024, backupCount=5),
        logging.StreamHandler()
    ])
logger = logging.getLogger(__name__)

def get_prefix(bot, message):
    """This function returns a Prefix for our bot's commands.
    
    Args:
        bot (commands.Bot): The bot that is invoking this function.
        message (discord.Message): The message that is invoking.
        
    Returns:
        string or iterable conteining strings: A string containing prefix or an iterable containing prefixes
    Notes:
        Through a database (or even a json) this function can be modified to returns per server prefixes.
        This function should returns only strings or iterable containing strings.
        This function shouldn't returns numeric values (int, float, complex).
        Empty strings as the prefix always matches, and should be avoided, at least in guilds. 
    """
    if not isinstance(message.guild, discord.Guild):
        """Checks if the bot isn't inside of a guild. 
        Returns a prefix string if true, otherwise passes.
        """
        return '!'

    return ['!']

class LiterallyBot(commands.Bot):
    async def setup_hook(self):
        """Runs once after login, BEFORE the gateway connects.

        Cogs (and the persistent-view registrations their setup() functions
        perform via bot.add_view / bot.add_dynamic_items) must be in place
        before any component interaction can arrive — on_ready is too late
        and fires again on every reconnect.

        It is also the token-verification boundary (#83): discord.py reaches
        setup_hook only after the REST login succeeded, so a token typed at the
        first-run prompt is written to disk here and nowhere earlier. An env-
        supplied token is never a candidate (core.bootstrap.run_bot leaves
        `_pending_token` None for it), and the attribute is cleared after the
        write so a reconnect can't re-persist.
        """
        pending = getattr(self, "_pending_token", None)
        if pending:
            self._pending_token = None
            try:
                bootstrap.persist_token(self.config, pending)
            except Exception:
                logger.error("Login succeeded but saving the token failed; "
                             "the bot is running, you will be prompted again "
                             "next start.", exc_info=True)
        await load_cogs()

    async def add_cog(self, cog, **kwargs):
        """Register the cog's `@op(...)` methods as it loads.

        Mirrors discord.py's own CogMeta/_eject lifecycle: a cog's ops live
        exactly as long as the cog does. In practice that means the whole
        process lifetime — the cog set is fixed at boot (#86), so this runs
        during startup and its `remove_cog` twin at shutdown teardown. The
        pairing is kept general anyway: it is what makes ops fail CLOSED if
        a cog ever goes away mid-run.

        Registration is all-or-none inside the registry. If it raises (a
        duplicate op name, a malformed declaration), the cog is ejected
        again before the error propagates: discord.py must not end up
        holding a loaded cog whose ops silently aren't there.
        """
        await super().add_cog(cog, **kwargs)
        try:
            names = ops_registry.register_cog_ops(cog)
        except Exception:
            await super().remove_cog(cog.qualified_name)
            raise
        if names:
            logger.info(f"Registered {len(names)} op(s) from {cog.qualified_name}: "
                        f"{', '.join(names)}")

    async def remove_cog(self, name, **kwargs):
        """Drop the cog's ops as it unloads.

        The unregistration runs in a `finally` so a cog whose own teardown
        raises still leaves no orphaned ops behind — a registered op whose
        owning cog is gone would fail at call time with a confusing error
        and would keep its name reserved against the next registration.
        """
        cog = self.get_cog(name)
        try:
            return await super().remove_cog(name, **kwargs)
        finally:
            if cog is not None:
                removed = ops_registry.unregister_owner(cog)
                if removed:
                    logger.info(f"Unregistered {len(removed)} op(s) from {name}: "
                                f"{', '.join(removed)}")


bot = LiterallyBot(command_prefix=get_prefix, intents=discord.Intents.all())
# Attach central logger to bot for use in cogs
bot.logger = logger
bot.config = Config()

# Function to load all cogs from ./cogs/{core,optional}
async def load_cogs():
    failed_cogs = []  # Track failed cogs for reporting

    from core.utils import COG_GROUPS, list_cog_modules

    for group in COG_GROUPS:
        # bot.config filters out globally disabled cogs (never cogs/core/).
        for cog_name in list_cog_modules(group, bot.config):
            try:
                # Skip if already loaded (handles reconnection scenarios)
                if cog_name in bot.extensions:
                    logger.debug(f"{cog_name} already loaded, skipping")
                    continue

                await bot.load_extension(cog_name)
                logger.info(f"Successfully loaded {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load {cog_name}: {e}", exc_info=True)
                # Store failed cog info for Discord reporting
                failed_cogs.append({
                    'name': cog_name,
                    'error': str(e),
                    'type': type(e).__name__
                })

    # Store failed cogs on bot for later reporting
    bot.failed_cogs = failed_cogs if failed_cogs else []

@bot.event
async def on_ready():
    """Called on connect (and reconnect): syncs application commands once
    per process. Cogs are loaded in LiterallyBot.setup_hook (before the
    gateway connects) so persistent views are registered before any click
    arrives; status rotation lives in cogs/optional/status.py."""

    logger.info(f'{bot.user.name} is online and ready!')

    # First-run superadmin (#83): on a fresh install the `superadmins` list is
    # empty, and the application owner is granted it automatically instead of
    # having to discover the `!claimsuper` incantation. Gated on the empty
    # list ONLY, so an existing deployment is never touched. Guarded by the
    # same once-per-process flag family as the tree sync — on_ready refires.
    if not getattr(bot, "_superadmin_bootstrapped", False):
        bot._superadmin_bootstrapped = True
        try:
            await bootstrap.bootstrap_superadmin(bot)
        except Exception:
            logger.error("Superadmin bootstrap failed; use !claimsuper.",
                         exc_info=True)

    # on_ready refires on reconnect — only sync the command tree once per
    # process (Control's !sync command handles manual re-syncs).
    if not getattr(bot, "_synced", False):
        await bot.tree.sync()
        bot._synced = True

    # MCP ops server — OFF unless the `mcp_ops_enabled` global config bool is
    # set; loopback-only, bearer auth mandatory (a token is generated and
    # stored in global config if none is configured). See core/mcp_server.py.
    # Started here, not in setup_hook, so its tool surface is built from a
    # registry that already has every cog's ops in it.
    if getattr(bot, '_mcp_ops_task', None) is None:
        try:
            from core.mcp_server import maybe_start_in_bot
            bot._mcp_ops_task = maybe_start_in_bot(bot)
        except Exception as e:
            logger.error(f"Failed to start MCP ops server: {e}", exc_info=True)

    # Report any failed cog loads to Discord now that we're connected
    if hasattr(bot, 'failed_cogs') and bot.failed_cogs:
        try:
            from core.error_handler import log_error_to_discord, ErrorCategory, ErrorSeverity

            # Create a custom exception for cog loading failures
            error_msg = f"Failed to load {len(bot.failed_cogs)} cog(s) during startup:\n\n"
            for cog_info in bot.failed_cogs:
                error_msg += f"• **{cog_info['name']}**: {cog_info['type']} - {cog_info['error']}\n"

            class CogLoadError(Exception):
                pass

            error = CogLoadError(error_msg)

            # Send to Discord with high severity
            await log_error_to_discord(
                bot,
                error,
                "startup_cog_load",
                category=ErrorCategory.OTHER,
                severity=ErrorSeverity.CRITICAL,
                extra_info=f"Total cogs failed: {len(bot.failed_cogs)}"
            )
            logger.info(f"Reported {len(bot.failed_cogs)} cog loading failures to Discord")
        except Exception as report_error:
            logger.error(f"Failed to report cog loading errors to Discord: {report_error}")

@bot.event
async def on_message(message):
    if message.author.bot:
        # discord.py's process_commands drops ALL bot-authored messages, so a
        # bot (including this bot itself, e.g. via its MCP ops server) can
        # never trigger a command through the normal path. Config-gated shim:
        # authors on the global `command_author_allowlist` (default empty =
        # feature off) get their prefixed messages processed as commands.
        # Requiring the command prefix keeps this bot's own replies — which
        # never start with the prefix — from re-triggering commands.
        allowlist = bot.config.get(None, "command_author_allowlist", scope="global") or []
        if message.author.id in allowlist:
            prefixes = await bot.get_prefix(message)
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            if any(message.content.startswith(p) for p in prefixes):
                ctx = await bot.get_context(message)
                if not ctx.valid and message.author.id == bot.user.id:
                    # Bot.get_context early-returns WITHOUT prefix/command
                    # parsing for self-authored messages (discord.py 2.x,
                    # ext/commands/bot.py). Finish the identical parse here
                    # so an allowlisted self-invocation (e.g. sent through
                    # the bot's own MCP ops server) still resolves.
                    prefix = next((p for p in prefixes if message.content.startswith(p)), None)
                    if prefix is not None and ctx.view.skip_string(prefix):
                        ctx.invoked_with = ctx.view.get_word()
                        ctx.prefix = prefix
                        ctx.command = bot.all_commands.get(ctx.invoked_with)
                if ctx.valid:
                    logger.info(
                        f'Processing allowlisted bot-authored command from '
                        f'{message.author} (ID: {message.author.id}): {message.content[:100]}'
                    )
                    # Escalation guard lives at the root: core.utils.is_admin /
                    # is_superadmin never grant the bot's own account privileges
                    # (its Discord Administrator role would otherwise pass the
                    # admin gate), so a self-invoked admin command fails closed.
                    await bot.invoke(ctx)
        return
    if isinstance(message.channel, discord.DMChannel):
        logger.info(f'Received DM from {message.author} (ID: {message.author.id}): {message.content}')
        # Persist inbound DMs so read_dms can serve them back. Never let a
        # storage failure swallow the message — logging and command
        # processing must still happen.
        try:
            log_dm(message.author.id, row_from_message(message, message.author.id))
        except Exception as dm_log_error:
            logger.error(f"Failed to persist inbound DM: {dm_log_error}")
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    """Handle errors in text commands with enhanced logging."""
    await handle_command_error(bot, ctx, error)

@bot.event
async def on_command(ctx):
    logger.info(f'Command {ctx.command} invoked by {ctx.author} (ID: {ctx.author.id}) args={ctx.args} kwargs={ctx.kwargs}')

@bot.event
async def on_command_completion(ctx):
    logger.info(f'Command {ctx.command} completed by {ctx.author} in {ctx.channel}')

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    """Handle errors in slash commands with enhanced logging."""
    await handle_app_command_error(bot, interaction, error)

@bot.event
async def on_error(event, *args, **kwargs):
    """Handle errors in events with enhanced logging."""
    await handle_event_error(bot, event, *args, **kwargs)

if __name__ == "__main__":
    # .env is DEPRECATED (#83) but still loaded when present, so existing
    # deployments keep working untouched: load_dotenv only populates env vars
    # that aren't already set, which leaves a real DISCORD_TOKEN env var (the
    # panel/Docker/systemd contract) winning over a stale file.
    load_dotenv()

    try:
        # Resolves env -> config -> interactive prompt -> exit-with-instructions,
        # and runs the bot. See core/bootstrap.py for the full chain.
        bootstrap.run_bot(bot)
    except SystemExit:
        raise
    except Exception:
        logger.critical('Bot terminated unexpectedly', exc_info=True)
        # Exit NONZERO. A crashed bot that reports success is the exact
        # confusion #83 exists to remove: a panel host shows a green "stopped
        # normally", and `./start.sh && echo ok` prints ok. (Previously this
        # fell through to a 0 exit.)
        sys.exit(1)
    finally:
        # Properly shutdown config system
        bot.config.shutdown()
        logger.info('Config system shutdown complete')
    #Runs the bot with its token. Don't put code below this command.
