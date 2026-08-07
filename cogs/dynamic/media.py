import os
import glob
import subprocess
import discord
from discord.ext import commands
from discord import File, app_commands
import yt_dlp
import requests
from core.error_handler import register_error_whitelist_hook, unregister_error_whitelist_hook
from core.utils import is_admin


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
        if len(file_name) < 2:
            return False
        return any(f.startswith(file_name) for f in self._guild_files(ctx.guild))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.guild is None:
            return

        if message.content.startswith('!'):
            file_name = message.content[1:].lower()
            if len(file_name) < 2:
                return
            for file in self._guild_files(message.guild):
                if file.startswith(file_name):
                    await message.channel.send(
                        file=File(os.path.join(self._guild_dir(message.guild), file)))
                    return

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

    # ---- admin UI: single /media panel ----------------------------------

    @app_commands.command(
        name="media",
        description="Manage this server's media library (admin)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def media_panel(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True)
            return
        view = MediaView(self, interaction)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


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


class MediaView(discord.ui.View):
    """Single-invoker ephemeral panel: file list + Add / Delete (two-click)."""

    def __init__(self, cog: Media, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = interaction.guild
        self.invoker_id = interaction.user.id
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel isn't yours — run /media to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Panel expired — run /media again.", view=self)
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Media(bot))
