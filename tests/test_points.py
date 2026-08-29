"""utils.points clamps at zero so a subtract cannot store a negative balance."""


class _User:
    def __init__(self, user_id=1):
        self.id = user_id


class _Config:
    def __init__(self):
        self.users = {}

    def get_user(self, user, key, default=None):
        return self.users.get(getattr(user, "id", user), {}).get(key, default)

    def set_user(self, user, key, value):
        self.users.setdefault(getattr(user, "id", user), {})[key] = value


class _Bot:
    def __init__(self):
        self.config = _Config()


def test_get_points_defaults_to_zero():
    from utils.points import get_points
    assert get_points(_Bot(), _User()) == 0


def test_add_points_clamps_at_zero():
    from utils.points import add_points, get_points
    bot, user = _Bot(), _User()
    add_points(bot, user, 5)
    assert add_points(bot, user, -20) == 0
    assert get_points(bot, user) == 0


def test_set_points_clamps_at_zero():
    from utils.points import set_points, get_points
    bot, user = _Bot(), _User()
    assert set_points(bot, user, -3) == 0
    assert get_points(bot, user) == 0


def test_points_ops_register_from_the_module_not_the_cog():
    """The store is the primitive; the Points cog is only UI. Tools must
    exist even if that cog is in disabled_cogs."""
    from core.ops import OpsRegistry
    from utils import points as points_mod
    reg = OpsRegistry()
    names = set(reg.register_module_ops(points_mod))
    assert names == {"get_user_points", "add_user_points", "set_user_points"}
    assert reg.label_for("points") == "Points"
    for name in names:
        op = reg.require(name)
        assert op.origin == "cog"
        assert op.owner is points_mod
        assert op.default_gate() == "off"
        assert op.group == "points"
