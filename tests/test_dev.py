import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from discord.ext import commands
import discord
from cogs.static.dev import Dev

class TestDev(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.dev_cog = Dev(self.bot)
        await self.bot.add_cog(self.dev_cog)
        self.bot.load_extension = AsyncMock()
        self.bot.unload_extension = AsyncMock()
        self.bot.reload_extension = AsyncMock()
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.message.delete = AsyncMock()
        self.ctx.author = MagicMock()
        self.ctx.guild = MagicMock()

    # async def test_on_ready(self):
    #     with patch('cogs.static.dev.sysv', new_callable=MagicMock) as mock_sysv:
    #         mock_sysv.major = 3
    #         mock_sysv.minor = 8
    #         mock_sysv.micro = 5
    #         with patch('cogs.static.dev.discord.__version__', '1.4.0a'):
    #             self.dev_cog.on_ready.__doc__ = 'Python 3.8.5 - Disord.py 1.4.0a'
    #             await self.dev_cog.on_ready()
    #             self.assertIn('Python 3.8.5 - Disord.py 1.4.0a', self.dev_cog.on_ready.__doc__)

    # async def test_reload_all(self):
    #     await self.bot.get_command('reload_all').callback(self.dev_cog, self.ctx)
    #     self.ctx.send.assert_called_with('Reloading...')
    #     self.ctx.message.delete.assert_called()
    #     self.bot.reload_extension.assert_called()


    # async def test_update_code(self):
    #     with patch('cogs.static.dev.subprocess.run') as mock_run:
    #         mock_run.return_value.returncode = 0
    #         mock_run.return_value.stdout = 'Updated successfully'
    #         mock_run.return_value.stderr = ''
    #         await self.bot.get_command('update').callback(self.dev_cog, self.ctx)
    #         self.ctx.send.assert_called_with('Updating code...')
    #         self.ctx.message.delete.assert_called()
    #         mock_run.assert_called_with(['git', 'pull'], capture_output=True, text=True)
    #         self.ctx.send.assert_called_with('Code updated successfully:\nUpdated successfully', delete_after=20)

    # async def test_set_bot_operator(self):
    #     config = Config(self.ctx.guild.id)
    #     config.add_bot_operator = AsyncMock()
    #     with patch('cogs.static.dev.Config', return_value=config):
    #         await self.bot.get_command('set_bot_operator').callback(self.dev_cog, self.ctx, user=self.ctx.message.author)
    #         config.add_bot_operator.assert_called_with(self.ctx.message.author.id)
    #         self.ctx.send.assert_called_with(f'{self.ctx.message.author.mention} has been set as a bot operator.')

if __name__ == '__main__':
    unittest.main()
