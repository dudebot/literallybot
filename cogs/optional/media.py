"""Per-guild media library.

Ops: `list_media` and `post_media` are cog-provided behavioral primitives
(see docs/cog-development.md). Both call the same services the `!<name>`
listener does (`_guild_files` / `_find_file` / `_post_file`), so an agent
posting a library item and a user typing `!poggers` resolve the name by the
identical prefix rule.

`post_media` deliberately SENDS rather than returning a host path: the
library is public (any member can post any item with `!<name>`), whereas
handing back a filesystem path would be the admin-gated attachment surface
in disguise (`_require_admin_for_attachments` in core/ops.py).
"""
import os
import glob
import subprocess
import discord
from discord.ext import commands
from discord import File, app_commands
import yt_dlp
import requests
from core.error_handler import register_error_whitelist_hook, unregister_error_whitelist_hook
from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op
from core.utils import InvokerOnlyView, is_admin


def _serialize_media_list(result: dict) -> dict:
    """Wire payload for `list_media`. Names only — the host paths behind them
    are deliberately not exposed (see the module docstring)."""
    names = list(result.get("names") or [])
    return {"names": names, "count": len(names)}


def _serialize_posted_media(result: dict) -> dict:
    """Wire payload for `post_media`. `name` is what actually matched (the
    caller may have passed a prefix), and message_id travels as a string for
    the 2**53 reason ids do (see core/ops.py)."""
    return {"status": result["status"], "name": result["name"],
            "message_id": str(result["message_id"])}


def _format_size(num_bytes):
    """Human-readable file size for the panel listing."""
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB'):
        if size < 1024:
            return f'{num_bytes} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'

class Media(commands.Cog):
    """Per-guild media library: `!<name>` posts the guild's file of that
    name. Each guild's files live in media/<guild_id>/ (runtime data, not
    in git) — libraries never bleed across guilds, and DMs have none."""

    def __init__(self, bot):
        self.bot = bot
        register_error_whitelist_hook(self._is_media_command)

    def cog_unload(self):
        unregister_error_whitelist_hook(self._is_media_command)

    @staticmethod
    def _guild_dir(guild):
        return os.path.join('media', str(guild.id))

    def _guild_files(self, guild):
        """Filenames in the guild's media dir; [] when absent (a guild that
        never ran !addmedia has no library, which must read as empty, not
        as an error)."""
        try:
            return os.listdir(self._guild_dir(guild))
        except OSError:
            return []

    def _is_media_command(self, ctx, error):
        """Return True if the failed command matches a media file in the
        invoking guild (suppress the CommandNotFound error)."""
        if ctx.guild is None or not ctx.message.content.startswith('!'):
            return False
        file_name = ctx.message.content[1:].split()[0].lower()
        return self._find_file(ctx.guild, file_name) is not None

    # --- services ---------------------------------------------------------
    #
    # Headless logic shared by the `!<name>` listener and the cog ops. Plain
    # objects in, plain data out; the ops never touch an Interaction.

    def _find_file(self, guild, name):
        """Filename in this guild's library matching `name` by prefix, or
        None. The 2-character floor is what stops `!a` sweeping the library
        (and it also keeps `!` itself inert).

        Deliberately does NOT strip: the `!<name>` listener never did, so
        `!pog ` with a trailing space is inert and stays inert. The op strips
        its own argument before calling in, where a stray space is a caller
        typo rather than a message the user chose to send."""
        name = str(name).lower()
        if len(name) < 2:
            return None
        for file in self._guild_files(guild):
            if file.startswith(name):
                return file
        return None

    async def _post_file(self, guild, channel, name):
        """Send the library item matching `name` into `channel`.

        Returns {"status": "posted", "name": <matched filename>,
        "message_id": int}. Raises ValueError when nothing matches — the
        library is per guild, so a name from another guild is simply absent.
        """
        file = self._find_file(guild, name)
        if file is None:
            raise ValueError(
                f"No media file matching '{name}' in this server's library.")
        sent = await channel.send(
            file=File(os.path.join(self._guild_dir(guild), file)))
        return {"status": "posted", "name": file,
                "message_id": getattr(sent, "id", 0)}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.guild is None:
            return

        if message.content.startswith('!'):
            # Branch on _find_file rather than catching _post_file's ValueError:
            # a `!` message that names nothing is simply not a media command
            # (every other command in the bot starts the same way), while a
            # ValueError raised by File()/channel.send() is a real failure that
            # must keep reaching the error handler.
            if self._find_file(message.guild, message.content[1:]) is None:
                return
            await self._post_file(message.guild, message.channel,
                                  message.content[1:])

    def _cleanup_media_files(self, media_dir, file_name):
        """Remove any media files matching the given base name, including temp files."""
        for pattern in [os.path.join(media_dir, f'{file_name}.*'),
                        os.path.join(media_dir, f'{file_name}_tmp.*')]:
            for f in glob.glob(pattern):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _trim_media(self, file_path, start_ms, end_ms):
        """Trim media file in place using ffmpeg. Returns True on success.

        Args:
            file_path: Path to the media file
            start_ms: Where to start from
            end_ms: Where to end
        """
        base, ext = os.path.splitext(file_path)
        temp_path = f'{base}_tmp{ext}'
        duration_ms = end_ms - start_ms

        cmd = ['ffmpeg', '-y']

        if start_ms > 0:
            cmd.extend(['-ss', str(start_ms / 1000)])

        cmd.extend(['-i', file_path, '-t', str(duration_ms / 1000),
                    '-c:v', 'libx264', '-c:a', 'aac', temp_path])

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

        os.replace(temp_path, file_path)
        return True

    def _do_addmedia(self, guild, link, file_name, start_ms, end_ms):
        """Download (and optionally trim) a file into the guild's library.

        Synchronous — the panel runs it via asyncio.to_thread so a slow
        yt-dlp download can't stall the event loop. Returns the result
        message shown in the panel."""
        file_name = file_name.lower()

        if len(file_name) < 2:
            return "Filename must be at least 2 characters long."
        if start_ms is not None and start_ms < 0:
            return "start_ms cannot be negative."
        if end_ms is not None and end_ms <= 0:
            return "end_ms must be positive."
        if start_ms is not None and end_ms is not None and start_ms >= end_ms:
            return "start_ms must be less than end_ms."

        media_dir = self._guild_dir(guild)
        os.makedirs(media_dir, exist_ok=True)

        # Check for prefix conflicts with existing files (within this guild)
        for existing in os.listdir(media_dir):
            existing_base = os.path.splitext(existing)[0]
            # New file would be shadowed by existing (existing is shorter prefix)
            if file_name.startswith(existing_base):
                return f"Conflict: `!{file_name}` would be captured by existing `{existing}`"
            # New file would shadow existing (new is shorter prefix)
            if existing_base.startswith(file_name):
                return f"Conflict: `!{file_name}` would shadow existing `{existing}`"

        # Check if it's a direct media URL
        clean_url = link.split('?')[0]
        direct_extensions = ('.mp4', '.ogg', '.webm', '.mp3')
        file_path = None

        # Clean up any existing files with this name before downloading
        self._cleanup_media_files(media_dir, file_name)

        try:
            if clean_url.lower().endswith(direct_extensions):
                # Direct file download - extract extension from URL
                file_extension = clean_url.split('.')[-1].lower()
                file_path = os.path.join(media_dir, f'{file_name}.{file_extension}')

                with requests.get(link, stream=True) as response:
                    response.raise_for_status()
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
            else:
                # yt-dlp download - let it determine extension
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': os.path.join(media_dir, f'{file_name}.%(ext)s'),
                    'merge_output_format': 'mp4',
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])

                # Find what yt-dlp created (exclude temp files)
                matches = [f for f in glob.glob(os.path.join(media_dir, f'{file_name}.*'))
                           if '_tmp.' not in f]
                if not matches:
                    return 'Download appeared to succeed but no file was created.'
                file_path = matches[0]

            # Trim if requested
            if start_ms is not None or end_ms is not None:
                # If only start_ms provided, treat as "first N ms"
                if start_ms is not None and end_ms is None:
                    end_ms = start_ms
                    start_ms = 0

                if not self._trim_media(file_path, start_ms, end_ms):
                    self._cleanup_media_files(media_dir, file_name)
                    return 'Failed to trim media file.'

            final_name = os.path.basename(file_path)
            return f'Media file {final_name} has been added — post it with `!{file_name}`.'

        except requests.RequestException as e:
            self._cleanup_media_files(media_dir, file_name)
            return f'Failed to download the file: {e}'
        except yt_dlp.utils.DownloadError as e:
            self._cleanup_media_files(media_dir, file_name)
            return f'Failed to download the video: {e}'
        except Exception as e:
            self._cleanup_media_files(media_dir, file_name)
            return f'Unexpected error: {e}'

    # ---- admin UI -------------------------------------------------------

    @app_commands.command(name="media",
                          description="Open the media-library panel")
    @app_commands.guild_only()
    @app_commands.check(is_admin)
    async def media_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!media` (#76). The real gate is
        `@app_commands.check(is_admin)` — same predicate as `!media`."""
        if not is_admin(interaction):
            await interaction.response.send_message(
                "Requires admin.", ephemeral=True)
            return
        view = MediaView(self, interaction.user, interaction.guild)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = None

    @commands.command(name="media", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def media_panel(self, ctx):
        """Open the media-library panel. Posts PUBLICLY — use /media for a
        private one (#76)."""
        view = MediaView(self, ctx.author, ctx.guild)
        view.message = await ctx.send(embed=view.render_embed(), view=view)

    # --- ops --------------------------------------------------------------
    #
    # Registered against the live cog instance by LiterallyBot.add_cog and
    # dropped on unload. EVERYONE, matching `!<name>`: the library is a
    # public surface, and adding/deleting items stays behind the admin panel.

    @op(
        "list_media",
        "List the names of the media files in this guild's library. Any of "
        "them can be posted with post_media (or by a user typing !<name>). "
        "Returns names only, not file paths, and does not post anything.",
        PermissionLevel.EVERYONE,
        serialize=_serialize_media_list,
        agent_guidance=(
            "The library is per guild — a name from another server is not "
            "here. Adding or deleting library files is not available as a "
            "tool; that is the admin `!media` panel."),
        scope=OpScope.GUILD,
        group="media",
        group_label="Media library",
    )
    async def op_list_media(self, ctx) -> dict:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            raise ValueError("list_media must be called in a guild.")
        return {"names": sorted(self._guild_files(guild), key=str.lower)}

    @op(
        "post_media",
        "Post a file from this guild's media library into a channel. The name "
        "matches by prefix, the same way !<name> does, and must be at least 2 "
        "characters. This SENDS the file — do not also send_message with it.",
        PermissionLevel.EVERYONE,
        params=[
            OpParam("channel", ParamKind.CHANNEL, "Channel to post the file in."),
            OpParam("name", ParamKind.STRING,
                    "Library item name, or a prefix of it (as with !<name>)."),
        ],
        serialize=_serialize_posted_media,
        agent_guidance=(
            "This posts the file itself; the returned name is the item that "
            "actually matched, which may be longer than the prefix you asked "
            "for. Call list_media first if unsure what exists — a name that "
            "matches nothing is an error, not an empty post."),
        scope=OpScope.GUILD,
        group="media",
        group_label="Media library",
    )
    async def op_post_media(self, ctx, channel, name: str) -> dict:
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("post_media requires a guild channel.")
        return await self._post_file(guild, channel, str(name).strip())


class _AddMediaModal(discord.ui.Modal, title="Add media file"):
    def __init__(self, panel: "MediaView"):
        super().__init__()
        self._panel = panel
        self.link = discord.ui.TextInput(
            label="URL (YouTube or direct file link)", required=True, max_length=400)
        self.file_name = discord.ui.TextInput(
            label="Name (posted with !<name>)", required=True, max_length=64)
        self.trim = discord.ui.TextInput(
            label="Trim ms: end, or start-end (blank = full)",
            required=False, max_length=20, placeholder="2000  or  200-1700")
        for item in (self.link, self.file_name, self.trim):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        start_ms = end_ms = None
        raw = str(self.trim.value).strip()
        if raw:
            try:
                if "-" in raw:
                    start_str, end_str = raw.split("-", 1)
                    start_ms, end_ms = int(start_str), int(end_str)
                else:
                    end_ms = int(raw)
                    start_ms = 0
            except ValueError:
                self._panel.flash("⚠ Trim must be `end` or `start-end` in ms — nothing added.")
                await self._panel.rerender(interaction)
                return
        # Download can take a while: defer, run off-loop, then re-render.
        await interaction.response.defer()
        import asyncio
        result = await asyncio.to_thread(
            self._panel.cog._do_addmedia, self._panel.guild,
            str(self.link.value).strip(), str(self.file_name.value).strip(),
            start_ms, end_ms)
        self._panel.flash(result)
        await self._panel.rerender(interaction)


class _FileSelect(discord.ui.Select):
    def __init__(self, panel: "MediaView"):
        self._panel = panel
        files = sorted(panel.files(), key=str.lower)
        options = [
            discord.SelectOption(label=f[:100], value=f,
                                 default=(f == panel.selected))
            for f in files[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="(no files)", value="_none")]
        placeholder = "Select a file to delete"
        if len(files) > 25:
            placeholder += f" (first 25 of {len(files)})"
        super().__init__(placeholder=placeholder, min_values=0, max_values=1,
                         options=options, disabled=not files, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = self.values[0] if self.values else None
        self._panel.confirming = False
        await self._panel.rerender(interaction)


class MediaView(InvokerOnlyView, discord.ui.View):
    """Single-invoker ephemeral panel: file list + Add / Delete (two-click)."""

    panel_command = "!media"

    def __init__(self, cog: Media, user, guild):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.invoker_id = user.id
        self.selected = None
        self.confirming = False   # armed delete: next click actually deletes
        self.message = None
        self._flash = None
        self._build()

    def files(self):
        return self.cog._guild_files(self.guild)

    def flash(self, text):
        self._flash = text

    def _build(self):
        self.clear_items()
        files = self.files()
        if self.selected not in files:
            self.selected = None
            self.confirming = False
        self.add_item(_FileSelect(self))
        add_btn = discord.ui.Button(label="➕ Add", style=discord.ButtonStyle.primary, row=1)
        del_label = "⚠ Confirm delete" if self.confirming else "🗑 Delete"
        del_btn = discord.ui.Button(label=del_label, style=discord.ButtonStyle.danger,
                                    row=1, disabled=self.selected is None)

        async def add_cb(interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            await interaction.response.send_modal(_AddMediaModal(self))

        async def del_cb(interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            if self.selected is None:
                await interaction.response.send_message("Select a file first.", ephemeral=True)
                return
            if not self.confirming:
                # First click arms; the relabeled button makes the second
                # click an explicit confirmation (deletes are unrecoverable).
                self.confirming = True
                self.flash(f"Click **⚠ Confirm delete** to remove `{self.selected}`.")
                await self.rerender(interaction)
                return
            target = self.selected
            path = os.path.join(self.cog._guild_dir(self.guild), target)
            try:
                os.remove(path)
                self.flash(f"Deleted `{target}`.")
            except OSError as e:
                self.flash(f"Failed to delete `{target}`: {e}")
            self.selected = None
            self.confirming = False
            await self.rerender(interaction)

        add_btn.callback = add_cb
        del_btn.callback = del_cb
        self.add_item(add_btn)
        self.add_item(del_btn)

    def render_embed(self):
        files = sorted(self.files(), key=str.lower)
        e = discord.Embed(
            title="Media library",
            description=(f"**{self.guild.name}** — any file posts with `!<name>` "
                         "(prefix match, e.g. `!pog` hits poggers.mp4)."),
            color=discord.Color.blurple(),
        )
        if files:
            lines = []
            for f in files:
                try:
                    size = _format_size(os.path.getsize(
                        os.path.join(self.cog._guild_dir(self.guild), f)))
                except OSError:
                    size = "?"
                lines.append(f"`{f}` — {size}")
            body = "\n".join(lines)
            e.add_field(name=f"Files ({len(files)})",
                        value=body[:1010] + ("\n… (more)" if len(body) > 1010 else ""),
                        inline=False)
        else:
            e.add_field(name="Files", value="*none — this server's library is empty*",
                        inline=False)
        if self._flash:
            e.add_field(name="Last action", value=self._flash[:1024], inline=False)
            self._flash = None
        e.set_footer(text="Panel expires after 3 minutes of inactivity.")
        return e

    async def rerender(self, interaction: discord.Interaction):
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.render_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.render_embed(), view=self)


async def setup(bot):
    await bot.add_cog(Media(bot))
