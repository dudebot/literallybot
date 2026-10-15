"""Media names must not escape the library or expose host files."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace as NS

import pytest

from cogs.optional.media import Media, MediaView


@pytest.fixture
def library(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    cog = Media(NS(config=config))
    guild = NS(id=17, name='Example')
    directory = tmp_path / 'media' / '17'
    directory.mkdir(parents=True)
    yield cog, guild, directory
    cog.cog_unload()


def test_unsafe_names_cannot_download_or_cleanup_other_files(library, tmp_path):
    cog, guild, directory = library
    outside = tmp_path / 'keep.mp3'
    outside.write_bytes(b'outside')
    own = directory / 'keep.mp3'
    own.write_bytes(b'library')
    for name in ('../keep', '/keep', r'..\keep', '*', 'ke?p', '[keep]',
                 '%(title)s', 'ke\x00ep', '..'):
        result = cog._do_addmedia(guild, 'http://invalid.test/file.mp3', name, None, None)
        assert 'Filename must' in result
        with pytest.raises(ValueError):
            cog._cleanup_media_files(directory, name)
        assert cog._find_file(guild, name) is None
    assert outside.read_bytes() == b'outside'
    assert own.read_bytes() == b'library'
    (directory / 'keep_tmp.mp3').write_bytes(b'temporary')
    (directory / 'keeper.mp3').write_bytes(b'other')
    cog._cleanup_media_files(directory, 'keep')
    assert sorted(p.name for p in directory.iterdir()) == ['keeper.mp3']
    assert outside.read_bytes() == b'outside'


@pytest.mark.asyncio
async def test_links_are_hidden_and_delete_rechecks_a_stale_selection(library, tmp_path):
    cog, guild, directory = library
    outside = tmp_path / 'private.mp3'
    outside.write_bytes(b'private')
    (directory / 'leak.mp3').symlink_to(outside)
    (directory / 'folder').mkdir()
    good = directory / 'good.mp3'
    good.write_bytes(b'public')
    assert cog._guild_files(guild) == ['good.mp3']
    with pytest.raises(ValueError):
        await cog._post_file(guild, NS(), 'leak')
    panel = MediaView(cog, NS(id=8), guild)
    panel.selected, panel.confirming = 'good.mp3', True
    good.unlink()
    good.symlink_to(outside)
    class Response:
        def is_done(self):
            return False
        async def edit_message(self, **kwargs):
            pass
    interaction = NS(user=NS(id=8, guild_permissions=NS(administrator=True)),
                     guild=guild, response=Response(), client=cog.bot)
    await panel.children[-1].callback(interaction)
    assert good.is_symlink()
    assert outside.read_bytes() == b'private'
    cog._cleanup_media_files(directory, 'leak')
    assert (directory / 'leak.mp3').is_symlink()
    panel.stop()
    for item in directory.iterdir():
        if item.is_dir():
            item.rmdir()
        else:
            item.unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    assert cog._guild_files(guild) == []
    assert 'Cannot access media library' in cog._do_addmedia(
        guild, 'http://invalid.test/file.mp3', 'safe', None, None)


def test_download_staging_does_not_follow_preexisting_links(library, tmp_path):
    cog, guild, directory = library
    outside = tmp_path / 'outside.mp3'
    outside.write_bytes(b'keep')
    (directory / 'clip.mp3').symlink_to(outside)
    (directory / 'clip.mp3.part').symlink_to(outside)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'new media')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/file.mp3'
        result = cog._do_addmedia(guild, url, 'clip', None, None)
        assert 'symbolic links' in result
        assert outside.read_bytes() == b'keep'
        assert (directory / 'clip.mp3').is_symlink()
        (directory / 'clip.mp3').unlink()
        result = cog._do_addmedia(guild, url, 'clip', None, None)
        assert 'has been added' in result
        assert (directory / 'clip.mp3').read_bytes() == b'new media'
        assert outside.read_bytes() == b'keep'
        assert (directory / 'clip.mp3.part').is_symlink()
        assert not any(p.is_dir() for p in directory.iterdir())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
