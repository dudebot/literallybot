import sys
import os

import discord
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from unittest.mock import AsyncMock, MagicMock
from discord.ext import commands
from cogs.dynamic.quote import Quote

class TestQuote(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.quote_cog = Quote(self.bot)
        self.bot.add_cog(self.quote_cog)
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()

    async def test_refresh_quote(self):
        self.quote_cog.get_quote = AsyncMock(return_value=("Test quote", "Test author"))
        await self.quote_cog.refresh_quote()
        self.assertEqual(self.quote_cog.qod, "Test quote")
        self.assertEqual(self.quote_cog.qod_auth, "Test author")

    async def test_get_quote(self):
        self.quote_cog.get_quote = AsyncMock(return_value=("Test quote", "Test author"))
        quote, author = await self.quote_cog.get_quote()
        self.assertEqual(quote, "Test quote")
        self.assertEqual(author, "Test author")

    async def test_quote(self):
        self.quote_cog.qod = "Test quote"
        self.quote_cog.qod_auth = "Test author"
        await self.quote_cog.quote(self.ctx, "Your test quote")
        self.ctx.send.assert_called_with("Expected response")

if __name__ == '__main__':
    unittest.main()
