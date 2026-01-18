import discord
from discord.ext import commands, tasks
import time

class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_reminders.start()
    
    def cog_unload(self):
        self.check_reminders.cancel()
    
    @commands.command(name="remindme", aliases=["setreminder"])
    async def remindme(self, ctx, *, args: str = None):
        """
        Sets a reminder for the user.
        Example usage:
          !remindme 10 minutes Check the oven
        """
        usage = "Usage: `!remindme <number> <unit> <message>`\nExample: `!remindme 10 minutes Check the oven`"

        if not args:
            await ctx.send(usage)
            return

        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            await ctx.send(usage)
            return

        number_str, unit, text = parts

        try:
            number = int(number_str)
        except ValueError:
            await ctx.send(usage)
            return

        unit_lower = unit.lower()
        if unit_lower not in ["minutes", "hours", "days", "minute", "hour", "day", "m", "h", "d", "min", "hr"]:
            await ctx.send(f"Unit {unit} must be 'minutes', 'hours' or 'days'.")
            return
        current_time = int(time.time())
        if unit_lower == "minutes" or unit_lower == "minute" or unit_lower == "m" or unit_lower == "min":
            delay = number * 60
        elif unit_lower == "hours" or unit_lower == "hour" or unit_lower == "h" or unit_lower == "hr":
            delay = number * 3600
        else:
            delay = number * 86400
        remind_time = current_time + delay
        config = self.bot.config
        reminders = config.get(None, "reminders", [])
        reminders.append({"user_id": ctx.author.id, "timestamp": remind_time, "text": text})
        config.set(None, "reminders", reminders)
        await ctx.send(f"Reminder set for {number} {unit_lower} from now.")

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
                        print(f"Failed to fetch user {reminder['user_id']}: {e}")
                        continue
                try:
                    await user.send(f"Reminder: {reminder['text']}")
                except Exception as e:
                    print(f"Failed to send DM to {reminder['user_id']}: {e}")
            else:
                updated_reminders.append(reminder)
        if len(updated_reminders) != len(reminders):
            config.set(None, "reminders", updated_reminders)
    
    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Reminders(bot))
