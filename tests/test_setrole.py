import sys
import os

import discord
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands
from cogs.dynamic.setrole import SetRole
from config import Config

class TestSetRole(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.setrole_cog = SetRole(self.bot)
        await self.bot.add_cog(self.setrole_cog)  # Await add_cog
        self.ctx = MagicMock()
        self.ctx.send = AsyncMock()
        self.ctx.author.add_roles = AsyncMock()
        self.ctx.author.remove_roles = AsyncMock()
        self.ctx.guild.roles = [MagicMock(name="TestRole")]

    async def test_setrole_add(self):
        config = Config(self.ctx.guild.id)
        config.config["whitelist_roles"] = ["TestRole"]
        with patch('cogs.dynamic.setrole.Config', return_value=config):
            await self.setrole_cog.setrole(self.ctx, action="+", rolename="TestRole")
            self.ctx.author.add_roles.assert_called()
            self.ctx.send.assert_called_with("Added to role: TestRole")

    async def test_setrole_remove(self):
        config = Config(self.ctx.guild.id)
        config.config["whitelist_roles"] = ["TestRole"]
        with patch('cogs.dynamic.setrole.Config', return_value=config):
            await self.setrole_cog.setrole(self.ctx, action="-", rolename="TestRole")
            self.ctx.author.remove_roles.assert_called()
            self.ctx.send.assert_called_with("Removed from role: TestRole")

    async def test_setrole_invalid_action(self):
        # Use command callback instead of direct method call
        await self.bot.get_command('setrole').callback(self.setrole_cog, self.ctx, action="*", rolename="TestRole")
        self.ctx.send.assert_called_with("Use a + or - to add or remove the role (eg: !setrole +Kinography)")

    async def test_setrole_role_not_in_whitelist(self):
        config = Config(self.ctx.guild.id)
        config.config["whitelist_roles"] = ["AnotherRole"]
        with patch('cogs.dynamic.setrole.Config', return_value=config):
            await self.setrole_cog.setrole(self.ctx, action="+", rolename="TestRole")
            self.ctx.send.assert_called_with("Role not in whitelist: TestRole")

    async def test_setrole_role_not_found(self):
        config = Config(self.ctx.guild.id)
        config.config["whitelist_roles"] = ["TestRole"]
        self.ctx.guild.roles = []
        with patch('cogs.dynamic.setrole.Config', return_value=config):
            await self.setrole_cog.setrole(self.ctx, action="+", rolename="TestRole")
            self.ctx.send.assert_called_with("Could not find role: TestRole")

if __name__ == '__main__':
    unittest.main()
