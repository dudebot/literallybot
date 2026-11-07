"""Content-rating enforcement and boot-time recovery protection."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from cogs.danbooru import Danbooru
from core.cog_discovery import list_cog_modules


@pytest.mark.asyncio
async def test_danbooru_service_enforces_rating_on_query_and_response(config, monkeypatch):
    import requests
    from urllib.parse import parse_qs, urlsplit

    cog = Danbooru(NS(config=config, logger=logging.getLogger('test.search')))
    # Exercise the real service; only the HTTP boundary is replaced.
    response = Mock()
    response.json.return_value = [
        {'id': 1, 'rating': 's', 'file_url': 'https://example.invalid/filtered.png'},
        {'id': 2, 'file_url': 'https://example.invalid/unverified.png'},
        {'id': 3, 'rating': 'g', 'file_url': 'https://example.invalid/image.png',
         'tag_string': '1girl cat cat_paws', 'score': 19, 'fav_count': 7},
    ]
    request = Mock(return_value=response)
    monkeypatch.setattr('cogs.danbooru.requests.get', request)
    channel = NS(id=10, is_nsfw=lambda: False)
    tag = 'cat&tags=landscape'
    result = await cog.search([tag, 'rating:sensitive'], channel)
    assert result['url'] == 'https://example.invalid/image.png'
    prepared = requests.Request('GET', request.call_args.args[0],
                                params=request.call_args.kwargs['params']).prepare()
    assert parse_qs(urlsplit(prepared.url).query)['tags'] == [tag + ' rating:general']



def test_disabled_cogs_cannot_remove_the_recovery_surface(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / 'cogs'
    directory.mkdir()
    for name in ('control', 'admin', 'gpt', 'points'):
        (directory / f'{name}.py').write_text('raise RuntimeError("Discovery must not import features")')
    (directory / '_helper.py').touch()
    (directory / 'ignored').mkdir()
    config.set_global('disabled_cogs', ['control', 'cogs.core.admin', 'gpt'])
    assert list_cog_modules(config) == ['cogs.admin', 'cogs.control', 'cogs.points']
    assert set(list_cog_modules()) == {'cogs.admin', 'cogs.control', 'cogs.gpt', 'cogs.points'}
