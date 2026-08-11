"""Zero-friction first run: token resolution and superadmin bootstrap (#83).

The load-bearing piece is the **resolution chain**, not the stdin prompt. The
prompt is sugar for the human sitting at a terminal; the chain is what keeps
every hosting path (panel host, Docker, systemd, bare metal) working with the
same binary and no config file at all.

    1. DISCORD_TOKEN env var  — first-class forever, NEVER persisted to disk.
    2. `discord_token` global config value — the self-hosted steady state.
    3. Interactive getpass prompt — ONLY when stdin is a TTY, and the token is
       persisted ONLY after discord.py confirms the login.
    4. Neither, and no TTY — exit nonzero with per-platform instructions.
       Never block on a stdin that cannot answer (a panel console, a systemd
       unit, a Docker container) — that is a hang, not a prompt.

Why the env var is never written: a panel-supplied secret copied into a file
would surprise the operator (they set it in one place, it lives in two) and
doubles the leak surface for a credential they already rotate elsewhere.

Why the prompt verifies before it writes: a typo'd token that lands in
global.json turns a one-line fix into a "why does it fail with a token
configured" debugging session. `run_bot` below stages the candidate and lets
bot.py's `setup_hook` persist it only once discord.py's login has succeeded, so
the only tokens on disk are tokens that worked.
"""

import getpass
import logging
import os
import sys

logger = logging.getLogger(__name__)

#: Env var checked first, and never persisted.
TOKEN_ENV_VAR = "DISCORD_TOKEN"
#: Global config key holding a prompt-persisted token.
TOKEN_CONFIG_KEY = "discord_token"
#: How many typos we forgive at the interactive prompt before giving up.
MAX_PROMPT_ATTEMPTS = 3

#: Sources, in precedence order. Only SOURCE_PROMPT is ever persisted.
SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_PROMPT = "prompt"

PORTAL_INSTRUCTIONS = """
No Discord bot token found.

Get one in about a minute:
  1. Open https://discord.com/developers/applications and click New Application
  2. Open the Bot tab, then Reset Token, then Copy
  3. Paste it below (it will not be echoed)

The token is saved to configs/global.json (mode 0600) once it logs in
successfully, so you only do this once.
""".strip()

NO_TTY_INSTRUCTIONS = """
No Discord bot token found, and stdin is not a terminal — so there is nobody
to prompt. Supply the token one of these ways and start again:

  Linux / macOS shell:  DISCORD_TOKEN=your_token ./start.sh
  Windows (cmd):        set DISCORD_TOKEN=your_token && start.bat
  Windows (PowerShell): $env:DISCORD_TOKEN='your_token'; .\\start.bat
  Game/hosting panel:   add a DISCORD_TOKEN startup variable in the panel
                        (Pterodactyl/Pelican: Startup tab; most panels have
                        an equivalent environment-variable field)
  Docker:               docker run -e DISCORD_TOKEN=your_token ...
  systemd:              Environment="DISCORD_TOKEN=your_token" in the unit,
                        or an EnvironmentFile= pointing at a 0600 file

Or run the bot once from an interactive terminal and paste the token at the
prompt — it is then stored in configs/global.json and no env var is needed.

Get a token at https://discord.com/developers/applications (New Application
-> Bot -> Reset Token).
""".strip()


def resolve_token(config, *, stdin_is_tty=None, prompt=None, env=None):
    """Resolve a bot token, returning ``(token, source)``.

    ``source`` is one of ``SOURCE_ENV`` / ``SOURCE_CONFIG`` / ``SOURCE_PROMPT``
    and tells the caller whether the value may be persisted — only a prompted
    token may, and only after a verified login.

    Raises ``SystemExit`` (nonzero) when there is no token and no TTY to ask.
    All the environment-dependent bits are injectable so the chain is testable
    without a terminal, a network, or a real Discord token.
    """
    env = os.environ if env is None else env

    token = (env.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        logger.info("Discord token: using the %s environment variable "
                    "(not persisted).", TOKEN_ENV_VAR)
        return token, SOURCE_ENV

    stored = config.get_global(TOKEN_CONFIG_KEY)
    if isinstance(stored, str) and stored.strip():
        logger.info("Discord token: using the stored `%s` global config value.",
                    TOKEN_CONFIG_KEY)
        return stored.strip(), SOURCE_CONFIG

    if stdin_is_tty is None:
        stdin_is_tty = _stdin_is_a_tty()

    if not stdin_is_tty:
        # Deliberately print rather than log: this is the last thing an
        # operator sees in a panel console, and it must not be filtered by a
        # log level or buried in a file.
        print(NO_TTY_INSTRUCTIONS, file=sys.stderr)
        raise SystemExit(1)

    prompt = getpass.getpass if prompt is None else prompt
    print(PORTAL_INSTRUCTIONS)
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        entered = (prompt("Bot token: ") or "").strip()
        if entered:
            return entered, SOURCE_PROMPT
        remaining = MAX_PROMPT_ATTEMPTS - attempt
        if remaining:
            print(f"Empty token — try again ({remaining} attempt(s) left).",
                  file=sys.stderr)
    print("No token entered. Nothing was saved.", file=sys.stderr)
    raise SystemExit(1)


def _stdin_is_a_tty():
    """True when stdin can actually be prompted.

    Guarded because a detached/closed stdin (systemd with StandardInput=null,
    some panel wrappers) can make `isatty` raise rather than return False.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def persist_token(config, token):
    """Write a *verified* token to the global config store.

    There is no separate secrets.json (owner decision, #83): provider API keys
    already live in global.json as a documented accepted risk, and a second
    plaintext file is separation without a distinct threat model. The hardening
    that does the work is filesystem permissions — see `core.config.Config`,
    which lands the directory 0700 and every config file 0600.
    """
    config.set_global(TOKEN_CONFIG_KEY, token)
    config.flush()  # don't leave a first-run token in the 5s write buffer
    logger.info("Discord token verified and saved to global config under `%s`. "
                "Future starts need no token entry.", TOKEN_CONFIG_KEY)


def run_bot(bot, run=None, login_failure=None, **resolve_kwargs):
    """Resolve a token, run the bot, and re-prompt if a typed token is bad.

    The verification boundary is discord.py's own login: `bot.run(token)`
    raises `LoginFailure` before the gateway connects when the token is
    rejected, and reaches `setup_hook` only after the REST login succeeded.
    So the rule "never write an unverified token" is implemented as: stash the
    candidate on the bot, let `setup_hook` persist it once login is confirmed,
    and on `LoginFailure` write nothing and ask again.

    Only a `SOURCE_PROMPT` token is ever a persistence candidate — an env token
    stays out of the config store by design, and a config token is already
    there. Retry is likewise prompt-only: re-prompting after a bad *env* token
    would silently paper over the operator's broken panel variable.

    ``resolve_kwargs`` are forwarded to `resolve_token` (``env``, ``prompt``,
    ``stdin_is_tty``) so a test can drive the whole loop without a terminal.
    """
    if run is None:
        run = bot.run
    if login_failure is None:
        import discord
        login_failure = discord.LoginFailure

    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        token, source = resolve_token(bot.config, **resolve_kwargs)
        # Read by LiterallyBot.setup_hook; cleared once written so a
        # reconnect-triggered hook never re-persists.
        bot._pending_token = token if source == SOURCE_PROMPT else None
        try:
            run(token)
            return
        except login_failure:
            bot._pending_token = None
            if source != SOURCE_PROMPT:
                where = (f"the {TOKEN_ENV_VAR} environment variable"
                         if source == SOURCE_ENV
                         else f"the stored `{TOKEN_CONFIG_KEY}` global config value")
                print(f"\nDiscord rejected the token from {where}.\n"
                      f"It is wrong, revoked, or truncated — reset it in the "
                      f"Developer Portal and set it again.", file=sys.stderr)
                raise SystemExit(1)
            remaining = MAX_PROMPT_ATTEMPTS - attempt
            print("\nDiscord rejected that token — nothing was saved.",
                  file=sys.stderr)
            if not remaining:
                print("Out of attempts. Reset the token in the Developer "
                      "Portal and run again.", file=sys.stderr)
                raise SystemExit(1)
            print(f"Check for a truncated copy/paste and try again "
                  f"({remaining} attempt(s) left).", file=sys.stderr)


async def bootstrap_superadmin(bot):
    """Grant the application owner superadmin on a fresh install.

    Empty-list gate ONLY. An existing deployment always has a non-empty
    `superadmins` (someone ran `!claimsuper`), so this never touches it — the
    feature exists to delete the `!claimsuper` incantation for *new* installs,
    not to re-seat ownership on established ones.

    Team-owned applications report `application_info().team`, whose `owner_id`
    is the human to grant; `application_info().owner` on a team app is the
    team's pseudo-user and would be useless as a Discord id. See
    `_application_owner_id` — the attribute name there is a trap.
    """
    from core.utils import get_superadmins

    if get_superadmins(bot.config):
        return None

    try:
        info = await bot.application_info()
    except Exception as e:  # noqa: BLE001 - never let bootstrap kill startup
        logger.error("Could not fetch application info for superadmin "
                     "bootstrap: %s. Run !claimsuper instead.", e)
        return None

    owner_id = _application_owner_id(info)
    if owner_id is None:
        logger.error("Application info carried no usable owner id; skipping "
                     "superadmin bootstrap. Run !claimsuper instead.")
        return None

    # Re-check after the await. Cogs are loaded by setup_hook, so commands are
    # already dispatchable while application_info() is in flight — someone
    # racing `!claimsuper` in that window must not have their claim silently
    # overwritten. The empty-list gate is the safety property; it has to hold
    # at the moment of the WRITE, not merely when we started.
    if get_superadmins(bot.config):
        logger.info("Superadmins were claimed while application info was in "
                    "flight; leaving the existing list alone.")
        return None

    bot.config.set_global("superadmins", [owner_id])
    bot.config.flush()
    logger.info("First run: granted superadmin to the application owner "
                "(ID: %s). Use !addsuperadmin to add more.", owner_id)
    return owner_id


def _application_owner_id(info):
    """The human user id to grant, for both personal and team-owned apps.

    On a team-owned app `info.owner` is the team's pseudo-user and is useless
    as a Discord user id, so the team is checked first. discord.py's `Team`
    exposes the owner as **`owner_id`** — `owner_user_id` is only the raw JSON
    key it parses that from (discord/team.py: `self.owner_id = ...
    _get_as_snowflake(data, 'owner_user_id')`), so reading the JSON name off
    the object always yields None. Both are tried, cheaply, because getting
    this wrong fails *silently*: `Team.owner` is a `utils.get` over `members`
    and returns None when the owner isn't in that list, which would leave a
    team app with no superadmin and no error.
    """
    team = getattr(info, "team", None)
    if team is not None:
        for attr in ("owner_id", "owner_user_id"):
            team_owner = getattr(team, attr, None)
            if team_owner:
                return int(team_owner)
        # Last resort: the resolved TeamMember object.
        owner_id = getattr(getattr(team, "owner", None), "id", None)
        return int(owner_id) if owner_id else None

    owner_id = getattr(getattr(info, "owner", None), "id", None)
    return int(owner_id) if owner_id else None
