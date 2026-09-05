"""User-scope on the real Config class.

The points commit (e1a4a68) deleted get_user/set_user while adding a
points store that calls them. Every existing test faked Config, so the
AttributeError never showed up. These hit core.config.Config.
"""
import os

from core.config import Config
from utils.points import add_points, get_points


class _User:
    def __init__(self, user_id):
        self.id = user_id


def _config(tmp_path):
    return Config(config_dir=str(tmp_path / "configs"))


def test_get_user_returns_default_without_creating_a_file(tmp_path):
    """A DM from someone never seen must not spawn user_<id>.json."""
    config = _config(tmp_path)
    try:
        user = _User(1034209140886749225)
        assert config.get_user(user, "points", 0) == 0
        assert config.has_user(user, "points") is False
        files = os.listdir(config.config_dir)
        assert "user_1034209140886749225.json" not in files
    finally:
        config.shutdown()


def test_set_user_writes_user_id_json_for_an_object_and_a_bare_int(tmp_path):
    config = _config(tmp_path)
    try:
        config.set_user(_User(7), "points", 10)
        config.set_user(8, "points", 20)
        config.flush()

        assert config.get_user(_User(7), "points") == 10
        assert config.get_user(8, "points") == 20
        assert os.path.isfile(os.path.join(config.config_dir, "user_7.json"))
        assert os.path.isfile(os.path.join(config.config_dir, "user_8.json"))
    finally:
        config.shutdown()


def test_rem_user_and_has_user(tmp_path):
    config = _config(tmp_path)
    try:
        user = _User(9)
        config.set_user(user, "points", 5)
        assert config.has_user(user, "points") is True
        assert config.rem_user(user, "points") is True
        assert config.has_user(user, "points") is False
        assert config.get_user(user, "points", 0) == 0
        assert config.rem_user(user, "points") is False
    finally:
        config.shutdown()


def test_unresolvable_user_context_raises(tmp_path):
    config = _config(tmp_path)
    try:
        try:
            config.get_user("not-a-user", "points")
        except ValueError as err:
            assert "Cannot resolve user context" in str(err)
        else:
            raise AssertionError("expected ValueError")
    finally:
        config.shutdown()


def test_points_helpers_use_real_user_scope(tmp_path):
    """utils.points is the caller the deleting commit itself introduced."""
    class _Bot:
        def __init__(self, config):
            self.config = config

    config = _config(tmp_path)
    try:
        bot, user = _Bot(config), _User(11)
        assert get_points(bot, user) == 0
        assert add_points(bot, user, 4) == 4
        config.flush()
        assert get_points(bot, user) == 4
        assert os.path.isfile(os.path.join(config.config_dir, "user_11.json"))
    finally:
        config.shutdown()
