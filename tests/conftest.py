"""Real config stores in temporary directories; drive timer callbacks explicitly."""
import pytest

from core.config import Config


class ManualTimer:
    def __init__(self, interval, function):
        self.function = function
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    monkeypatch.setattr('core.config.Timer', ManualTimer)
    stores = []

    def create(path=None):
        store = Config(str(path or tmp_path / 'configs'))
        stores.append(store)
        return store

    yield create
    for store in stores:
        store.shutdown()


@pytest.fixture
def config(config_factory):
    return config_factory()
