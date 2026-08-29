"""End-to-end lifecycle for the REAL cog-provided ops.

tests/test_ops_registry.py already proves the registry machinery against
synthetic cogs. This file proves the shipped cogs actually participate: that
`cogs/optional/setrole.py`, `cogs/optional/danbooru.py`,
`cogs/optional/auto_response.py` and `cogs/optional/media.py` declare ops
which appear on load, vanish on unload, and re-register cleanly on the next
boot without colliding with their own stale names.
Most tests here drive the registry API directly for isolation; the
`test_real_literallybot_add_cog_*` pair at the bottom exercises the actual
`LiterallyBot.add_cog`/`remove_cog` overrides bot.py installs, against the
shared registry, so the wiring itself is covered too.

It also pins the properties that make these ops safe to hand to an agent:
the danbooru rating policy lives in the service (so no caller can bypass it),
the ops never touch an Interaction (issue #64: a tool needing `ctx.send` is
not headless), and every op that returns data declares a serializer.

That last one needs asserting through `Op.result_payload` specifically, NOT
through `op.impl(...)`. An op with no `serialize` still returns a rich dict
from its impl while both shipped frontends (core/agent_loop.py,
core/mcp_server.py) see nothing but `{"ok": true}` — an impl-level assertion
passes vacuously while the agent gets an empty payload. Every op here has a
`_payload` test for that reason; new cog ops want one too.
"""

import asyncio
import inspect
import logging

import pytest

from core.ops import (ORIGIN_COG, OpResult, OpScope, OpsRegistry,
                      PermissionLevel)

from cogs.optional.auto_response import AutoResponse, find_response
from cogs.optional.danbooru import Danbooru, apply_rating_policy
from cogs.optional.media import Media
from cogs.optional.setrole import SetRole

COG_CLASSES = [SetRole, Danbooru, AutoResponse, Media]
COG_OP_NAMES = {
    SetRole: {"add_emoji_role_toggle", "sync_emoji_role_toggles"},
    Danbooru: {"search_danbooru"},
    AutoResponse: {"list_autoresponses", "add_autoresponse",
                   "remove_autoresponse"},
    Media: {"list_media", "post_media"},
}


class _FakeConfig:
    """Minimal stand-in for core.config: an in-memory key/value store with the
    same (id, key, default) / scope signature the cogs call."""

    def __init__(self, values=None):
        self.values = values or {}

    def get(self, target_id, key, default=None, scope=None):
        return self.values.get((target_id, key), default)

    def set(self, target_id, key, value, scope=None):
        self.values[(target_id, key)] = value


class _FakeBot:
    """Enough bot for a cog's __init__ and its ops; no gateway, no Discord."""

    def __init__(self):
        self.config = _FakeConfig()
        self.logger = logging.getLogger("test")
        self.guilds = []
        self.user = None

    def get_channel(self, channel_id):
        return None

    def get_guild(self, guild_id):
        return None


class _FakeChannel:
    def __init__(self, nsfw=False, channel_id=1):
        self.id = channel_id
        self._nsfw = nsfw

    def is_nsfw(self):
        return self._nsfw


@pytest.fixture
def reg():
    """An isolated registry — never mutate the shared module-level one."""
    return OpsRegistry()


@pytest.fixture
def bot():
    return _FakeBot()


# --------------------------------------------------------------------------
# Lifecycle: load -> ops present; unload -> gone; load again -> no collision.
#
# The cog set is fixed at boot (#86), so in production this is startup and
# shutdown teardown. The register/unregister/register cycle is asserted at the
# REGISTRY level because that is the machinery both ends run through, and
# because it is what makes a restart (or a re-registration through the kept
# fail-closed belt) clean rather than a duplicate-name failure.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_cog_ops_register_with_cog_origin(reg, bot, cog_class):
    cog = cog_class(bot)
    names = reg.register_cog_ops(cog)
    assert set(names) == COG_OP_NAMES[cog_class]
    for name in names:
        o = reg.require(name)
        assert o.origin == ORIGIN_COG, "cog ops must be stamped by the registration path"
        assert o.owner is cog
        assert o.scope == OpScope.GUILD
        assert o.group, f"op '{name}' must declare a group id"
        assert reg.label_for(o.group), (
            f"op '{name}' group '{o.group}' has no label — pass group_label= "
            f"on @op so the panel can name the section")


@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_unload_removes_the_cogs_ops(reg, bot, cog_class):
    cog = cog_class(bot)
    reg.register_cog_ops(cog)
    removed = reg.unregister_owner(cog)
    assert set(removed) == COG_OP_NAMES[cog_class]
    assert reg.names() == [], "an unloaded cog must leave no ops behind"


@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_reregistration_does_not_collide(reg, bot, cog_class):
    """A restart builds a NEW cog instance after the old one was torn down;
    the new batch must register cleanly rather than hitting its own stale
    names. Asserted per-process here because unregister_owner leaving a name
    reserved would only surface on the NEXT boot, i.e. far from the cause."""
    old = cog_class(bot)
    reg.register_cog_ops(old)
    reg.unregister_owner(old)
    new = cog_class(bot)
    reg.register_cog_ops(new)
    assert set(reg.names()) == COG_OP_NAMES[cog_class]
    assert reg.require(next(iter(COG_OP_NAMES[cog_class]))).owner is new


def test_both_cogs_coexist(reg, bot):
    """Loaded together the two cogs must not fight over a name, and each
    stays independently removable."""
    setrole, danbooru = SetRole(bot), Danbooru(bot)
    reg.register_cog_ops(setrole)
    reg.register_cog_ops(danbooru)
    assert set(reg.names()) == COG_OP_NAMES[SetRole] | COG_OP_NAMES[Danbooru]
    reg.unregister_owner(danbooru)
    assert set(reg.names()) == COG_OP_NAMES[SetRole]


def test_lifecycle_against_the_shared_registry(bot):
    """The real cogs against the real module-level registry the frontends
    read, rather than an isolated one — this is what the panel and the MCP
    server actually see. The registry is restored afterwards so test order
    stays irrelevant.
    """
    from core.ops import registry as shared

    cog = SetRole(bot)
    # qualified_name comes from discord.py's CogMeta; bot.py's remove_cog
    # override looks the cog up by it before unregistering.
    assert cog.qualified_name == "SetRole"

    before = set(shared.names())
    assert not (COG_OP_NAMES[SetRole] & before), \
        "cog ops must not be registered at import time"
    try:
        registered = shared.register_cog_ops(cog)
        assert set(registered) == COG_OP_NAMES[SetRole]
        assert COG_OP_NAMES[SetRole] <= set(shared.names())
        # Guild-scoped cog ops join the live agent universe automatically.
        assert COG_OP_NAMES[SetRole] <= set(shared.guild_agent_names())
    finally:
        shared.unregister_owner(cog)
    assert set(shared.names()) == before, \
        "the shared registry must be left exactly as it was found"


def test_ops_are_absent_until_a_cog_instance_registers():
    """Importing the cog modules must not leak ops into the shared registry —
    a spec is inert until a live instance backs it."""
    from core.ops import registry as shared

    for names in COG_OP_NAMES.values():
        for name in names:
            assert shared.get(name) is None


# --------------------------------------------------------------------------
# Headlessness: an op may not depend on an Interaction (issue #64).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_op_impls_take_no_interaction(reg, bot, cog_class):
    """The op signature must speak ctx/plain objects. An op that wanted an
    Interaction would be an interaction handler wearing a tool's hat, and
    could never be called by the MCP frontend."""
    cog = cog_class(bot)
    for name in reg.register_cog_ops(cog):
        params = inspect.signature(reg.require(name).impl).parameters
        assert "interaction" not in params
        assert list(params)[0] == "ctx"


# --------------------------------------------------------------------------
# Danbooru: the rating policy travels WITH the search service.
# --------------------------------------------------------------------------

def test_rating_policy_forces_safe_outside_nsfw_channels():
    assert apply_rating_policy(["cat"], _FakeChannel(nsfw=False)) == ["cat", "rating:safe"]


def test_rating_policy_strips_user_supplied_rating_tags():
    """Without the strip, a caller could pass rating:explicit and have it sit
    alongside the forced rating:safe — Danbooru would honour the first."""
    tags = apply_rating_policy(["cat", "rating:explicit", "RATING:questionable"],
                               _FakeChannel(nsfw=False))
    assert tags == ["cat", "rating:safe"]


def test_rating_policy_leaves_nsfw_channels_alone():
    tags = apply_rating_policy(["cat", "rating:explicit"], _FakeChannel(nsfw=True))
    assert tags == ["cat", "rating:explicit"]


def test_rating_policy_treats_dms_as_not_nsfw():
    """A DMChannel has no is_nsfw at all; absence must fail SAFE."""
    class _DM:
        id = 5

    assert apply_rating_policy(["cat"], _DM()) == ["cat", "rating:safe"]


def test_search_applies_the_policy_before_querying(bot, monkeypatch):
    """The policy is enforced inside the service, so the op and the command
    both get it for free — this asserts the tags that reach the API."""
    cog = Danbooru(bot)
    seen = {}

    def fake_get(url, *args, **kwargs):
        seen["url"] = url

        class _Resp:
            def json(self):
                return [{"id": 1, "file_url": "https://example.invalid/i.png"}]
        return _Resp()

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", fake_get)
    result = asyncio.run(cog.search(["cat", "rating:explicit"], _FakeChannel(nsfw=False)))
    assert result["status"] == "ok"
    assert result["url"] == "https://example.invalid/i.png"
    assert "rating%3Asafe" in seen["url"] or "rating:safe" in seen["url"]
    assert "explicit" not in seen["url"]


def test_search_returns_a_url_and_posts_nothing(bot, monkeypatch):
    """The op returns the URL; the CALLER decides whether to post it. If the
    service ever started sending, this catches it — the fake channel has no
    send()."""
    cog = Danbooru(bot)

    def fake_get(url, *args, **kwargs):
        class _Resp:
            def json(self):
                return [{"id": 7, "file_url": "https://example.invalid/x.png"}]
        return _Resp()

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", fake_get)
    result = asyncio.run(cog.search(["cat"], _FakeChannel(nsfw=True)))
    assert result["url"] == "https://example.invalid/x.png"


def test_search_does_not_repeat_a_post(bot, monkeypatch):
    """posted_danbooru is per-cog-instance state the op shares with the
    command — proof the op runs against the live instance, not a fresh one."""
    cog = Danbooru(bot)

    def fake_get(url, *args, **kwargs):
        class _Resp:
            def json(self):
                return [{"id": 1, "file_url": "https://example.invalid/a.png"},
                        {"id": 2, "file_url": "https://example.invalid/b.png"}]
        return _Resp()

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", fake_get)
    channel = _FakeChannel(nsfw=True)
    first = asyncio.run(cog.search(["cat"], channel))
    second = asyncio.run(cog.search(["cat"], channel))
    assert first["url"] != second["url"]


def test_search_reports_api_failure_without_raising(bot, monkeypatch):
    cog = Danbooru(bot)

    def boom(url, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", boom)
    result = asyncio.run(cog.search(["cat"], _FakeChannel(nsfw=True)))
    assert result["status"] == "error"
    assert result["message"]


# --------------------------------------------------------------------------
# SetRole: the op and the slash command share one service.
# --------------------------------------------------------------------------

class _FakeRole:
    def __init__(self, role_id, name="role"):
        self.id = role_id
        self.name = name


class _FakeMessage:
    def __init__(self):
        self.reactions = []
        self.added = []

    async def add_reaction(self, emoji):
        self.added.append(str(emoji))


class _FakeGuild:
    def __init__(self, guild_id=100):
        self.id = guild_id
        self.roles = {}
        self.text_channels = []
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_emoji(self, emoji_id):
        return None


class _FakeGuildChannel:
    def __init__(self, guild, channel_id=200):
        self.id = channel_id
        self.guild = guild
        self.message = _FakeMessage()

    async def fetch_message(self, message_id):
        return self.message


def _setrole_env():
    bot = _FakeBot()
    cog = SetRole(bot)
    guild = _FakeGuild()
    channel = _FakeGuildChannel(guild)
    role = _FakeRole(300, "Verified")
    guild.roles[role.id] = role
    guild.channels[channel.id] = channel
    return cog, guild, channel, role


def test_add_toggle_creates_an_entry_in_the_flat_quadruplet_shape():
    """The stored shape is load-bearing: /role sync, the reaction listeners
    and any agent editing the JSON all read these four keys."""
    cog, guild, channel, role = _setrole_env()
    result = asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    assert result["status"] == "created"
    stored = cog.bot.config.values[(guild.id, "emoji_role_toggles")]
    assert stored == [{
        "channel_id": channel.id,
        "message_id": 42,
        "emoji": "\N{THUMBS UP SIGN}",
        "role_id": role.id,
    }]
    assert channel.message.added == ["\N{THUMBS UP SIGN}"], \
        "the toggle's reaction must be pre-populated on the message"


def test_add_toggle_is_idempotent_for_the_same_role():
    cog, guild, channel, role = _setrole_env()
    asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    result = asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    assert result["status"] == "unchanged"
    assert len(cog.bot.config.values[(guild.id, "emoji_role_toggles")]) == 1


def test_add_toggle_refuses_to_retarget_without_replace_existing():
    """The slash command asks the user to confirm; the op reports 'exists' and
    writes NOTHING. Silently retargeting would hand an agent the power to
    rewire a server's roles on a typo."""
    cog, guild, channel, role = _setrole_env()
    other = _FakeRole(301, "Other")
    guild.roles[other.id] = other
    asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    result = asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", other))
    assert result["status"] == "exists"
    assert result["old_role_id"] == role.id
    stored = cog.bot.config.values[(guild.id, "emoji_role_toggles")]
    assert stored[0]["role_id"] == role.id, "nothing may be written on 'exists'"


def test_add_toggle_retargets_when_replace_existing_is_set():
    cog, guild, channel, role = _setrole_env()
    other = _FakeRole(301, "Other")
    guild.roles[other.id] = other
    asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    result = asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", other,
                                         replace_existing=True))
    assert result["status"] == "updated"
    assert result["old_role_id"] == role.id
    stored = cog.bot.config.values[(guild.id, "emoji_role_toggles")]
    assert len(stored) == 1 and stored[0]["role_id"] == other.id


def test_add_toggle_surfaces_a_failed_reaction_as_a_message():
    """The op turns this into an OpResult error; the command shows the text.
    Either way the entry must not be stored for a message we can't react to."""
    cog, guild, channel, role = _setrole_env()

    async def boom(message_id):
        raise RuntimeError("unknown message")

    channel.fetch_message = boom
    with pytest.raises(RuntimeError, match="Failed to add reaction"):
        asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    assert (guild.id, "emoji_role_toggles") not in cog.bot.config.values


def test_op_add_emoji_role_toggle_goes_through_the_same_service(reg):
    """Registered and called as an op, with ids already resolved to objects —
    the same write the slash command performs."""
    cog, guild, channel, role = _setrole_env()
    reg.register_cog_ops(cog)
    o = reg.require("add_emoji_role_toggle")
    assert o.permission == PermissionLevel.ADMIN

    result = asyncio.run(o.impl(None, channel, 42, "\N{THUMBS UP SIGN}", role))
    assert result["status"] == "created"
    assert cog.bot.config.values[(guild.id, "emoji_role_toggles")][0]["role_id"] == role.id


def test_op_sync_returns_serializable_snowflake_strings(reg):
    """Snowflakes leave as strings for the 2**53 reason (see core/ops.py) —
    an int here would round in JSON transit and report the wrong message.

    Asserted on `result_payload`, the envelope both frontends actually send,
    so a missing serializer fails here instead of passing against an impl
    return value nothing downstream ever sees."""
    cog, guild, channel, role = _setrole_env()
    asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    guild.text_channels = [channel]
    reg.register_cog_ops(cog)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.guild = guild
    o = reg.require("sync_emoji_role_toggles")
    payload = o.result_payload(OpResult(ok=True, value=asyncio.run(o.impl(ctx))))
    assert payload["ok"] is True
    assert payload["count"] == 1
    entry = payload["entries"][0]
    for key in ("channel_id", "message_id", "role_id"):
        assert isinstance(entry[key], str), f"{key} must travel as a string"
    assert entry["emoji"] == "\N{THUMBS UP SIGN}"
    assert entry["status"]


# --------------------------------------------------------------------------
# Frontend payloads: what the agent and MCP actually receive.
#
# `op.impl(...)` returning a rich dict proves nothing — Op.serialize_result
# drops it whole when the op declares no serializer, so an impl-level
# assertion can pass while the frontend gets a bare {"ok": true}. These go
# through result_payload, which is what core/agent_loop.py and
# core/mcp_server.py call.
# --------------------------------------------------------------------------

def test_every_cog_op_that_returns_data_declares_a_serializer(reg):
    """The blanket rule, so a new cog op cannot ship data-blind. If an op is
    ever added that genuinely returns nothing, exempt it here BY NAME rather
    than deleting the check."""
    returns_nothing = set()
    for cog_class in COG_CLASSES:
        cog = cog_class(_FakeBot())
        for name in reg.register_cog_ops(cog):
            if name in returns_nothing:
                continue
            assert reg.require(name).serialize is not None, (
                f"op '{name}' returns data but declares no serialize=, so "
                f"every frontend would see only {{'ok': True}}")
        reg.unregister_owner(cog)


def test_search_danbooru_payload_carries_the_url(reg, bot, monkeypatch):
    """The op's whole purpose is handing the agent a URL to send itself."""
    cog = Danbooru(bot)
    reg.register_cog_ops(cog)

    def fake_get(url, *args, **kwargs):
        class _Resp:
            def json(self):
                return [{"id": 7, "file_url": "https://example.invalid/x.png"}]
        return _Resp()

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", fake_get)
    o = reg.require("search_danbooru")
    value = asyncio.run(o.impl(None, _FakeChannel(nsfw=True), "cat"))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["url"] == "https://example.invalid/x.png"
    assert payload["post_id"] == "7", "ids travel as strings (2**53)"


def test_search_danbooru_payload_carries_suggestions_on_no_results(reg, bot, monkeypatch):
    """'no_results' is an outcome the agent is told to retry from, so the
    status and the suggested spellings both have to survive the envelope."""
    cog = Danbooru(bot)
    reg.register_cog_ops(cog)

    def fake_get(url, *args, **kwargs):
        class _Resp:
            def json(self):
                return []
        return _Resp()

    monkeypatch.setattr("cogs.optional.danbooru.requests.get", fake_get)

    async def fake_suggest(first_tag):
        return ["cat_girl"]

    cog._suggest = fake_suggest
    o = reg.require("search_danbooru")
    value = asyncio.run(o.impl(None, _FakeChannel(nsfw=True), "ct"))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["status"] == "no_results"
    assert payload["suggestions"] == ["cat_girl"]
    assert "url" not in payload


def test_add_toggle_payload_carries_the_exists_conflict(reg):
    """The op's agent_guidance tells the model to branch on 'exists' (nothing
    was written). If the status doesn't reach the model, that is unfollowable
    and the agent reports a binding it never made."""
    cog, guild, channel, role = _setrole_env()
    other = _FakeRole(999, "other")
    reg.register_cog_ops(cog)
    o = reg.require("add_emoji_role_toggle")

    asyncio.run(o.impl(None, channel, 42, "\N{THUMBS UP SIGN}", role))
    value = asyncio.run(o.impl(None, channel, 42, "\N{THUMBS UP SIGN}", other))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["ok"] is True
    assert payload["status"] == "exists"
    assert payload["role_id"] == str(other.id)
    assert payload["old_role_id"] == str(role.id)
    # 'exists' means nothing was written — the stored role must be unchanged.
    stored = cog.bot.config.values[(guild.id, "emoji_role_toggles")]
    assert len(stored) == 1 and stored[0]["role_id"] == role.id


def test_retarget_reports_a_create_when_the_mapping_vanished_mid_prompt():
    """The /role add confirm closure re-runs the service rather than committing
    the entries list it read before the prompt, so a concurrent /role delete
    can't be clobbered. The cost is that the outcome may no longer be the edit
    the user was shown — so the status has to come back honestly as 'created',
    and the command reports the recreate instead of claiming "Updated"."""
    cog, guild, channel, role = _setrole_env()
    other = _FakeRole(999, "Other")
    guild.roles[other.id] = other

    # Admin A: emoji already bound to `role`, retarget to `other` -> conflict.
    asyncio.run(cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", role))
    conflict = asyncio.run(
        cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", other))
    assert conflict["status"] == "exists"

    # Admin B deletes the toggle while the confirm prompt sits open.
    cog._save(guild.id, [])

    # Admin A clicks "Change it": the entry is gone, so this is a CREATE.
    confirmed = asyncio.run(
        cog._add_toggle(guild, channel, 42, "\N{THUMBS UP SIGN}", other,
                        replace_existing=True))
    assert confirmed["status"] == "created", (
        "a vanished mapping must be reported as recreated, not as an edit")
    stored = cog.bot.config.values[(guild.id, "emoji_role_toggles")]
    assert len(stored) == 1 and stored[0]["role_id"] == other.id


def test_retarget_does_not_copy_the_custom_emoji_in_twice():
    """_add_toggle runs _ensure_guild_emoji before the duplicate check, and the
    confirm closure calls _add_toggle a second time. Passing the returned
    `emoji` (the guild-LOCAL copy) rather than the original foreign string is
    what stops the second pass burning another emoji slot."""
    cog, guild, channel, role = _setrole_env()
    other = _FakeRole(999, "Other")
    guild.roles[other.id] = other
    foreign, local = "<:foo:1111111111111111>", "<:foo:2222222222222222>"
    uploads = []

    class _LocalEmoji:
        id = 2222222222222222
        name = "foo"

        def __str__(self):
            return local

    async def fake_create(name, image):
        uploads.append(name)
        return _LocalEmoji()

    guild.create_custom_emoji = fake_create
    # The guild owns the local copy only once it has been created.
    guild.get_emoji = lambda eid: _LocalEmoji() if (
        eid == 2222222222222222 and uploads) else None

    async def fake_fetch(_guild, partial_emoji):
        if not partial_emoji.id or _guild.get_emoji(partial_emoji.id):
            return partial_emoji
        return await fake_create(partial_emoji.name, b"")

    cog._ensure_guild_emoji = fake_fetch

    first = asyncio.run(cog._add_toggle(guild, channel, 42, foreign, role))
    assert uploads == ["foo"], "first call copies the foreign emoji in"
    conflict = asyncio.run(cog._add_toggle(guild, channel, 42,
                                           first["emoji"], other))
    assert conflict["status"] == "exists"
    # The confirm path passes the LOCAL emoji string back in.
    asyncio.run(cog._add_toggle(guild, channel, 42, conflict["emoji"], other,
                                replace_existing=True))
    assert uploads == ["foo"], (
        "the retarget re-parsed the original foreign id and uploaded a "
        "second copy, burning another emoji slot")


def test_op_failure_payload_carries_the_error_not_the_value(reg):
    """The failure envelope is the same shape for every op — no serializer
    runs, and the reason travels instead."""
    cog, guild, channel, role = _setrole_env()
    reg.register_cog_ops(cog)
    o = reg.require("add_emoji_role_toggle")
    payload = o.result_payload(OpResult(ok=False, error="unknown message"))
    assert payload == {"ok": False, "error": "unknown message"}


# --------------------------------------------------------------------------
# AutoResponse: the ops and the panel share one service.
#
# The panel modal used to hold validation, normalization and the cap check
# inline; they live in _add_entry now, so these assertions cover BOTH callers.
# --------------------------------------------------------------------------

def _autoresponse_env():
    cog = AutoResponse(_FakeBot())
    return cog, 100  # cog, guild_id


def test_add_entry_lowercases_triggers_outside_regex_mode():
    cog, gid = _autoresponse_env()
    result = cog._add_entry(gid, ["PING", " Pong "], ["hi"], match="full")
    assert result["entry"]["triggers"] == ["ping", "pong"]
    assert cog.bot.config.values[(gid, "auto_responses")] == [result["entry"]]


def test_add_entry_preserves_case_for_regex_triggers():
    r"""Lowercasing a pattern mangles \bT\b and [A-Z] — matching is already
    case-insensitive at search time, so the stored pattern must be untouched."""
    cog, gid = _autoresponse_env()
    result = cog._add_entry(gid, [r"\bThink\b"], ["hm"], match="regex")
    assert result["entry"]["triggers"] == [r"\bThink\b"]


def test_add_entry_rejects_an_invalid_regex_without_storing():
    """find_response SKIPS a bad pattern rather than raising, so an unvalidated
    entry would be stored and silently never fire."""
    cog, gid = _autoresponse_env()
    with pytest.raises(ValueError, match="Invalid regex"):
        cog._add_entry(gid, ["[unclosed"], ["hi"], match="regex")
    assert (gid, "auto_responses") not in cog.bot.config.values


def test_add_entry_rejects_an_unknown_match_mode():
    cog, gid = _autoresponse_env()
    with pytest.raises(ValueError, match="match must be one of"):
        cog._add_entry(gid, ["ping"], ["pong"], match="fuzzy")


def test_add_entry_requires_a_trigger_and_a_response():
    cog, gid = _autoresponse_env()
    with pytest.raises(ValueError, match="at least one trigger"):
        cog._add_entry(gid, ["  "], ["pong"])
    with pytest.raises(ValueError, match="at least one trigger"):
        cog._add_entry(gid, ["ping"], [""])


def test_add_entry_enforces_the_25_entry_cap():
    """The cap is the panel dropdown's hard limit; an agent appending past it
    would build a table the panel cannot render."""
    from cogs.optional.auto_response import MAX_ENTRIES

    cog, gid = _autoresponse_env()
    for i in range(MAX_ENTRIES):
        cog._add_entry(gid, [f"t{i}"], ["r"])
    with pytest.raises(ValueError, match="cap"):
        cog._add_entry(gid, ["one-too-many"], ["r"])
    assert len(cog.bot.config.values[(gid, "auto_responses")]) == MAX_ENTRIES


def test_add_entry_at_an_index_overwrites_rather_than_appends():
    """The panel's Edit button passes index=; the op never does."""
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["a"], ["1"])
    cog._add_entry(gid, ["b"], ["2"])
    result = cog._add_entry(gid, ["b2"], ["2b"], index=1)
    assert result["status"] == "updated" and result["index"] == 1
    stored = cog.bot.config.values[(gid, "auto_responses")]
    assert [e["triggers"] for e in stored] == [["a"], ["b2"]]


def test_remove_entry_returns_what_it_dropped_and_rejects_a_bad_index():
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["a"], ["1"])
    cog._add_entry(gid, ["b"], ["2"])
    result = cog._remove_entry(gid, 0)
    assert result["status"] == "removed"
    assert result["entry"]["triggers"] == ["a"]
    assert result["count"] == 1
    with pytest.raises(ValueError, match="No entry at index"):
        cog._remove_entry(gid, 5)


def test_entries_written_by_the_service_match_at_runtime():
    """The whole point of sharing the service: what add_autoresponse stores is
    what the on_message matcher actually fires on."""
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["PING"], ["pong"], match="full")
    cog._add_entry(gid, ["cope"], ["seethe"], match="contains")
    cog._add_entry(gid, [r"\bthink\b"], ["hmm"], match="regex")
    entries = cog._entries(gid)

    assert find_response(entries, "ping")[1] == "pong"
    assert find_response(entries, "PING") [1] == "pong"
    assert find_response(entries, "well ping there") is None, "full mode is exact"
    assert find_response(entries, "I cope daily")[1] == "seethe"
    assert find_response(entries, "think about it")[1] == "hmm"
    assert find_response(entries, "rethinking it") is None, \
        r"\bthink\b must not fire inside 'rethinking'"


def test_find_response_picks_from_every_response(monkeypatch):
    """Responses are picked uniformly at random — the [text, weight] shape is
    tolerated by _response_texts but the weight is deliberately ignored."""
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["ping"], ["a", "b", "c"])
    entries = cog._entries(gid)
    picks = {find_response(entries, "ping")[1] for _ in range(200)}
    assert picks == {"a", "b", "c"}


def test_first_matching_entry_wins_in_config_order():
    """list_autoresponses reports the index precisely because order is
    precedence — an agent reordering by removing/re-adding changes behaviour."""
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["hi"], ["first"], match="contains")
    cog._add_entry(gid, ["hi"], ["second"], match="contains")
    assert find_response(cog._entries(gid), "hi there")[1] == "first"


def test_list_autoresponses_payload_carries_indexed_entries(reg):
    """Asserted through result_payload, the envelope the frontends send — an
    impl-level assertion would pass even with no serializer at all."""
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["ping"], ["pong"], match="contains", auto_delete=True)
    reg.register_cog_ops(cog)
    o = reg.require("list_autoresponses")
    assert o.permission == PermissionLevel.ADMIN

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.guild = _FakeGuild(gid)
    payload = o.result_payload(OpResult(ok=True, value=asyncio.run(o.impl(ctx))))
    assert payload["ok"] is True and payload["count"] == 1
    entry = payload["entries"][0]
    assert entry == {"index": 0, "triggers": ["ping"], "responses": ["pong"],
                     "match": "contains", "auto_delete": True}


def test_add_autoresponse_payload_carries_the_stored_entry(reg):
    cog, gid = _autoresponse_env()
    reg.register_cog_ops(cog)
    o = reg.require("add_autoresponse")
    assert o.permission == PermissionLevel.ADMIN

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.guild = _FakeGuild(gid)
    value = asyncio.run(o.impl(ctx, ["PING"], ["pong"], "full", False))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["status"] == "added"
    assert payload["index"] == 0 and payload["count"] == 1
    assert payload["entry"]["triggers"] == ["ping"]
    assert payload["entry"]["match"] == "full"


def test_remove_autoresponse_payload_reports_the_dropped_entry(reg):
    cog, gid = _autoresponse_env()
    cog._add_entry(gid, ["a"], ["1"])
    cog._add_entry(gid, ["b"], ["2"])
    reg.register_cog_ops(cog)
    o = reg.require("remove_autoresponse")
    assert o.permission == PermissionLevel.ADMIN

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.guild = _FakeGuild(gid)
    value = asyncio.run(o.impl(ctx, 0))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["status"] == "removed"
    assert payload["entry"]["triggers"] == ["a"]
    assert payload["count"] == 1
    assert [e["triggers"] for e in cog._entries(gid)] == [["b"]]


def test_autoresponse_ops_refuse_to_run_outside_a_guild(reg):
    """Guild-scoped config with no guild would write under a None key."""
    cog, _ = _autoresponse_env()
    reg.register_cog_ops(cog)

    class _Ctx:
        guild = None

    for name, args in (("list_autoresponses", ()),
                       ("add_autoresponse", (["a"], ["b"])),
                       ("remove_autoresponse", (0,))):
        with pytest.raises(ValueError, match="guild"):
            asyncio.run(reg.require(name).impl(_Ctx(), *args))


# --------------------------------------------------------------------------
# Media: post_media and !<name> resolve a name the same way.
# --------------------------------------------------------------------------

class _SentMessage:
    def __init__(self, message_id, file):
        self.id = message_id
        self.file = file


class _SendingChannel:
    """A channel that records what was sent, so a service that posts can be
    told apart from one that merely returns a path."""

    def __init__(self, guild, channel_id=200):
        self.id = channel_id
        self.guild = guild
        self.sent = []

    async def send(self, content=None, *, file=None, **kwargs):
        self.sent.append(file)
        return _SentMessage(9007199254740993, file)


def _media_env(tmp_path, names=("poggers.mp4", "wow.mp3")):
    cog = Media(_FakeBot())
    guild = _FakeGuild(100)
    guild_dir = tmp_path / "media" / str(guild.id)
    guild_dir.mkdir(parents=True)
    for name in names:
        (guild_dir / name).write_bytes(b"x")
    cog._guild_dir = staticmethod(lambda g, _root=tmp_path: str(
        _root / "media" / str(g.id)))
    channel = _SendingChannel(guild)
    return cog, guild, channel


def test_find_file_matches_by_prefix_like_the_bang_command(tmp_path):
    cog, guild, _ = _media_env(tmp_path)
    assert cog._find_file(guild, "pog") == "poggers.mp4"
    assert cog._find_file(guild, "POGGERS") == "poggers.mp4"
    assert cog._find_file(guild, "nope") is None


def test_find_file_refuses_a_one_character_name(tmp_path):
    """The 2-char floor is what stops `!p` sweeping the library — the op
    inherits it by sharing the service."""
    cog, guild, _ = _media_env(tmp_path)
    assert cog._find_file(guild, "p") is None
    assert cog._find_file(guild, "") is None


def test_find_file_does_not_strip_so_the_bang_listener_is_unchanged(tmp_path):
    """`!pog ` was inert before the refactor because the listener lowered but
    never stripped. Keeping that means the refactor moved logic without
    changing what a real Discord message does."""
    cog, guild, _ = _media_env(tmp_path)
    assert cog._find_file(guild, "pog ") is None
    assert cog._find_file(guild, " pog") is None


def test_post_media_op_strips_its_own_argument(reg, tmp_path):
    """A stray space in a tool argument is a caller typo, not a message the
    user chose to send — so the op strips where the listener does not."""
    cog, guild, channel = _media_env(tmp_path)
    reg.register_cog_ops(cog)
    value = asyncio.run(reg.require("post_media").impl(None, channel, "  pog "))
    assert value["name"] == "poggers.mp4"


def test_listener_lets_a_send_failure_reach_the_error_handler(tmp_path):
    """The listener branches on _find_file rather than catching _post_file's
    ValueError: an unmatched name is 'not a media command', but a ValueError
    out of File()/channel.send() is a real failure that must NOT be swallowed."""
    cog, guild, channel = _media_env(tmp_path)

    async def boom(*args, **kwargs):
        raise ValueError("attachment too large")

    channel.send = boom

    class _Msg:
        def __init__(self):
            self.guild = guild
            self.channel = channel
            self.content = "!pog"
            self.author = type("A", (), {"bot": False})()

    with pytest.raises(ValueError, match="attachment too large"):
        asyncio.run(cog.on_message(_Msg()))


def test_listener_ignores_a_bang_message_that_names_nothing(tmp_path):
    """Every other command in the bot also starts with `!`, so a non-matching
    name must be a silent no-op, not an error."""
    cog, guild, channel = _media_env(tmp_path)

    class _Msg:
        def __init__(self, content):
            self.guild = guild
            self.channel = channel
            self.content = content
            self.author = type("A", (), {"bot": False})()

    for content in ("!", "!help", "!x"):
        asyncio.run(cog.on_message(_Msg(content)))
    assert channel.sent == []


def test_guild_without_a_library_reads_as_empty_not_an_error(tmp_path):
    cog, _, _ = _media_env(tmp_path)
    other = _FakeGuild(999)
    assert cog._guild_files(other) == []
    assert cog._find_file(other, "pog") is None


def test_post_file_sends_the_matched_item(tmp_path):
    cog, guild, channel = _media_env(tmp_path)
    result = asyncio.run(cog._post_file(guild, channel, "pog"))
    assert result["status"] == "posted"
    assert result["name"] == "poggers.mp4"
    assert len(channel.sent) == 1, "post_media acts — it must actually send"


def test_post_file_raises_when_nothing_matches(tmp_path):
    """An unmatched name is an error, not a silent no-op: the agent guidance
    tells the model to list first, and a quiet success would be a lie."""
    cog, guild, channel = _media_env(tmp_path)
    with pytest.raises(ValueError, match="No media file matching"):
        asyncio.run(cog._post_file(guild, channel, "absent"))
    assert channel.sent == []


def test_list_media_payload_carries_names_only(reg, tmp_path):
    """Names, never host paths: the path-carrying surface is admin-gated in
    core/ops.py, and this op is EVERYONE."""
    cog, guild, _ = _media_env(tmp_path)
    reg.register_cog_ops(cog)
    o = reg.require("list_media")
    assert o.permission == PermissionLevel.EVERYONE

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.guild = guild
    payload = o.result_payload(OpResult(ok=True, value=asyncio.run(o.impl(ctx))))
    assert payload["ok"] is True
    assert payload["names"] == ["poggers.mp4", "wow.mp3"]
    assert payload["count"] == 2
    assert "/" not in "".join(payload["names"]), "no host paths on the wire"


def test_post_media_payload_carries_the_matched_name_and_a_string_id(reg, tmp_path):
    """The caller may have passed a prefix, so the name that actually matched
    has to travel. message_id is > 2**53 here on purpose."""
    cog, guild, channel = _media_env(tmp_path)
    reg.register_cog_ops(cog)
    o = reg.require("post_media")
    assert o.permission == PermissionLevel.EVERYONE

    value = asyncio.run(o.impl(None, channel, "pog"))
    payload = o.result_payload(OpResult(ok=True, value=value))
    assert payload["status"] == "posted"
    assert payload["name"] == "poggers.mp4"
    assert payload["message_id"] == "9007199254740993", \
        "ids travel as strings (2**53)"


def test_post_media_requires_a_guild_channel(reg, tmp_path):
    cog, _, _ = _media_env(tmp_path)
    reg.register_cog_ops(cog)

    class _DMChannel:
        id = 1
        guild = None

    with pytest.raises(ValueError, match="guild channel"):
        asyncio.run(reg.require("post_media").impl(None, _DMChannel(), "pog"))


# --------------------------------------------------------------------------
# The REAL wiring: bot.py's add_cog/remove_cog overrides, not registry calls.
# --------------------------------------------------------------------------

def _fresh_literallybot():
    """A real LiterallyBot, offline. Importing bot has module-level side
    effects (a logs/ dir, a Config over ./configs) that are acceptable in the
    worktree; the instance never logs in."""
    import discord
    from bot import LiterallyBot
    b = LiterallyBot(command_prefix="!", intents=discord.Intents.none())
    b.config = _FakeConfig()
    b.logger = logging.getLogger("test")
    return b


def test_real_literallybot_add_cog_registers_and_remove_cog_drops():
    """The override pair is the ONLY thing stamping origin='cog' in
    production — if this wiring breaks, every behavioral primitive silently
    disappears while the cogs keep loading fine."""
    from core.ops import registry as shared

    async def flow():
        b = _fresh_literallybot()
        before = set(shared.names())
        cog = Danbooru(b)
        await b.add_cog(cog)
        try:
            assert "search_danbooru" in shared.names()
            assert shared.require("search_danbooru").owner is cog
        finally:
            await b.remove_cog(cog.qualified_name)
        assert set(shared.names()) == before

    asyncio.run(flow())


@pytest.mark.parametrize("cog_class", [AutoResponse, Media],
                         ids=lambda c: c.__name__)
def test_real_literallybot_add_cog_wires_the_behavioral_primitives(cog_class):
    """Issue #85's cogs through the production path, not a registry call —
    the override pair is the only thing stamping origin='cog', so a cog that
    loads fine can still contribute zero ops if this wiring breaks."""
    from core.ops import registry as shared

    async def flow():
        b = _fresh_literallybot()
        before = set(shared.names())
        cog = cog_class(b)
        await b.add_cog(cog)
        try:
            assert COG_OP_NAMES[cog_class] <= set(shared.names())
            # Guild-scoped, so they join the live agent universe on load.
            assert COG_OP_NAMES[cog_class] <= set(shared.guild_agent_names())
            for name in COG_OP_NAMES[cog_class]:
                assert shared.require(name).owner is cog
        finally:
            await b.remove_cog(cog.qualified_name)
        assert set(shared.names()) == before, \
            "unload must leave no ops behind"

    asyncio.run(flow())


def test_real_literallybot_add_cog_ejects_the_cog_on_a_rejected_batch():
    """All-or-none reaches through to discord.py: a cog whose batch is
    rejected (name collision with a core op) must not stay loaded — a loaded
    cog whose ops silently aren't there is the failure bot.py documents."""
    from core.ops import registry as shared
    from core.ops import PermissionLevel as _PL
    from core.ops import op as _op
    from discord.ext import commands as _commands

    class _CollidingCog(_commands.Cog):
        @_op("search_history", "Collides with a core op.", _PL.EVERYONE,
             group="messaging")
        async def clash(self, ctx):
            return None

    async def flow():
        b = _fresh_literallybot()
        before = set(shared.names())
        with pytest.raises(ValueError):
            await b.add_cog(_CollidingCog())
        assert b.get_cog("_CollidingCog") is None, \
            "a rejected batch must eject the cog from discord.py too"
        assert set(shared.names()) == before

    asyncio.run(flow())
