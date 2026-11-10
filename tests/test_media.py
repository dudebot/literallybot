"""Media filenames must not write or delete files outside the library."""
from types import SimpleNamespace as NS

import pytest

from cogs.media import Media


@pytest.fixture
def library(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    cog = Media(NS(config=config))
    guild = NS(id=17, name='Example')
    directory = tmp_path / 'media' / '17'
    directory.mkdir(parents=True)
    yield cog, guild, directory
    cog.cog_unload()


def test_unsafe_names_cannot_download_or_resolve_other_files(library, tmp_path):
    cog, guild, directory = library
    outside = tmp_path / 'keep.mp3'
    outside.write_bytes(b'outside')
    own = directory / 'keep.mp3'
    own.write_bytes(b'library')
    for name in ('../keep', '/keep', r'..\keep', '%(title)s'):
        result = cog._do_addmedia(guild, 'http://invalid.test/file.mp3', name, None, None)
        assert 'Filename must' in result
        assert cog._find_file(guild, name) is None
    assert outside.read_bytes() == b'outside'
    assert own.read_bytes() == b'library'
