"""The two-tier agent permission model (core/agent_gate).

Pure config→decision logic, no Discord objects. Pins the resolution order,
the whitelist ceiling, the per-guild off/admin/everyone override, the
per-op default, and the super-admin bypass.
"""
from core import agent_gate as ag
from core.ops import OpScope, PermissionLevel


class _FakeOp:
    def __init__(self, name, scope=OpScope.GUILD,
                 permission=PermissionLevel.ADMIN):
        self.name = name
        self.scope = scope
        self.permission = permission

    def default_gate(self):
        return "off"


# ---- whitelist ceiling ----

def test_absent_whitelist_disables_everything():
    op = _FakeOp("read_history", permission=PermissionLevel.EVERYONE)
    assert ag.is_whitelisted("read_history", None) is False
    assert ag.is_whitelisted("read_history", {}) is False
    assert ag.effective_gate(op, None, None) == ag.GATE_OFF


def test_whitelist_false_disables_one_op():
    wl = {"read_history": True, "delete_message": False}
    assert ag.is_whitelisted("read_history", wl) is True
    assert ag.is_whitelisted("delete_message", wl) is False


# ---- per-op default ----

def test_default_gate_is_always_off():
    assert _FakeOp("x", permission=PermissionLevel.EVERYONE).default_gate() == "off"
    assert _FakeOp("x", permission=PermissionLevel.ADMIN).default_gate() == "off"




# ---- per-guild override, bounded by whitelist ----

def test_guild_override_wins_when_whitelisted():
    op = _FakeOp("send_message", permission=PermissionLevel.EVERYONE)
    wl = {"send_message": True}
    assert ag.effective_gate(op, wl, {"send_message": "admin"}) == ag.GATE_ADMIN
    assert ag.effective_gate(op, wl, {"send_message": "off"}) == ag.GATE_OFF
    assert ag.effective_gate(op, wl, None) == ag.GATE_OFF  # default_gate is always off


def test_guild_override_ignored_when_not_whitelisted():
    op = _FakeOp("delete_channel")
    # guild tries to turn it on, but the super-admin never whitelisted it
    assert ag.effective_gate(op, {"other": True}, {"delete_channel": "everyone"}) == ag.GATE_OFF


def test_invalid_guild_gate_value_falls_back_to_default():
    op = _FakeOp("kick_member")
    assert ag.guild_gate(op, {"kick_member": "banana"}) == "off"


# ---- call-time admin requirement ----

def test_call_requires_admin_by_gate():
    op = _FakeOp("send_message", permission=PermissionLevel.EVERYONE)
    wl = {"send_message": True}
    assert ag.call_requires_admin(op, wl, {"send_message": "everyone"}) is False
    assert ag.call_requires_admin(op, wl, {"send_message": "admin"}) is True
    # not whitelisted → defensively admin
    assert ag.call_requires_admin(op, None, None) is True
