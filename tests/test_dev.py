import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from discord.ext import commands
import discord
from cogs.static.dev import Dev
from config import Config

class TestDev(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.dev_cog = Dev(self.bot)
        self.bot.add_cog(self.dev_cog)
        self.bot.load_extension = AsyncMock()
        self.bot.unload_extension = AsyncMock()
        self.bot.reload_extension = AsyncMock()
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.message.delete = AsyncMock()

    async def test_on_ready(self):
        with patch('cogs.static.dev.sysv', new_callable=MagicMock) as mock_sysv:
            mock_sysv.major = 3
            mock_sysv.minor = 8
            mock_sysv.micro = 5
            with patch('cogs.static.dev.discord.__version__', '1.4.0a'):
                await self.dev_cog.on_ready()
                self.assertIn('Python 3.8.5 - Disord.py 1.4.0a', self.dev_cog.on_ready.__doc__)

    async def test_reload_all(self):
        await self.dev_cog.reload_all(self.ctx)
        self.ctx.send.assert_called_with('Reloading...')
        self.ctx.message.delete.assert_called()
        self.bot.reload_extension.assert_called()

    async def test_load_cog(self):
        await self.dev_cog.load_cog(self.ctx, cog='test_cog')
        self.ctx.send.assert_called_with('Loading...')
        self.ctx.message.delete.assert_called()
        self.bot.load_extension.assert_called_with('cogs.test_cog')

    async def test_unload_cog(self):
        await self.dev_cog.unload_cog(self.ctx, cog='test_cog')
        self.ctx.send.assert_called_with('Unloading...')
        self.ctx.message.delete.assert_called()
        self.bot.unload_extension.assert_called_with('cogs.test_cog')

    async def test_reload_cog(self):
        await self.dev_cog.reload_cog(self.ctx, cog='test_cog')
        self.ctx.send.assert_called_with('Reloading...')
        self.ctx.message.delete.assert_called()
        self.bot.reload_extension.assert_called_with('cogs.test_cog')

    async def test_set_bot_operator(self):
        config = Config(self.ctx.guild.id)
        config.add_bot_operator = AsyncMock()
        with patch('cogs.static.dev.Config', return_value=config):
            await self.dev_cog.set_bot_operator(self.ctx, user=self.ctx.message.author)
            config.add_bot_operator.assert_called_with(self.ctx.message.author.id)
            self.ctx.send.assert_called_with(f'{self.ctx.message.author.mention} has been set as a bot operator.')

if __name__ == '__main__':
    unittest.main()
