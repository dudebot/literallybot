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
