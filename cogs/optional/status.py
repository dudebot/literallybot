"""Presence rotation — cycles the bot's status line on a fixed interval.

Lives in cogs/optional rather than the entrypoint so the loop and its error
handler are declared together at class scope: the decorators run at import,
which makes the handler registration structurally correct instead of
depending on where it sits relative to the blocking bot.run() call.

Messages come from configs/status_messages.txt (one per line, blanks
ignored), falling back to a built-in list when the file is missing or
unreadable.
"""

from itertools import cycle

import discord
from discord.ext import commands, tasks

from core.error_handler import (
    ErrorCategory, ErrorSeverity, log_error_to_discord,
)

STATUS_FILE = "configs/status_messages.txt"
DEFAULT_STATUSES = ["01010101", "01110111", "01010101", "01111110"]
ROTATE_SECONDS = 300


class StatusRotation(commands.Cog):
    """Rotates the bot's presence through the configured status messages."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.statuslist = self.load_status_messages()

    def load_status_messages(self):
        """Status messages as an endless cycle, defaults if unavailable."""
        try:
            with open(STATUS_FILE, 'r') as f:
                messages = [line.strip() for line in f if line.strip()]
            if messages:
                self.logger.info(
                    f"Loaded {len(messages)} status messages from {STATUS_FILE}")
                return cycle(messages)
            self.logger.info(f"{STATUS_FILE} is empty, using defaults")
        except FileNotFoundError:
            self.logger.info(f"{STATUS_FILE} not found, using defaults")
        except OSError as e:
            self.logger.error(f"Error loading status messages: {e}, using defaults")
        return cycle(DEFAULT_STATUSES)

    async def cog_load(self):
        self.change_status.start()

    def cog_unload(self):
        self.change_status.cancel()

    @tasks.loop(seconds=ROTATE_SECONDS)
    async def change_status(self):
        """Set the next presence in the cycle."""
        await self.bot.change_presence(
            activity=discord.Game(next(self.statuslist)))

    @change_status.before_loop
    async def before_change_status(self):
        # change_presence needs a live gateway connection; cog_load runs
        # before the bot connects.
        await self.bot.wait_until_ready()

    @change_status.error
    async def change_status_error(self, error):
        """Report a loop failure instead of letting rotation die silently."""
        self.logger.error(f"Error in change_status task: {error}", exc_info=True)
        try:
            await log_error_to_discord(
                self.bot, error, 'task_change_status',
                category=ErrorCategory.TASK_ERROR,
                severity=ErrorSeverity.WARNING,
            )
        except Exception as log_error:
            self.logger.error(
                f"Failed to log error to Discord: {log_error}", exc_info=True)


async def setup(bot):
    await bot.add_cog(StatusRotation(bot))
