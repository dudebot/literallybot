"""Persistence failures that can lose settings, cross scopes, or expose secrets."""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from utils.points import add_points, get_points, set_points


def disk(config, name='global'):
    return json.loads((Path(config.config_dir) / f'{name}.json').read_text())


def test_missing_reads_do_not_persist_defaults(config):
    before = {p.name: p.read_bytes() for p in Path(config.config_dir).iterdir()}
    assert config.get(7, 'admins', []) == []
    assert config.get_user(NS(id=7), 'points', 0) == 0
    assert config.get_global('missing', False) is False
    assert not config.has(7, 'admins')
    assert not config.has_user(7, 'points')
    assert not config.has_global('missing')
    config.flush()
    assert {p.name: p.read_bytes() for p in Path(config.config_dir).iterdir()} == before


def test_load_and_roundtrip_isolate_guild_user_and_global(config, config_factory):
    config.set(NS(guild=NS(id=7)), 'value', 'guild')
    config.set_user(NS(id=7), 'value', 'user')
    config.set_global('value', 'global')
    config.flush()
    reopened = config_factory(Path(config.config_dir))
    assert reopened.get(7, 'value') == 'guild'
    assert reopened.get_user(7, 'value') == 'user'
    assert reopened.get(NS(guild=None), 'value') == 'global'
    assert reopened.get_global('value') == 'global'
    assert reopened.guild_ids() == [7]


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


def test_removals_survive_restart_without_dropping_other_keys(config, config_factory):
    config.set(7, 'keep', {'nested': [1]})
    config.set(7, 'remove', True)
    config.set_user(7, 'remove', True)
    config.set_global('remove', True)
    config.flush()
    assert config.rem(7, 'remove')
    assert config.rem_user(7, 'remove')
    assert config.rem_global('remove')
    config.flush()
    reopened = config_factory(Path(config.config_dir))
    assert reopened.get(7, 'keep') == {'nested': [1]}
    assert not reopened.has(7, 'remove')
    assert not reopened.has_user(7, 'remove')
    assert not reopened.has_global('remove')


def test_external_file_wins_over_dirty_memory_including_deleted_keys(config):
    config.set(7, 'old', 1)
    config.flush()
    path = Path(config.config_dir) / '7.json'
    modified = path.stat().st_mtime + 10
    config.set(7, 'unsaved', 2)
    path.write_text('{"external": 3}')
    os.utime(path, (modified, modified))
    (path.parent / '8.json').write_text('{"new": 4}')
    config._reload_timer.function()
    assert config.get(7, 'old') is None
    assert config.get(7, 'unsaved') is None
    assert config.get(7, 'external') == 3
    assert config.get(8, 'new') == 4
    config.flush()
    assert disk(config, '7') == {'external': 3}


def test_partial_external_json_preserves_memory_and_retries(config):
    config.set(7, 'value', 'known good')
    config.flush()
    path = Path(config.config_dir) / '7.json'
    modified = path.stat().st_mtime + 10
    path.write_text('{"value":')
    os.utime(path, (modified, modified))
    config._reload_timer.function()
    assert config.get(7, 'value') == 'known good'
    path.write_text('{"value": "repaired"}')
    os.utime(path, (modified, modified))
    config._reload_timer.function()
    assert config.get(7, 'value') == 'repaired'


def test_corrupt_startup_file_is_not_silently_overwritten(tmp_path, config_factory):
    path = tmp_path / 'global.json'
    path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        config_factory(tmp_path)
    assert path.read_text() == '{broken'


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


def test_shutdown_flushes_buffered_writes_and_cancels_timers(config):
    config.set_global('value', 1)
    assert 'value' not in disk(config)
    save, reload = config._save_timer, config._reload_timer
    config.shutdown()
    assert disk(config) == {'value': 1}
    assert save.cancelled and reload.cancelled


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


def test_points_use_real_user_store_and_cannot_persist_negative_balances(config):
    bot, user = NS(config=config), NS(id=7)
    assert get_points(bot, user) == 0
    assert add_points(bot, user, 4) == 4
    config.flush()
    assert disk(config, 'user_7') == {'points': 4}
    assert add_points(bot, user, -20) == 0
    assert set_points(bot, user, -3) == 0
    config.flush()
    assert disk(config, 'user_7') == {'points': 0}
    assert config.get(7, 'points') is None
