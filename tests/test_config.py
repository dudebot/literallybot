"""Persistence failure and file-mode checks against real temporary files."""
import json
import os
import stat
from pathlib import Path

import pytest



def disk(config, name='global'):
    return json.loads((Path(config.config_dir) / f'{name}.json').read_text())


def test_mutating_a_loaded_list_then_setting_it_persists(config, config_factory):
    config.set(7, 'admins', [1])
    config.flush()
    reopened = config_factory(Path(config.config_dir))
    admins = reopened.get(7, 'admins')
    admins.append(2)
    # get returns a reference: equality-based "unchanged" checks in set lose this write.
    reopened.set(7, 'admins', admins)
    reopened.flush()
    assert disk(reopened, '7') == {'admins': [1, 2]}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX atomic replacement path')
def test_failed_replace_preserves_disk_and_pending_write(config, monkeypatch):
    config.set_global('value', 'old')
    config.flush()
    config.set_global('value', 'new')
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError('disk failure')
        patch.setattr('core.config.os.rename', fail)
        with pytest.raises(OSError, match='disk failure'):
            config.flush()
    assert disk(config) == {'value': 'old'}
    assert not list(Path(config.config_dir).glob('*.tmp'))
    config.flush()
    assert disk(config) == {'value': 'new'}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
def test_secret_modes_are_tightened_before_writing_even_a_stale_temp_file(
        tmp_path, config_factory, monkeypatch):
    directory = tmp_path / 'configs'
    directory.mkdir(mode=0o755)
    config = config_factory(directory)
    stale = directory / 'global.json.tmp'
    stale.write_text('{}')
    stale.chmod(0o644)
    real_dump = json.dump
    observed = []

    def inspect_write(value, stream, **kwargs):
        observed.append(stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
        return real_dump(value, stream, **kwargs)

    monkeypatch.setattr('core.config.json.dump', inspect_write)
    config.set_global('discord_token', 'test-secret')
    config.flush()
    assert observed == [0o600]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / 'global.json').stat().st_mode) == 0o600
    assert not stale.exists()
