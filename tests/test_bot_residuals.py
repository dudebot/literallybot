"""Mutation contracts: edits preserve precedence, deletion removes stale bindings."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from cogs.optional.auto_response import AutoResponse
from cogs.optional.setrole import SetRole
from cogs.optional.gpt import guild_tool_budget, trigger_images
from core.llm.client import _to_pai_messages
from pydantic_ai.messages import ImageUrl

@pytest.mark.asyncio
async def test_autoresponse_edit_preserves_precedence_and_other_fields(config):
    cog = AutoResponse(NS(config=config, logger=logging.getLogger('test')))
    ctx = NS(guild=NS(id=7))
    cog._add_entry(7, ['hello'], ['first'], 'contains', True)
    cog._add_entry(7, ['hello'], ['second'])
    await cog.op_edit_autoresponse(ctx, 0, responses=['updated'])
    assert cog._list_entries(7)[0] == dict(index=0, triggers=['hello'], responses=['updated'], match='contains', auto_delete=True)
    await cog.op_edit_autoresponse(ctx, 0, delete=True)
    assert cog._list_entries(7)[0]['responses'] == ['second']

@pytest.mark.asyncio
async def test_remove_toggle_drops_binding_and_only_bot_reaction(config):
    message = NS(remove_reaction=AsyncMock())
    channel = NS(fetch_message=AsyncMock(return_value=message))
    guild = NS(id=7, get_channel=lambda _: channel)
    bot = NS(config=config, logger=logging.getLogger('test'), user=NS(id=8))
    cog = SetRole(bot)
    config.set(7, 'emoji_role_toggles', [dict(channel_id=10,message_id=20,emoji='👍',role_id=30)])
    result = await cog.op_remove_emoji_role_toggle(NS(guild=guild),20,'👍')
    assert result['reaction_removed'] and config.get(7,'emoji_role_toggles') == []
    assert message.remove_reaction.await_args.args[1] is bot.user
    assert (await cog.op_remove_emoji_role_toggle(NS(guild=guild),20,'👍'))['status'] == 'not_found'

def test_model_input_contains_pixels_not_just_filename():
    message=NS(attachments=[NS(content_type='image/png', url=f'https://cdn.discordapp.com/{i}.png') for i in range(8)],embeds=[])
    urls=trigger_images(message)
    assert len(urls)==4
    parts=_to_pai_messages([{'role':'user','content':'Read the chart','images':urls}])[0].parts[0].content
    assert len([x for x in parts if isinstance(x,ImageUrl)])==4
    assert _to_pai_messages([{'role':'user','content':'filename only'}])[0].parts[0].content=='filename only'

def test_budget_is_guild_local_and_bounded(config):
    config.set(7,'ai_tool_budget',10000)
    assert guild_tool_budget(config,7)==16 and guild_tool_budget(config,8)==8

@pytest.mark.asyncio
async def test_eos_reply_is_silent_without_empty_apology(config, monkeypatch):
    from contextlib import asynccontextmanager
    from cogs.optional.gpt import Gpt
    @asynccontextmanager
    async def typing():
        yield
    author=NS(id=10,display_name='Reader')
    ctx=NS(guild=NS(id=7),channel=NS(id=8),author=author,message=NS(id=9),typing=typing,send=AsyncMock())
    cog=Gpt(NS(config=config,logger=logging.getLogger('test'),user=NS(id=11)))
    monkeypatch.setattr(cog,'_check_cooldown',lambda ctx: None)
    monkeypatch.setattr(cog,'_prepare_cached_history',AsyncMock(return_value=([],('key',),9,0)))
    monkeypatch.setattr(cog,'call_ai_api',AsyncMock(return_value=' <|eos|> \n'))
    await cog.process_askgpt(ctx,'hello')
    ctx.send.assert_not_awaited()
    cog.call_ai_api=AsyncMock(return_value='Done. <|eos|>')
    await cog.process_askgpt(ctx,'hello')
    assert ctx.send.await_args.args[0]=='Done.'
