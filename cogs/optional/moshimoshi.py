"""!moshimoshi: push a Discord message to the MCP clients connected to the bot.

Proof of concept. `!moshimoshi` lists live MCP sessions; `!moshimoshi <msg>`
pushes the message to each one as a `notifications/message` (see
core/mcp_clients.py) and reports per-client delivery. Whether a client shows
a server-pushed notification to its model is the client\x27s choice.

Optional fallback (`moshimoshi_spawn_fallback` = "grok" or "codex"): when no
client took the push, run that agent headless once in
`moshimoshi_workdir` and post its reply. Off by default.

Gate: the author id must be in global `moshimoshi_allowed_users`
(default: dudebot only). Superadmin alone is not enough.
"""
import asyncio
import io
import os
import tempfile

import discord
from discord.ext import commands

from core.mcp_clients import registry as mcp_clients

DEFAULT_ALLOWED = [125839498150936576]
SPAWN_TIMEOUT = 600
_HOME = os.path.expanduser("~")


def allowed(config, user_id: int) -> bool:
    if not config.get_global("moshimoshi_enabled", True):
        return False
    users = config.get_global("moshimoshi_allowed_users", DEFAULT_ALLOWED) or []
    return int(user_id) in {int(u) for u in users}


def spawn_argv(target: str, prompt_file: str):
    if target == "grok":
        return [f"{_HOME}/.local/bin/grok", "--agent", "evelyn", "--always-approve",
                "--permission-mode", "bypassPermissions", "--output-format", "plain",
                "--prompt-file", prompt_file, "--max-turns", "20"]
    if target == "codex":
        return [f"{_HOME}/.local/bin/codex", "exec", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-o", prompt_file + ".out",
                "-"]
    raise ValueError(f"unknown spawn target {target!r}")


async def run_agent(target: str, message: str, workdir: str,
                    timeout: int = SPAWN_TIMEOUT) -> "tuple[bool, str]":
    """Run one headless agent turn. Returns (ok, text)."""
    os.makedirs(workdir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pf = os.path.join(tmp, "prompt.txt")
        with open(pf, "w") as f:
            f.write(message)
        argv = spawn_argv(target, pf)
        stdin = open(pf, "rb") if target == "codex" else asyncio.subprocess.DEVNULL
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, stdin=stdin,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False, f"{target} timed out after {timeout}s"
        finally:
            if stdin is not asyncio.subprocess.DEVNULL:
                stdin.close()
        text = out.decode(errors="replace").strip()
        if target == "codex" and os.path.exists(pf + ".out"):
            text = open(pf + ".out", errors="replace").read().strip()
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[-800:]
            return False, f"{target} exited {proc.returncode}: {tail or text[-800:]}"
        return True, text or "(empty reply)"


class MoshiMoshi(commands.Cog):
    """Wake the MCP clients connected to this bot."""

    def __init__(self, bot):
        self.bot = bot
        self._busy = False

    @commands.command(name="moshimoshi")
    async def moshimoshi(self, ctx, *, message: str = ""):
        """List connected MCP clients, or push a message to them."""
        if not allowed(self.bot.config, ctx.author.id):
            return  # silent: do not advertise the command
        clients = mcp_clients.live()
        if not message.strip():
            if not clients:
                await ctx.send("No MCP clients connected.")
                return
            lines = [f"`{c.name}` {c.version} session `{c.session_id[:8]}` "
                     f"proto {c.protocol_version}, last seen <t:{int(c.last_seen)}:R>"
                     for c in clients]
            await ctx.send("Connected MCP clients:\n" + "\n".join(lines))
            return
        author = f"{ctx.author} ({ctx.author.id})"
        results = await mcp_clients.push(message, author)
        sent = [c for c, s in results if s == "sent"]
        report = [f"`{c.name}` `{c.session_id[:8]}`: {s}" for c, s in results]
        await ctx.send(("Pushed to:\n" + "\n".join(report)) if report
                       else "No MCP clients connected.")
        fallback = self.bot.config.get_global("moshimoshi_spawn_fallback")
        if sent or not fallback:
            return
        if self._busy:
            await ctx.send(f"{fallback} is already running a request.")
            return
        workdir = os.path.expanduser(self.bot.config.get_global(
            "moshimoshi_workdir", "~/moshimoshi-workspace"))
        self._busy = True
        try:
            async with ctx.typing():
                ok, text = await run_agent(fallback, message, workdir)
        finally:
            self._busy = False
        prefix = f"**{fallback}**" + ("" if ok else " (failed)") + ":\n"
        if len(prefix) + len(text) <= 2000:
            await ctx.send(prefix + text)
        else:
            await ctx.send(prefix + "(reply attached)", file=discord.File(
                io.BytesIO(text.encode()), filename=f"{fallback}-reply.txt"))


async def setup(bot):
    await bot.add_cog(MoshiMoshi(bot))
