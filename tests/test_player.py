import sys
import os

import discord
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from unittest.mock import AsyncMock, MagicMock
from discord.ext import commands
from cogs.dynamic.player import MusicPlayer

class TestMusicPlayer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.music_cog = MusicPlayer(self.bot)
        self.bot.add_cog(self.music_cog)
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.author.voice = MagicMock()
        self.ctx.guild.voice_client = MagicMock()

    async def test_play(self):
        self.ctx.author.voice.channel.connect = AsyncMock()
        self.ctx.guild.voice_client.is_playing = MagicMock(return_value=False)
        self.ctx.guild.voice_client.play = AsyncMock()
        await self.music_cog.play(self.ctx, url="test_url")
        self.ctx.send.assert_called_with("Now playing: test_url")

    async def test_stop(self):
        await self.music_cog.stop(self.ctx)
        self.ctx.send.assert_called_with("Audio stream stopped.")

    async def test_pause(self):
        self.ctx.guild.voice_client.is_paused = MagicMock(return_value=False)
        await self.music_cog.pause(self.ctx)
        self.ctx.send.assert_called_with("Audio stream paused.")

    async def test_resume(self):
        self.ctx.guild.voice_client.is_playing = MagicMock(return_value=False)
        await self.music_cog.resume(self.ctx)
        self.ctx.send.assert_called_with("Audio stream resumed.")

    async def test_skip(self):
        self.music_cog.queue = ["test_url"]
        self.ctx.guild.voice_client.stop = AsyncMock()
        self.ctx.guild.voice_client.play = AsyncMock()
        await self.music_cog.skip(self.ctx)
        self.ctx.send.assert_called_with("Now playing: test_url")

    async def test_queue(self):
        self.music_cog.queue_list = ["test_url"]  # Updated attribute name
        await self.music_cog.queue(self.ctx)
        self.ctx.send.assert_called_with("Current queue:\ntest_url")

    async def test_volume(self):
        self.ctx.guild.voice_client.source = MagicMock()
        await self.music_cog.volume(self.ctx, volume=50)
        self.ctx.send.assert_called_with("Volume set to 50%")

    async def test_loop(self):
        self.ctx.guild.voice_client.loop = False
        await self.music_cog.loop(self.ctx)
        self.ctx.send.assert_called_with("Looping is now enabled.")

    async def test_shuffle(self):
        self.music_cog.queue = ["test_url1", "test_url2"]
        await self.music_cog.shuffle(self.ctx)
        self.ctx.send.assert_called_with("Queue shuffled.")

if __name__ == '__main__':
    unittest.main()
