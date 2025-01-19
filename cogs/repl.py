import asyncio
import discord
from discord.ext import commands

class REPL(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.server = None
        self.channel = None
        self.repl_task = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self.repl_task is None:
            self.repl_task = self.bot.loop.create_task(self.repl())

    async def repl(self):
        while True:
            command = input("REPL> ").strip()
            if command == "exit":
                self.server = None
                self.channel = None
                print("Exited REPL mode.")
            elif command.startswith("server"):
                await self.select_server(command)
            elif command.startswith("channel"):
                await self.select_channel(command)
            elif command.startswith("send"):
                await self.send_message(command)
            else:
                print("Unknown command. Available commands: server, channel, send, exit")

    async def select_server(self, command):
        server_name = command[len("server"):].strip()
        servers = [guild for guild in self.bot.guilds if server_name.lower() in guild.name.lower()]
        if len(servers) == 1:
            self.server = servers[0]
            print(f"Selected server: {self.server.name}")
        elif len(servers) > 1:
            print("Multiple servers found:")
            for i, server in enumerate(servers, 1):
                print(f"{i}. {server.name}")
            choice = int(input("Select server number: "))
            self.server = servers[choice - 1]
            print(f"Selected server: {self.server.name}")
        else:
            print("No server found with that name.")

    async def select_channel(self, command):
        if self.server is None:
            print("No server selected. Use 'server' command first.")
            return
        channel_name = command[len("channel"):].strip()
        channels = [channel for channel in self.server.channels if channel_name.lower() in channel.name.lower()]
        if len(channels) == 1:
            self.channel = channels[0]
            print(f"Selected channel: {self.channel.name}")
        elif len(channels) > 1:
            print("Multiple channels found:")
            for i, channel in enumerate(channels, 1):
                print(f"{i}. {channel.name}")
            choice = int(input("Select channel number: "))
            self.channel = channels[choice - 1]
            print(f"Selected channel: {self.channel.name}")
        else:
            print("No channel found with that name.")

    async def send_message(self, command):
        if self.channel is None:
            print("No channel selected. Use 'channel' command first.")
            return
        message = command[len("send"):].strip()
        await self.channel.send(message)
        print(f"Sent message: {message}")

async def setup(bot):
    await bot.add_cog(REPL(bot))
