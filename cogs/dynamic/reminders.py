import discord
from discord.ext import commands, tasks
import re
import time

# Snooze bounds: below 10 minutes a snooze races the 10s delivery loop and
# reads as noise; above 30 days it outlives the reminder's relevance.
SNOOZE_MIN_SECONDS = 10 * 60
SNOOZE_MAX_SECONDS = 30 * 86400
# Hybrid scheme, chosen by USE CASE (labeled from real invocations,
# 2026-08), not just the duration distribution. Two real categories:
#   - deadline-anchored ("stream in 36h", "boost in february"): the event
#     time is fixed, so proportional re-offsets are meaningless — the only
#     useful snooze is a short "ping me again soon" refresh.
#   - fuzzy-future todos (most usage): "push it by the same again / by
#     double" is exactly right, so proportional options scale with the
#     reminder (durations span 1min–300days; no static set serves that).
# The text can't tell us which category a reminder is, so every delivery
# offers the static floor PLUS the proportional pair, deduped after
# clamping. Legacy reminders stored without a duration fall back to
# static offsets.
SNOOZE_STATIC_FLOOR = 600  # 10m — always offered
SNOOZE_MULTIPLIERS = (1.0, 2.0)
SNOOZE_STATIC_FALLBACK = (600, 3600, 86400)  # 10m / 1h / 1d


def format_duration(seconds: int) -> str:
    """Compact human duration: 90 -> '1m', 5400 -> '1h30m', 129600 -> '1d12h'."""
    seconds = max(int(seconds), 60)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [(days, "d"), (hours, "h"), (minutes, "m")]
    out = "".join(f"{n}{u}" for n, u in parts if n)
    return out or "1m"


def snooze_offsets(original_delay=None):
    """The snooze durations offered for a reminder, ascending, deduped."""
    if not original_delay:
        return list(SNOOZE_STATIC_FALLBACK)
    offsets = [SNOOZE_STATIC_FLOOR]
    for mult in SNOOZE_MULTIPLIERS:
        secs = int(original_delay * mult)
        secs = max(SNOOZE_MIN_SECONDS, min(SNOOZE_MAX_SECONDS, secs))
        if secs not in offsets:
            offsets.append(secs)
    return offsets


class SnoozeButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"remsnooze:(?P<secs>\d+)"):
    """A snooze button whose entire state is its custom_id.

    The final snooze duration rides in the custom_id and the reminder text
    is recovered from the message it is attached to, so the button needs no
    server-side state and keeps working across bot restarts (registered via
    bot.add_dynamic_items in setup).
    """

    def __init__(self, secs: int):
        secs = int(secs)
        self.secs = secs
        # 💤 instead of the word "Snooze": three buttons must fit one row on
        # a small phone screen, and the word alone blew the width budget.
        super().__init__(discord.ui.Button(
            label=format_duration(secs),
            emoji="\N{SLEEPING SYMBOL}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"remsnooze:{secs}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["secs"]))

    async def callback(self, interaction: discord.Interaction):
        text = interaction.message.content
        if text.startswith("Reminder: "):
            text = text[len("Reminder: "):]
        remind_time = int(time.time()) + self.secs
        config = interaction.client.config
        reminders = config.get(None, "reminders", [])
        reminders.append({
            "user_id": interaction.user.id,
            "timestamp": remind_time,
            "text": text,
            "delay": self.secs,
        })
        config.set(None, "reminders", reminders)
        # Strip the buttons so one delivery can't be snoozed twice, then
        # confirm on the same message.
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(f"Snoozed — <t:{remind_time}:R>.")


def snooze_view(original_delay=None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for secs in snooze_offsets(original_delay):
        view.add_item(SnoozeButton(secs))
    return view

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
        # "delay" feeds the snooze buttons at delivery (0.5x/1x/2x of the
        # original duration); legacy rows without it get static offsets.
        reminders.append({"user_id": ctx.author.id, "timestamp": remind_time,
                          "text": text, "delay": delay})
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
                    await user.send(f"Reminder: {reminder['text']}",
                                    view=snooze_view(reminder.get("delay")))
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
    # DynamicItem registration is what routes remsnooze:* interactions after
    # a restart, when the delivering view object no longer exists.
    bot.add_dynamic_items(SnoozeButton)
    await bot.add_cog(Reminders(bot))
