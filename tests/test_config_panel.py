"""Admin/superadmin slash panels must not appear in Discord's picker.

Failure mode: Discord's slash picker showing /cogs, /config, /aisettings
or /autoresponse to ordinary users (including in DMs). The command must
be guild-only + Administrator so the client hides it;
`@app_commands.check(is_admin)` / `is_superadmin` is still the real gate.
"""
import logging

from cogs.core.control import Control
from core.utils import GATE_ADMIN, GATE_SUPERADMIN, gate_of, is_admin, is_superadmin


class _FakeBot:
    def __init__(self):
        self.logger = logging.getLogger("tests")


class TestSuperadminSlashVisibility:
    def _cmds(self):
        return {c.name: c for c in Control(_FakeBot()).get_app_commands()}

    def _assert_superadmin_picker_hidden(self, cmd, name):
        assert cmd.guild_only is True, \
            f"/{name} in DMs is how a regular user found a superadmin panel"
        perms = cmd.default_permissions
        assert perms is not None and perms.administrator, \
            f"without Administrator, Discord shows /{name} to every member"
        assert any(ch is is_superadmin for ch in (cmd.checks or [])), \
            f"/{name} must wrap is_superadmin, same predicate as !{name}"

    def test_config_hidden_from_dms_and_non_admins(self):
        self._assert_superadmin_picker_hidden(self._cmds()["config"], "config")

    def test_cogs_hidden_from_dms_and_non_admins(self):
        self._assert_superadmin_picker_hidden(self._cmds()["cogs"], "cogs")

    def test_prefix_and_slash_wrap_the_same_function(self):
        cog = Control(_FakeBot())
        prefix = {c.name: c for c in cog.get_commands()}["cogs"]
        slash = {c.name: c for c in cog.get_app_commands()}["cogs"]
        assert is_superadmin in prefix.checks
        assert is_superadmin in slash.checks


class TestAdminSlashVisibility:
    def _assert_admin_picker_hidden(self, cmd, name):
        assert cmd.guild_only is True, \
            f"/{name} in DMs is how a regular user found an admin panel"
        perms = cmd.default_permissions
        assert perms is not None and perms.administrator, \
            f"without Administrator, Discord shows /{name} to every member"
        assert any(ch is is_admin for ch in (cmd.checks or [])), \
            f"/{name} must wrap is_admin, same predicate as !{name}"

    def test_autoresponse_hidden_from_dms_and_non_admins(self):
        from cogs.optional.auto_response import AutoResponse
        cmds = {c.name: c for c in AutoResponse(_FakeBot()).get_app_commands()}
        self._assert_admin_picker_hidden(cmds["autoresponse"], "autoresponse")

    def test_aisettings_hidden_from_dms_and_non_admins(self):
        from cogs.optional.gpt import Gpt
        bot = _FakeBot()
        bot.config = _FakeConfig()
        cmds = {c.name: c for c in Gpt(bot).get_app_commands()}
        self._assert_admin_picker_hidden(cmds["aisettings"], "aisettings")


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
        assert perms is not None and perms.manage_roles
        assert _gate_tier(role) == GATE_ADMIN
        assert any(gate_of(ch) == GATE_ADMIN for ch in _checks_of(role))
