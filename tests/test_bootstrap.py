"""Zero-friction bootstrap (#83): token resolution, persistence, hardening.

Everything here is offline. There is no network, no real Discord, no real
getpass, and no real token — the environment-dependent boundaries
(`os.environ`, `sys.stdin.isatty`, `getpass.getpass`, `bot.run`,
`bot.application_info`) are all injected or monkeypatched.

The properties worth locking, in the order they bite an operator:

1. **Precedence.** env beats config beats prompt. A panel operator who sets
   DISCORD_TOKEN must not silently keep running on a stale stored token.
2. **The env token is never persisted.** Copying a panel-supplied secret to
   disk would surprise the operator and double the leak surface.
3. **Non-TTY fails fast.** A prompt on a systemd unit or panel console is a
   hang, not a question — and a hang looks like a crashed bot.
4. **A typed token is written only after a verified login.** A typo'd token in
   global.json converts a one-line fix into a debugging session.
5. **Superadmin bootstrap is empty-list-gated.** Existing deploys must be
   untouched; this exists to delete `!claimsuper` for NEW installs only.
6. **Secrets on disk are 0600 in a 0700 directory.** The token, provider API
   keys, and the MCP bearer token all live in the config store as plaintext.
"""

import asyncio
import os
import stat
import sys
import types

import pytest

from core import bootstrap
from core.config import DIR_MODE, FILE_MODE, Config


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------

class FakeConfig:
    """Just enough Config surface for the bootstrap chain."""

    def __init__(self, globals_=None):
        self._globals = dict(globals_ or {})
        self.flushed = 0

    def get(self, ctx, key, default=None, scope="guild"):
        # core.utils.get_superadmins reads through the general accessor.
        assert scope == "global", "bootstrap only touches global scope"
        return self._globals.get(key, default)

    def get_global(self, key, default=None):
        return self._globals.get(key, default)

    def set(self, ctx, key, value, scope="guild"):
        # get_superadmins re-persists when it normalizes a legacy shape.
        assert scope == "global", "bootstrap only touches global scope"
        self._globals[key] = value

    def set_global(self, key, value):
        self._globals[key] = value

    def flush(self):
        self.flushed += 1


class FakeBot:
    def __init__(self, config):
        self.config = config
        self._pending_token = None


class FakeLoginFailure(Exception):
    """Stands in for discord.LoginFailure so no discord client is built."""


def prompter(*answers):
    """A getpass double that returns each answer in turn."""
    answers = list(answers)
    calls = []

    def _prompt(message=""):
        calls.append(message)
        return answers.pop(0) if answers else ""

    _prompt.calls = calls
    return _prompt


# --------------------------------------------------------------------------
# 1-2. Precedence, and the env token never reaching disk
# --------------------------------------------------------------------------

def test_env_token_wins_over_config_and_prompt():
    config = FakeConfig({bootstrap.TOKEN_CONFIG_KEY: "stored-token"})
    never = prompter("prompted-token")

    token, source = bootstrap.resolve_token(
        config, env={bootstrap.TOKEN_ENV_VAR: "env-token"},
        stdin_is_tty=True, prompt=never)

    assert (token, source) == ("env-token", bootstrap.SOURCE_ENV)
    assert never.calls == []


def test_env_token_is_never_persisted():
    """The whole point of source tracking: only SOURCE_PROMPT is writable."""
    config = FakeConfig()
    token, source = bootstrap.resolve_token(
        config, env={bootstrap.TOKEN_ENV_VAR: "env-token"}, stdin_is_tty=True)

    assert source == bootstrap.SOURCE_ENV
    # resolve_token itself writes nothing...
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None

    # ...and run_bot leaves no persistence candidate for setup_hook.
    bot = FakeBot(config)
    bootstrap.run_bot(bot, run=lambda t: None, login_failure=FakeLoginFailure,
                      env={bootstrap.TOKEN_ENV_VAR: "env-token"},
                      stdin_is_tty=True)
    assert bot._pending_token is None
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None


def test_config_token_wins_over_prompt():
    config = FakeConfig({bootstrap.TOKEN_CONFIG_KEY: "stored-token"})
    never = prompter("prompted-token")

    token, source = bootstrap.resolve_token(
        config, env={}, stdin_is_tty=True, prompt=never)

    assert (token, source) == ("stored-token", bootstrap.SOURCE_CONFIG)
    assert never.calls == []


def test_blank_env_and_config_values_fall_through():
    """Whitespace-only is not a token — an operator with an empty panel field
    should reach the prompt, not hand Discord an empty string."""
    config = FakeConfig({bootstrap.TOKEN_CONFIG_KEY: "   "})
    ask = prompter("typed-token")

    token, source = bootstrap.resolve_token(
        config, env={bootstrap.TOKEN_ENV_VAR: "  "}, stdin_is_tty=True,
        prompt=ask)

    assert (token, source) == ("typed-token", bootstrap.SOURCE_PROMPT)


def test_prompted_token_is_stripped():
    config = FakeConfig()
    token, source = bootstrap.resolve_token(
        config, env={}, stdin_is_tty=True, prompt=prompter("  tok-en \n"))
    assert (token, source) == ("tok-en", bootstrap.SOURCE_PROMPT)


def test_prompt_reasks_on_empty_input_then_gives_up():
    config = FakeConfig()
    ask = prompter("", "", "")
    with pytest.raises(SystemExit) as exc:
        bootstrap.resolve_token(config, env={}, stdin_is_tty=True, prompt=ask)
    assert exc.value.code != 0
    assert len(ask.calls) == bootstrap.MAX_PROMPT_ATTEMPTS
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None


# --------------------------------------------------------------------------
# 3. Non-TTY fail-fast
# --------------------------------------------------------------------------

def test_non_tty_exits_without_reading_stdin(monkeypatch, capsys):
    """A panel console / systemd unit must get instructions and a nonzero
    exit, never a read() that can't be answered."""
    config = FakeConfig()

    def exploding_prompt(message=""):
        raise AssertionError("prompted on a non-TTY stdin")

    with pytest.raises(SystemExit) as exc:
        bootstrap.resolve_token(config, env={}, stdin_is_tty=False,
                                prompt=exploding_prompt)

    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert bootstrap.TOKEN_ENV_VAR in err
    assert "panel" in err.lower()
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None


def test_non_tty_detected_from_real_stdin(monkeypatch):
    """The default `stdin_is_tty=None` path consults sys.stdin.isatty."""
    config = FakeConfig()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit):
        bootstrap.resolve_token(config, env={})


def test_broken_stdin_counts_as_non_tty(monkeypatch):
    """A closed/detached stdin raises from isatty; that must fail fast rather
    than propagate an unrelated ValueError out of startup."""
    def boom():
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=boom))
    with pytest.raises(SystemExit):
        bootstrap.resolve_token(FakeConfig(), env={})


# --------------------------------------------------------------------------
# 4. Persist only after a verified login
# --------------------------------------------------------------------------

def test_prompted_token_persists_only_after_verified_login():
    """The candidate is staged on the bot; the WRITE happens at the login
    boundary (bot.py's setup_hook calls persist_token), not at prompt time."""
    config = FakeConfig()
    bot = FakeBot(config)
    seen = {}

    def fake_run(token):
        # Mid-run: prompted and staged, but nothing written yet.
        seen["token"] = token
        seen["staged"] = bot._pending_token
        seen["on_disk_before_login"] = config.get_global(bootstrap.TOKEN_CONFIG_KEY)
        # This is what LiterallyBot.setup_hook does once login succeeds.
        bootstrap.persist_token(bot.config, bot._pending_token)

    bootstrap.run_bot(bot, run=fake_run, login_failure=FakeLoginFailure,
                      env={}, stdin_is_tty=True, prompt=prompter("good-token"))

    assert seen["token"] == "good-token"
    assert seen["staged"] == "good-token"
    assert seen["on_disk_before_login"] is None
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) == "good-token"
    assert config.flushed  # not left sitting in the 5s write buffer


def test_bad_prompted_token_is_never_written_and_reprompts():
    config = FakeConfig()
    bot = FakeBot(config)
    attempts = []

    def fake_run(token):
        attempts.append(token)
        if token == "typo-token":
            raise FakeLoginFailure("Improper token has been passed.")
        bootstrap.persist_token(bot.config, bot._pending_token)

    bootstrap.run_bot(bot, run=fake_run, login_failure=FakeLoginFailure,
                      env={}, stdin_is_tty=True,
                      prompt=prompter("typo-token", "good-token"))

    assert attempts == ["typo-token", "good-token"]
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) == "good-token"


def test_prompt_retries_are_capped(capsys):
    config = FakeConfig()
    bot = FakeBot(config)
    attempts = []

    def always_fail(token):
        attempts.append(token)
        raise FakeLoginFailure("Improper token has been passed.")

    with pytest.raises(SystemExit) as exc:
        bootstrap.run_bot(bot, run=always_fail, login_failure=FakeLoginFailure,
                          env={}, stdin_is_tty=True,
                          prompt=prompter("bad1", "bad2", "bad3", "bad4"))

    assert exc.value.code != 0
    assert len(attempts) == bootstrap.MAX_PROMPT_ATTEMPTS
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None


def test_bad_env_token_exits_instead_of_prompting(capsys):
    """Re-prompting after a rejected ENV token would paper over the operator's
    broken panel variable — the loop is prompt-only on purpose."""
    config = FakeConfig()
    never = prompter("would-be-typed")

    def always_fail(token):
        raise FakeLoginFailure("Improper token has been passed.")

    bot = FakeBot(config)
    with pytest.raises(SystemExit):
        bootstrap.run_bot(bot, run=always_fail, login_failure=FakeLoginFailure,
                          env={bootstrap.TOKEN_ENV_VAR: "bad-env-token"},
                          stdin_is_tty=True, prompt=never)

    assert never.calls == []
    assert bootstrap.TOKEN_ENV_VAR in capsys.readouterr().err
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None


def test_bad_stored_token_exits_instead_of_looping(capsys):
    config = FakeConfig({bootstrap.TOKEN_CONFIG_KEY: "stale-token"})

    def always_fail(token):
        raise FakeLoginFailure("Improper token has been passed.")

    with pytest.raises(SystemExit):
        bootstrap.run_bot(FakeBot(config), run=always_fail,
                          login_failure=FakeLoginFailure, env={},
                          stdin_is_tty=True, prompt=prompter("unused"))

    assert bootstrap.TOKEN_CONFIG_KEY in capsys.readouterr().err
    # The stale value is left alone: clearing it would strand an operator who
    # merely revoked and is about to paste a fresh one into the same key.
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) == "stale-token"


# --------------------------------------------------------------------------
# 5. First-run superadmin
# --------------------------------------------------------------------------

def _app_info(owner_id=None, team_owner_id=None, team_owner_obj_id=None):
    team = None
    if team_owner_id is not None or team_owner_obj_id is not None:
        team = types.SimpleNamespace(
            owner_user_id=team_owner_id,
            owner=(types.SimpleNamespace(id=team_owner_obj_id)
                   if team_owner_obj_id is not None else None))
    owner = types.SimpleNamespace(id=owner_id) if owner_id is not None else None
    return types.SimpleNamespace(owner=owner, team=team)


def _bot_with_app_info(config, info=None, raises=None):
    bot = FakeBot(config)

    async def application_info():
        if raises is not None:
            raise raises
        return info

    bot.application_info = application_info
    return bot


def test_superadmin_bootstrap_grants_owner_when_list_empty():
    config = FakeConfig()
    bot = _bot_with_app_info(config, _app_info(owner_id=125839498150936576))

    granted = asyncio.run(bootstrap.bootstrap_superadmin(bot))

    assert granted == 125839498150936576
    assert config.get_global("superadmins") == [125839498150936576]


def test_superadmin_bootstrap_leaves_existing_deploys_untouched():
    """The empty-list gate is the whole safety property: an established
    deployment's superadmin list must never be rewritten."""
    config = FakeConfig({"superadmins": [111]})
    bot = _bot_with_app_info(config, _app_info(owner_id=999))

    granted = asyncio.run(bootstrap.bootstrap_superadmin(bot))

    assert granted is None
    assert config.get_global("superadmins") == [111]


def test_superadmin_bootstrap_uses_team_owner_for_team_apps():
    """A team-owned app's `owner` is the team pseudo-user, which is useless as
    a Discord user id — the human is the team's owner id."""
    config = FakeConfig()
    bot = _bot_with_app_info(
        config, _app_info(owner_id=555, team_owner_id=777))

    granted = asyncio.run(bootstrap.bootstrap_superadmin(bot))

    assert granted == 777
    assert config.get_global("superadmins") == [777]


def test_team_owner_id_read_from_a_REAL_discord_team_object():
    """Pin the attribute name against the actual library, not a hand-made
    double that would happily agree with a wrong guess.

    discord.py's Team parses the JSON key `owner_user_id` into the attribute
    `owner_id`. Reading the JSON name off the object yields None, and the
    `Team.owner` fallback is a `utils.get` over `members` that returns None
    when the owner isn't listed — so this mistake fails SILENTLY, leaving a
    team-owned app with no superadmin. A synthetic double cannot catch it.
    """
    from discord.team import Team

    team = Team(state=None, data={
        "id": "123", "name": "A Team", "icon": None,
        "owner_user_id": "777",
        "members": [],  # owner deliberately absent: the fallback can't save us
    })
    # Guard the premise: if discord.py ever renames this, fail loudly here.
    assert team.owner_id == 777
    assert getattr(team, "owner_user_id", None) is None
    assert team.owner is None

    info = types.SimpleNamespace(owner=types.SimpleNamespace(id=555), team=team)
    assert bootstrap._application_owner_id(info) == 777


def test_superadmin_bootstrap_loses_the_race_to_claimsuper():
    """The empty-list gate must hold at the moment of the WRITE. Cogs are
    loaded before on_ready, so a `!claimsuper` can land while
    application_info() is still in flight — that claim must survive."""
    config = FakeConfig()
    bot = FakeBot(config)

    async def application_info():
        # Somebody runs !claimsuper while we're awaiting the API.
        config.set_global("superadmins", [42])
        return _app_info(owner_id=999)

    bot.application_info = application_info

    granted = asyncio.run(bootstrap.bootstrap_superadmin(bot))

    assert granted is None
    assert config.get_global("superadmins") == [42]


def test_superadmin_bootstrap_falls_back_to_team_owner_object():
    config = FakeConfig()
    bot = _bot_with_app_info(config, _app_info(team_owner_obj_id=888))
    assert asyncio.run(bootstrap.bootstrap_superadmin(bot)) == 888


def test_superadmin_bootstrap_survives_application_info_failure():
    """A failed lookup must not take startup down — !claimsuper still works."""
    config = FakeConfig()
    bot = _bot_with_app_info(config, raises=RuntimeError("HTTP 503"))

    assert asyncio.run(bootstrap.bootstrap_superadmin(bot)) is None
    assert config.get_global("superadmins") is None


def test_superadmin_bootstrap_skips_when_owner_id_missing():
    config = FakeConfig()
    bot = _bot_with_app_info(config, _app_info())
    assert asyncio.run(bootstrap.bootstrap_superadmin(bot)) is None
    assert config.get_global("superadmins") is None


# --------------------------------------------------------------------------
# 6. Filesystem hardening
# --------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX modes; chmod is best-effort on Windows")
def test_config_store_is_owner_only_on_disk(tmp_path):
    """The store holds the Discord token, provider API keys and the MCP bearer
    token in plaintext, so 0700/0600 is the substitute for encryption."""
    config = Config(config_dir=str(tmp_path / "configs"))
    try:
        config.set_global("discord_token", "a-secret")
        config.flush()

        dir_mode = stat.S_IMODE(os.stat(config.config_dir).st_mode)
        assert dir_mode == DIR_MODE

        path = os.path.join(config.config_dir, "global.json")
        assert stat.S_IMODE(os.stat(path).st_mode) == FILE_MODE
    finally:
        config.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_existing_loose_config_dir_is_tightened(tmp_path):
    """Upgrading an existing install must fix its permissions, not only guard
    freshly created directories (os.makedirs ignores mode when it exists)."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir(mode=0o755)
    loose = config_dir / "global.json"
    loose.write_text("{}")
    os.chmod(loose, 0o644)

    config = Config(config_dir=str(config_dir))
    try:
        config.set_global("discord_token", "a-secret")
        config.flush()
        assert stat.S_IMODE(os.stat(config_dir).st_mode) == DIR_MODE
        assert stat.S_IMODE(os.stat(loose).st_mode) == FILE_MODE
    finally:
        config.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_secret_is_never_on_disk_under_a_loose_mode(tmp_path, monkeypatch):
    """The window, not just the end state.

    A `.tmp` surviving a previous crash keeps its old mode — O_CREAT's mode
    argument is ignored when the file already exists — so tightening *after*
    json.dump would leave the secret briefly world-readable. Asserting the
    landed file's mode can't catch that: the post-rename chmod fixes the end
    state either way. So this samples the temp file's mode from inside
    json.dump, i.e. at the exact moment the secret is hitting the disk.
    """
    import json as _json

    config_dir = tmp_path / "configs"
    config = Config(config_dir=str(config_dir))
    try:
        stale = config_dir / "global.json.tmp"
        stale.write_text("{}")
        os.chmod(stale, 0o644)  # crash leftover, world-readable

        observed = {}
        real_dump = _json.dump

        def spying_dump(obj, fp, **kw):
            # The mode the secret is written under, sampled mid-write.
            observed["mode"] = stat.S_IMODE(os.fstat(fp.fileno()).st_mode)
            return real_dump(obj, fp, **kw)

        monkeypatch.setattr("core.config.json.dump", spying_dump)

        config.set_global("discord_token", "a-secret")
        config.flush()

        assert observed["mode"] == FILE_MODE, (
            f"secret written under mode {observed['mode']:o}, not 0600")
        landed = config_dir / "global.json"
        assert stat.S_IMODE(os.stat(landed).st_mode) == FILE_MODE
        assert not stale.exists()  # consumed by the rename
    finally:
        config.shutdown()


def test_config_writes_stay_atomic(tmp_path):
    """Hardening must not have broken tmp+rename: no .tmp left behind, and the
    landed file is valid JSON with the value in it."""
    import json

    config = Config(config_dir=str(tmp_path / "configs"))
    try:
        config.set_global("k", {"nested": [1, 2, 3]})
        config.flush()
        files = sorted(os.listdir(config.config_dir))
        assert files == ["global.json"], f"stray temp file: {files}"
        with open(os.path.join(config.config_dir, "global.json")) as f:
            assert json.load(f)["k"] == {"nested": [1, 2, 3]}
    finally:
        config.shutdown()
