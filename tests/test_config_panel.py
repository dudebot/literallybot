"""Admin/superadmin slash panels are guild-only, bot-gated, and Manage-Messages-pinned.

Failure mode: `default_permissions(administrator=True)` makes Discord itself
refuse the interaction with native "Missing Permissions" for a bot admin
who is not a Discord Administrator. The picker pin is Manage Messages (a
typical moderator bit) so ordinary members do not see the command;
authorization remains `@app_commands.check(is_admin)` / `is_superadmin`.
`/help` still hides them from anyone who would fail that check.
"""
import logging

from cogs.core.control import Control
from core.utils import (GATE_ADMIN, GATE_SUPERADMIN, PANEL_SLASH_PERMISSIONS,
                        gate_of, is_admin, is_superadmin)


class _FakeBot:
    def __init__(self):
        self.logger = logging.getLogger("tests")


class TestSuperadminSlashVisibility:
    def _cmds(self):
        return {c.name: c for c in Control(_FakeBot()).get_app_commands()}

    def _assert_superadmin_slash_gated(self, cmd, name):
        assert cmd.guild_only is True, \
            f"/{name} in DMs is how a regular user found a superadmin panel"
        perms = cmd.default_permissions
        assert perms == PANEL_SLASH_PERMISSIONS, \
            f"/{name} picker pin is PANEL_SLASH_PERMISSIONS (Manage Messages)"
        assert not perms.administrator
        assert any(ch is is_superadmin for ch in (cmd.checks or [])), \
            f"/{name} must wrap is_superadmin, same predicate as !{name}"

    def test_config_hidden_from_dms_and_gated(self):
        self._assert_superadmin_slash_gated(self._cmds()["config"], "config")

    def test_cogs_hidden_from_dms_and_gated(self):
        self._assert_superadmin_slash_gated(self._cmds()["cogs"], "cogs")

    def test_prefix_and_slash_wrap_the_same_function(self):
        cog = Control(_FakeBot())
        prefix = {c.name: c for c in cog.get_commands()}["cogs"]
        slash = {c.name: c for c in cog.get_app_commands()}["cogs"]
        assert is_superadmin in prefix.checks
        assert is_superadmin in slash.checks


class TestAdminSlashVisibility:
    def _assert_admin_slash_gated(self, cmd, name):
        assert cmd.guild_only is True, \
            f"/{name} in DMs is how a regular user found an admin panel"
        perms = cmd.default_permissions
        assert perms == PANEL_SLASH_PERMISSIONS, \
            f"/{name} picker pin is PANEL_SLASH_PERMISSIONS (Manage Messages)"
        assert not perms.administrator
        assert any(ch is is_admin for ch in (cmd.checks or [])), \
            f"/{name} must wrap is_admin, same predicate as !{name}"

    def test_autoresponse_hidden_from_dms_and_gated(self):
        from cogs.optional.auto_response import AutoResponse
        cmds = {c.name: c for c in AutoResponse(_FakeBot()).get_app_commands()}
        self._assert_admin_slash_gated(cmds["autoresponse"], "autoresponse")

    def test_aisettings_hidden_from_dms_and_gated(self):
        from cogs.optional.gpt import Gpt
        bot = _FakeBot()
        bot.config = _FakeConfig()
        cmds = {c.name: c for c in Gpt(bot).get_app_commands()}
        self._assert_admin_slash_gated(cmds["aisettings"], "aisettings")

    def test_media_hidden_from_dms_and_gated(self):
        from cogs.optional.media import Media
        bot = _FakeBot()
        bot.config = _FakeConfig()
        cmds = {c.name: c for c in Media(bot).get_app_commands()}
        self._assert_admin_slash_gated(cmds["media"], "media")


class _FakeConfig:
    def get(self, *a, **k):
        return None

    def get_global(self, *a, **k):
        return None

    def get_user(self, *a, **k):
        return None


class TestPrefixGateStamps:
    """/help cannot execute a prefix check (it has an Interaction). The
    predicate itself must declare the tier, or every superadmin prefix
    command is listed to every guild admin."""

    def test_helpers_declare_their_tier(self):
        from core.utils import is_admin, is_superadmin
        assert gate_of(is_admin) == GATE_ADMIN
        assert gate_of(is_superadmin) == GATE_SUPERADMIN

    def test_control_prefix_commands_are_superadmin(self):
        from cogs.optional.help import _gate_tier
        cog = Control(_FakeBot())
        by_name = {c.name: c for c in cog.get_commands()}
        for name in ("cogs", "config", "restart", "sync", "enable", "disable",
                     "list_cogs"):
            assert _gate_tier(by_name[name]) == GATE_SUPERADMIN, name


class TestRoleSlashVisibility:
    def test_role_group_is_admin_gated(self):
        from cogs.optional.help import _checks_of, _gate_tier
        from cogs.optional.setrole import SetRole
        cmds = {c.name: c for c in SetRole(_FakeBot()).get_app_commands()}
        role = cmds["role"]
        assert role.guild_only is True
        perms = role.default_permissions
        assert perms is not None and perms.manage_roles, \
            "/role has no prefix twin; pin stays Manage Roles so visibility " \
            "is not a hard gate for a role-manager without Manage Messages"
        assert not perms.administrator
        assert not perms.manage_messages
        assert _gate_tier(role) == GATE_ADMIN
        assert any(gate_of(ch) == GATE_ADMIN for ch in _checks_of(role))


class TestHelpHidesAdminSlash:
    """The Discord picker pin is visibility for slash; /help is a second
    surface and must not list panels a non-admin cannot run."""

    def test_ordinary_member_does_not_see_aisettings_in_help(self):
        from cogs.optional.help import _slash_visible_to
        from cogs.optional.gpt import Gpt
        import asyncio

        bot = _FakeBot()
        bot.config = _FakeConfig()
        bot.user = type("B", (), {"id": 99})()
        cmd = {c.name: c for c in Gpt(bot).get_app_commands()}["aisettings"]

        class _Perms:
            administrator = False

        class _User:
            id = 1
            guild_permissions = _Perms()

        class _Ix:
            user = _User()
            client = bot
            guild = type("G", (), {"id": 5, "owner": None})()

        assert asyncio.run(_slash_visible_to(cmd, _Ix(), False, False)) is False


class TestSlashDenialCopy:
    """Expected CheckFailure must refuse with the gate's sentence, not
    'Something went wrong' (the production /aisettings + /cogs log noise)."""

    def test_aisettings_denial_says_requires_admin(self):
        from core.error_handler import app_command_denial_message
        from discord import app_commands
        from cogs.optional.gpt import Gpt

        bot = _FakeBot()
        bot.config = _FakeConfig()
        cmd = {c.name: c for c in Gpt(bot).get_app_commands()}["aisettings"]

        class _Ix:
            command = cmd
            user = type("U", (), {"id": 1})()

        msg = app_command_denial_message(_Ix(), app_commands.CheckFailure("x"))
        assert msg == "Requires admin."

    def test_cogs_denial_says_requires_superadmin(self):
        from core.error_handler import app_command_denial_message
        from discord import app_commands

        cmd = {c.name: c for c in Control(_FakeBot()).get_app_commands()}["cogs"]

        class _Ix:
            command = cmd
            user = type("U", (), {"id": 1})()

        msg = app_command_denial_message(_Ix(), app_commands.CheckFailure("x"))
        assert msg == "Requires superadmin."

    def test_dm_prefix_denial_is_a_server_command(self):
        from core.error_handler import prefix_denial_message
        from discord.ext import commands

        class _Ctx:
            command = type("C", (), {"checks": [is_admin]})()

        msg = prefix_denial_message(_Ctx(), commands.NoPrivateMessage())
        assert "server" in msg.lower()

    def test_slash_cooldown_is_not_a_gate_denial(self):
        from core.error_handler import _is_expected_app_denial
        from discord import app_commands
        err = app_commands.CommandOnCooldown(
            app_commands.Cooldown(1, 1.0), 1.0)
        assert _is_expected_app_denial(err) is False

    def test_expected_slash_denial_still_schedules_discord_log(self, monkeypatch):
        import asyncio
        from core import error_handler as eh
        from discord import app_commands

        scheduled = []

        async def fake_log(*a, **k):
            scheduled.append(k)

        async def fake_ack(*a, **k):
            return None

        monkeypatch.setattr(eh, "log_error_to_discord", fake_log)
        monkeypatch.setattr(eh, "_ack_app_error", fake_ack)

        bot = _FakeBot()
        cmd = {c.name: c for c in Control(_FakeBot()).get_app_commands()}["cogs"]

        class _Ix:
            command = cmd
            user = type("U", (), {"id": 1})()
            guild = None
            channel = None

        asyncio.run(eh.handle_app_command_error(
            bot, _Ix(), app_commands.CheckFailure("no")))
        assert len(scheduled) == 1
        assert scheduled[0]["severity"] == eh.ErrorSeverity.WARNING
