import random
import time
from discord.ext import commands
from phue import Bridge
import os

class PhueCog(commands.Cog):
    """Cog to interact with Philips Hue lights."""
    def __init__(self, bot):
        self.bot = bot
        self.bridge = Bridge(os.getenv("HUE_BRIDGE_IP"))
        self.bridge.connect()
        self.lights = self.bridge.lights

    def flash_lights(self, flash_times, delay):
        # Turn on all lights
        for light in self.lights:
            light.on = True
        # Flash lights for flash_times iterations
        for _ in range(flash_times):
            for light in self.lights:
                if "bed" in light.name.lower():
                    random_hue = random.randint(0, 65535)
                    random_sat = random.randint(100, 254)
                    light.on = not light.on
                    if light.on:
                        light.brightness = 254
                        light.hue = random_hue
                        light.saturation = random_sat
            time.sleep(delay)

    @commands.command(name='annoydudebot', description='Annoys dude bot by flashing Hue lights.', hidden=True)
    async def annoydudebot(self, ctx):
        self.flash_lights(3, 3)
        await ctx.send("Annoyed the dude bot!")

async def setup(bot):
    await bot.add_cog(PhueCog(bot))