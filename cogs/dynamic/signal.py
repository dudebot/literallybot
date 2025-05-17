import random
import time
from discord.ext import commands
from phue import Bridge, PhueRegistrationException
import os


class Signal(commands.Cog):
    """Cog to interact with Philips Hue lights."""
    def __init__(self, bot):
        self.bot = bot
        self.bridge = None

    def connect_to_bridge(self):
        # retrieve global hue_bridge_ip from bot.config
        ip = self.bot.config.get(None, "hue_bridge_ip")
        self.bridge = Bridge(ip)
        self.bridge.connect()
        self.lights = self.bridge.lights
        
    def flash_lights(self, flash_times, delay):
        if not self.bridge:
            self.connect_to_bridge()
        
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

    @commands.command(name='annoydudebot', hidden=True)
    async def annoydudebot(self, ctx):
        if ctx.author.id != 219309814856024064:
            return
        self.flash_lights(3, 3)
        await ctx.send("signal sent")
        
    @commands.command(name='sethuebridgeip', hidden=True)
    async def sethuebridgeip(self, ctx, ip: str):
        # store global hue_bridge_ip
        self.bot.config.set(None, "hue_bridge_ip", ip)
        try:
            self.connect_to_bridge()
        except PhueRegistrationException as e:
            await ctx.send(f"Failed to connect to the Hue bridge: {str(e)}")
            return
        await ctx.send(f"Hue bridge IP updated to {ip}")

async def setup(bot):
    await bot.add_cog(Signal(bot))