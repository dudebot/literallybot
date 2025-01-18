import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from discord.ext import commands, tasks
import discord
import os
from bot import bot, get_prefix, change_status
from config import Config

class TestBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = bot
        self.bot.load_extension = AsyncMock()
        self.bot.change_presence = AsyncMock()
        self.bot.send = AsyncMock()
        self.ctx = MagicMock()
        self.ctx.message.author.mention = "@testuser"
        self.ctx.send = AsyncMock()

    async def test_bot_initialization(self):
        self.assertEqual(self.bot.command_prefix, get_prefix)
        self.assertEqual(self.bot.description, 'A simple example of bot made with Discord.py')

    async def test_on_ready(self):
        with patch('bot.load_cogs', new_callable=AsyncMock):
            await self.bot.on_ready()
            self.bot.load_extension.assert_called()
            self.bot.change_presence.assert_called()

    async def test_change_status(self):
        await change_status()
        self.bot.change_presence.assert_called()

    async def test_greet(self):
        await self.bot.greet(self.ctx)
        self.ctx.send.assert_called_with('Hello @testuser!')

    async def test_set_bot_operator(self):
        config = Config(self.ctx.guild.id)
        config.add_bot_operator = AsyncMock()
        with patch('bot.Config', return_value=config):
            await self.bot.set_bot_operator(self.ctx, user=self.ctx.message.author)
            config.add_bot_operator.assert_called_with(self.ctx.message.author.id)
            self.ctx.send.assert_called_with(f'{self.ctx.message.author.mention} has been set as a bot operator.')

if __name__ == '__main__':
    unittest.main()
