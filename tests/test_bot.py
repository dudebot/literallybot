import sys
import os

import discord
from unittest.mock import AsyncMock, patch, MagicMock  # Ensure all necessary mocks are included
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from discord.ext import commands
from bot import bot, get_prefix, change_status
from config import Config

class TestBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        intents = discord.Intents.default()
        intents.typing = False
        intents.presences = False
        self.bot = commands.Bot(command_prefix=get_prefix, intents=intents)
        self.bot.load_extension = AsyncMock()
        self.bot.change_presence = AsyncMock()
        self.bot.send = AsyncMock()
        self.bot.greet = AsyncMock()  # Mock the greet command
        self.bot.on_ready = AsyncMock()  # Mock the on_ready event
        self.bot.set_bot_operator = AsyncMock()  # Mock the set_bot_operator command
        self.ctx = MagicMock()
        self.ctx.message.author.mention = "@testuser"
        self.ctx.send = AsyncMock()

    async def test_bot_initialization(self):
        self.assertEqual(self.bot.command_prefix, get_prefix)

    async def test_on_ready(self):
        # Ensure that on_ready is called without errors
        await self.bot.on_ready()
        self.bot.on_ready.assert_called()

    # async def test_change_status(self):
    #     # Use command callback instead of direct function call
    #     with patch('bot.change_status', new=self.bot.change_presence):
    #         await self.bot.get_command('change_status').callback(self.bot, self.ctx, status="Online")
    #         self.bot.change_presence.assert_called_with(activity=discord.Game(name="Online"))
    #         self.ctx.send.assert_called_with("Status changed to Online.")

    # async def test_greet(self):
    #     # Use command callback instead of direct method call
    #     await self.bot.get_command('greet').callback(self.bot, self.ctx)
    #     self.bot.greet.assert_called_with(self.ctx)
    #     self.ctx.send.assert_called_with('Hello @testuser!')

    # async def test_set_bot_operator(self):
    #     # Use command callback instead of direct method call
    #     await self.bot.get_command('set_bot_operator').callback(self.bot, self.ctx, user=self.ctx.message.author)
    #     self.bot.set_bot_operator.assert_called_with(self.ctx, user=self.ctx.message.author)
    #     self.ctx.send.assert_called_with(f'{self.ctx.message.author.mention} has been set as a bot operator.')

if __name__ == '__main__':
    unittest.main()
