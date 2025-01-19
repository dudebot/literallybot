import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from unittest.mock import AsyncMock, MagicMock
from discord.ext import commands
from cogs.tools import Tools

class TestTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!")
        self.tools_cog = Tools(self.bot)
        self.bot.add_cog(self.tools_cog)
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.message.delete = AsyncMock()

    async def test_echo(self):
        await self.tools_cog.echo(self.ctx, message="Hello, World!")
        self.ctx.message.delete.assert_called()
        self.ctx.send.assert_called_with("Hello, World!")

    async def test_ping(self):
        self.bot.latency = 0.123
        await self.tools_cog.ping(self.ctx)
        self.ctx.send.assert_called_with("🏓 123 ms.")

    async def test_get_info(self):
        await self.tools_cog.get_info(self.ctx)
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
