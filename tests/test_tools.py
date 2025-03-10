import sys
import os

import discord
from unittest.mock import AsyncMock, MagicMock, PropertyMock  # Keep PropertyMock
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from discord.ext import commands
from cogs.dynamic.tools import Tools

class TestTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.tools_cog = Tools(self.bot)
        await self.bot.add_cog(self.tools_cog)  # Use await for add_cog
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.message.delete = AsyncMock()

    async def test_echo(self):
        await self.bot.get_command('echo').callback(self.tools_cog, self.ctx, message="Hello, World!")
        self.ctx.message.delete.assert_called()
        self.ctx.send.assert_called_with("Hello, World!")

    async def test_ping(self):
        type(self.bot).latency = PropertyMock(return_value=0.123)
        await self.bot.get_command('ping').callback(self.tools_cog, self.ctx)
        self.ctx.send.assert_called_with("🏓 123 ms.")

    async def test_get_info(self):
        await self.bot.get_command('info').callback(self.tools_cog, self.ctx)
        self.ctx.send.assert_called()
        embed = self.ctx.send.call_args[1]['embed']
        self.assertEqual(embed.title, 'Info')
        self.assertEqual(embed.description, 'An info message using an embed!')
        self.assertEqual(embed.fields[0].name, 'Version')
        self.assertEqual(embed.fields[0].value, '0.1')
        self.assertEqual(embed.fields[1].name, 'Language')
        self.assertEqual(embed.fields[1].value, 'Python 3.8')

if __name__ == '__main__':
    unittest.main()
