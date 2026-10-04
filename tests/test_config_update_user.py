"""Related values must persist together or remain unchanged on failure."""
import json
from pathlib import Path
import pytest


def test_atomic_user_update_commit_and_failures(config, monkeypatch):
    config.set_user(123, 'balance', 20)
    def reserve(doc):
        doc['balance'] -= 10
        doc['reservation'] = 'pending'
        return doc['balance']
    assert config.update_user(123, reserve) == 10
    path = Path(config.config_dir) / 'user_123.json'
    saved = json.loads(path.read_text())
    assert saved == {'balance': 10, 'reservation': 'pending'}
    def invalid(doc):
        doc['balance'] = 0
        raise ValueError('invalid transaction')
    with pytest.raises(ValueError):
        config.update_user(123, invalid)
    assert config.get_user(123, 'balance') == 10
    def failed_write(*args):
        raise OSError('disk unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(config, '_immediate_save', failed_write)
        with pytest.raises(OSError):
            config.update_user(123, reserve)
    assert config.get_user(123, 'balance') == 10
    assert json.loads(path.read_text()) == saved
