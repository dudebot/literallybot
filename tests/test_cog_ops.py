"""End-to-end lifecycle for the REAL cog-provided ops.

tests/test_ops_registry.py already proves the registry machinery against
synthetic cogs. This file proves the shipped cogs actually participate: that
`cogs/optional/setrole.py` and `cogs/optional/danbooru.py` declare ops which
appear on load, vanish on unload, and survive a reload without colliding —
driven through the same `LiterallyBot.add_cog`/`remove_cog` overrides bot.py
installs, not by calling the registry directly.

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

from core.ops import (ORIGIN_COG, OP_GROUPS, OpResult, OpScope, OpsRegistry,
                      PermissionLevel)

from cogs.optional.danbooru import Danbooru, apply_rating_policy
from cogs.optional.setrole import SetRole

COG_CLASSES = [SetRole, Danbooru]
COG_OP_NAMES = {
    SetRole: {"add_emoji_role_toggle", "sync_emoji_role_toggles"},
    Danbooru: {"search_danbooru"},
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
# Lifecycle: load -> ops present; unload -> gone; reload -> no collision.
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
        assert o.group in OP_GROUPS, (
            f"op '{name}' declares group '{o.group}' — cog groups must be "
            f"declared in OP_GROUPS so the panel can label them")


@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_unload_removes_the_cogs_ops(reg, bot, cog_class):
    cog = cog_class(bot)
    reg.register_cog_ops(cog)
    removed = reg.unregister_owner(cog)
    assert set(removed) == COG_OP_NAMES[cog_class]
    assert reg.names() == [], "an unloaded cog must leave no ops behind"


@pytest.mark.parametrize("cog_class", COG_CLASSES, ids=lambda c: c.__name__)
def test_reload_does_not_collide(reg, bot, cog_class):
    """A reload builds a NEW cog instance after tearing the old one down; the
    new batch must register cleanly rather than hitting its own stale names."""
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
