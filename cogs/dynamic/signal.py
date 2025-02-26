from discord.ext import commands
import random
import os
import time
from phue import Bridge

class Signal(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='signal', description='Signal command.', hidden=True)
    async def signal(self, ctx):
        try:
            bridge_ip = os.getenv("HUE_BRIDGE_IP")
            bridge = Bridge(bridge_ip)
            bridge.connect()
            lights = bridge.lights

            def flash_lights(flash_count, pause):
                for lamp in lights:
                    lamp.on = True
                for _ in range(flash_count):
                    for lamp in lights:
                        if "bed" in lamp.name.lower():
                            lamp.on = not lamp.on
                            if lamp.on:
                                lamp.brightness = 254
                                lamp.hue = random.randint(0, 65535)
                                lamp.saturation = random.randint(100, 254)
                    time.sleep(pause)

            flash_lights(20, 2)
            lights[0].on = True
            lights[0].brightness = 254
            lights[0].hue = 50000
            lights[0].saturation = 254
        except Exception:
            await ctx.send("Signal was not sent. Maybe Gondor needs to fix the beacons?")
    
async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Signal(bot))
