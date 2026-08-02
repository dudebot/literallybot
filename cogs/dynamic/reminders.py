import discord
from discord.ext import commands, tasks
import re
import time

# Longest units first within each duration bucket so the alternation doesn't
# stop early (e.g. "hr" must be tried before "h").
_UNIT_SECONDS = {
    60: ["minutes", "minute", "mins", "min", "m"],
    3600: ["hours", "hour", "hrs", "hr", "h"],
    86400: ["days", "day", "d"],
}
_UNIT_TO_SECONDS = {
    unit: seconds for seconds, units in _UNIT_SECONDS.items() for unit in units
}
_UNIT_ALTERNATION = "|".join(
    sorted(map(re.escape, _UNIT_TO_SECONDS), key=len, reverse=True)
)
# (?=$|\d|\W) stops a unit alias from matching as a bare prefix of a real
# word, e.g. "1meter"/"1ms" no longer get eaten as "1m" (minutes).
_UNIT = rf"(?:{_UNIT_ALTERNATION})(?=$|\d|\W)"
_CHUNK = rf"\d+\s*{_UNIT}"
# Matches one or more "<number><unit>" chunks, optionally separated by
# whitespace/commas/"and", e.g. "3h", "3 h", "1d12h", "1 day, 2 hours".
# A trailing separator is only consumed when another chunk follows it, so
# "1h and call mom" keeps "and" in the message instead of swallowing it.
_DURATION_RE = re.compile(
    rf"^\s*({_CHUNK}(?:\s*(?:,|and)?\s*{_CHUNK})*)\s*,?\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CHUNK_RE = re.compile(rf"(\d+)\s*({_UNIT_ALTERNATION})(?=$|\d|\W)", re.IGNORECASE)


def parse_duration(args: str):
    """
    Parses a leading duration off the front of `args`.
    Accepts spaced ("3 h"), glued ("3h"), and multi-part ("1d 12h") forms.
    Returns (delay_seconds, remainder_text) or None if no duration is found.
    """
    match = _DURATION_RE.match(args)
    if not match:
        return None

    duration_part, remainder = match.groups()
    delay = 0
    for number_str, unit in _CHUNK_RE.findall(duration_part):
        delay += int(number_str) * _UNIT_TO_SECONDS[unit.lower()]

    remainder = remainder.strip()
    if delay <= 0 or not remainder:
        return None
    return delay, remainder


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    @commands.command(name="remindme", aliases=["setreminder", "reminder", "r"])
    async def remindme(self, ctx, *, args: str = None):
        """
        Sets a reminder for the user.
        Example usage:
          !remindme 10 minutes Check the oven
          !remindme 3h https://example.com/some/link
          !remindme 1d12h Check the oven
        """
        usage = (
            "Usage: `!remindme <duration> <message>`\n"
            "Duration examples: `10 minutes`, `3h`, `1d12h`\n"
            "Example: `!remindme 10 minutes Check the oven`"
        )

        if not args:
            await ctx.send(usage)
            return

        parsed = parse_duration(args)
        if not parsed:
            await ctx.send(usage)
            return

        delay, text = parsed
        current_time = int(time.time())
        remind_time = current_time + delay
        config = self.bot.config
        reminders = config.get(None, "reminders", [])
        reminders.append({"user_id": ctx.author.id, "timestamp": remind_time, "text": text})
        config.set(None, "reminders", reminders)
        await ctx.send(f"Reminder set — <t:{remind_time}:R>.")

    @tasks.loop(seconds=10)
    async def check_reminders(self):
        current_time = int(time.time())
        config = self.bot.config
        reminders = config.get(None, "reminders", [])
        updated_reminders = []
        for reminder in reminders:
            if reminder["timestamp"] <= current_time:
                user = self.bot.get_user(reminder["user_id"])
                if not user:
                    try:
                        user = await self.bot.fetch_user(reminder["user_id"])
                    except Exception as e:
                        self.logger.warning(f"Failed to fetch user {reminder['user_id']}: {e}")
                        continue
                try:
                    await user.send(f"Reminder: {reminder['text']}")
                except Exception as e:
                    self.logger.warning(f"Failed to send DM to {reminder['user_id']}: {e}")
            else:
                updated_reminders.append(reminder)
        if len(updated_reminders) != len(reminders):
            config.set(None, "reminders", updated_reminders)
    
    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Reminders(bot))
