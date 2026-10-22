"""Media names must not escape the library or expose host files."""
import json
from types import SimpleNamespace as NS
from pathlib import Path

import pytest

from cogs.media import Media, MediaView
from core import mcp_server
from core.ops import OpContext, registry


@pytest.fixture
def library(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    cog = Media(NS(config=config))
    guild = NS(id=17, name='Example')
    directory = tmp_path / 'media' / '17'
    directory.mkdir(parents=True)
    yield cog, guild, directory
    cog.cog_unload()


@pytest.mark.asyncio
async def test_media_listing_selects_mcp_guild_and_confines_chat(library, config):
    cog, guild, directory = library
    (directory / 'clip.mp4').write_bytes(b'clip')
    other = NS(id=18, get_member=lambda uid: None)
    guild.get_member = lambda uid: None
    bot = NS(config=config, get_guild=lambda gid: {17: guild, 18: other}.get(gid))
    config.set_global('mcp_tools_enabled', ['list_media'])
    registry.register_cog_ops(cog)
    try:
        # FastMCP validates arguments against the generated schema; an
        # undeclared guild_id would be discarded before context resolution.
        server = mcp_server.build_server(bot)
        blocks = await server.call_tool('list_media',
                                        {'actor_id': '8', 'guild_id': '17'})
        payload = json.loads(blocks.content[0].text)
        assert payload == {'ok': True, 'names': ['clip.mp4'], 'count': 1}
        ctx = OpContext(bot=bot, author=NS(id=8), guild=guild)
        ambient = await registry.call_ids('list_media', ctx, allowed_guild_ids={17})
        assert ambient.ok and ambient.value['names'] == ['clip.mp4']
        refused = await registry.call_ids('list_media', ctx,
                                          allowed_guild_ids={17}, guild_id='18')
        assert not refused.ok
    finally:
        registry.unregister_owner(cog)


def test_unsafe_names_cannot_download_or_cleanup_other_files(library, tmp_path):
    cog, guild, directory = library
    outside = tmp_path / 'keep.mp3'
    outside.write_bytes(b'outside')
    own = directory / 'keep.mp3'
    own.write_bytes(b'library')
    for name in ('../keep', '/keep', r'..\keep', '%(title)s'):
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


def test_download_staging_does_not_follow_preexisting_links(library, tmp_path, monkeypatch):
    cog, guild, directory = library
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'keep')
    for name in ('clip.mp4.part', 'clip_tmp.mp4'):
        (directory / name).symlink_to(outside)

    class Downloader:
        def __init__(self, options):
            self.output = Path(options['outtmpl'].replace('%(ext)s', 'mp4'))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def download(self, urls):
            # Model the scratch write that an external downloader performs.
            scratch = self.output.with_suffix('.mp4.part')
            scratch.write_bytes(b'new media')
            scratch.replace(self.output)

    def ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b'trimmed media')
        return NS(returncode=0)

    monkeypatch.setattr('cogs.media.yt_dlp.YoutubeDL', Downloader)
    monkeypatch.setattr('cogs.media.subprocess.run', ffmpeg)
    result = cog._do_addmedia(guild, 'https://example.test/watch?v=clip', 'clip', 0, 1000)
    assert 'has been added' in result
    assert (directory / 'clip.mp4').read_bytes() == b'trimmed media'
    assert outside.read_bytes() == b'keep'
    assert (directory / 'clip.mp4.part').is_symlink()
    assert (directory / 'clip_tmp.mp4').is_symlink()
    assert not any(p.is_dir() for p in directory.iterdir())
