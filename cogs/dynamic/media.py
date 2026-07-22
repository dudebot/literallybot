import os
import glob
import subprocess
import discord
from discord.ext import commands
from discord import File
import yt_dlp
import requests
from core.error_handler import register_error_whitelist_hook, unregister_error_whitelist_hook
from core.utils import is_admin


def _format_size(num_bytes):
    """Human-readable file size for the delmedia confirmation embed."""
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB'):
        if size < 1024:
            return f'{num_bytes} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


class ConfirmDeleteView(discord.ui.View):
    """Confirm/Cancel buttons for !delmedia. Invoker-only, 60s timeout.

    Only the Confirm callback deletes anything, and it re-checks is_admin
    server-side — the button gate alone is cosmetic.
    """

    def __init__(self, ctx, file_path):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.file_path = file_path
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "These buttons aren't yours — run `!delmedia` yourself.",
                ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Delete confirmation expired — nothing was deleted.",
                    view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to do this.", ephemeral=True)
            return
        file_name = os.path.basename(self.file_path)
        self._disable_all()
        self.stop()
        try:
            os.remove(self.file_path)
        except OSError as e:
            await interaction.response.edit_message(
                content=f"Failed to delete `{file_name}`: {e}", embed=None, view=self)
            return
        await interaction.response.edit_message(
            content=f"Deleted `{file_name}`.", embed=None, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content="Deletion cancelled — nothing was deleted.", embed=None, view=self)

class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._media_dir = 'media/'
        register_error_whitelist_hook(self._is_media_command)

    def cog_unload(self):
        unregister_error_whitelist_hook(self._is_media_command)

    def _is_media_command(self, ctx, error):
        """Return True if the failed command matches a media file (suppress error)."""
        if not ctx.message.content.startswith('!'):
            return False
        file_name = ctx.message.content[1:].split()[0].lower()
        if len(file_name) < 2:
            return False
        try:
            for file in os.listdir(self._media_dir):
                if file.startswith(file_name):
                    return True
        except OSError:
            pass
        return False

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        if message.content.startswith('!'):
            file_name = message.content[1:].lower()
            if len(file_name) < 2:
                return
            for file in os.listdir(self._media_dir):
                if file.startswith(file_name):
                    await message.channel.send(file=File(os.path.join(self._media_dir, file)))
                    return

    def _cleanup_media_files(self, file_name):
        """Remove any media files matching the given base name, including temp files."""
        for pattern in [os.path.join(self._media_dir, f'{file_name}.*'),
                        os.path.join(self._media_dir, f'{file_name}_tmp.*')]:
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

    @commands.command(name='addmedia')
    @commands.check(is_admin)
    async def addmedia(self, ctx, link: str = None, file_name: str = None,
                       start_ms: int = None, end_ms: int = None):
        """Download and save YouTube videos or direct file links as media files.

        Usage: !addmedia <link> <filename> [start_ms] [end_ms]
        - start_ms: start from this point (optional, if alone = first N ms)
        - end_ms: end at this point (optional)

        Examples:
          !addmedia <url> clip           - full video
          !addmedia <url> clip 2000      - first 2 seconds
          !addmedia <url> clip 200 1700  - from 200ms to 1700ms (1500ms clip)
        """
        # Authorization is handled by @commands.check(is_admin) on the
        # decorator (shared gate: superadmins, per-guild admins list, and
        # Discord Administrator).
        if not link or not file_name:
            await ctx.send("Missing required arguments.\nUsage: `!addmedia <link> <filename> [start_ms] [end_ms]`")
            return

        file_name = file_name.lower()

        if len(file_name) < 2:
            await ctx.send("Filename must be at least 2 characters long.")
            return

        if start_ms is not None and start_ms < 0:
            await ctx.send("start_ms cannot be negative.")
            return

        if end_ms is not None and end_ms <= 0:
            await ctx.send("end_ms must be positive.")
            return

        if start_ms is not None and end_ms is not None and start_ms >= end_ms:
            await ctx.send("start_ms must be less than end_ms.")
            return

        # Check for prefix conflicts with existing files
        for existing in os.listdir(self._media_dir):
            existing_base = os.path.splitext(existing)[0]
            # New file would be shadowed by existing (existing is shorter prefix)
            if file_name.startswith(existing_base):
                await ctx.send(f"Conflict: `!{file_name}` would be captured by existing `{existing}`")
                return
            # New file would shadow existing (new is shorter prefix)
            if existing_base.startswith(file_name):
                await ctx.send(f"Conflict: `!{file_name}` would shadow existing `{existing}`")
                return

        # Check if it's a direct media URL
        clean_url = link.split('?')[0]
        direct_extensions = ('.mp4', '.ogg', '.webm', '.mp3')
        file_path = None

        # Clean up any existing files with this name before downloading
        self._cleanup_media_files(file_name)

        try:
            if clean_url.lower().endswith(direct_extensions):
                # Direct file download - extract extension from URL
                file_extension = clean_url.split('.')[-1].lower()
                file_path = os.path.join(self._media_dir, f'{file_name}.{file_extension}')

                with requests.get(link, stream=True) as response:
                    response.raise_for_status()
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
            else:
                # yt-dlp download - let it determine extension
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': os.path.join(self._media_dir, f'{file_name}.%(ext)s'),
                    'merge_output_format': 'mp4',
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])

                # Find what yt-dlp created (exclude temp files)
                matches = [f for f in glob.glob(os.path.join(self._media_dir, f'{file_name}.*'))
                           if '_tmp.' not in f]
                if not matches:
                    await ctx.send('Download appeared to succeed but no file was created.')
                    return
                file_path = matches[0]

            # Trim if requested
            if start_ms is not None or end_ms is not None:
                # If only start_ms provided, treat as "first N ms"
                if start_ms is not None and end_ms is None:
                    end_ms = start_ms
                    start_ms = 0

                if not self._trim_media(file_path, start_ms, end_ms):
                    self._cleanup_media_files(file_name)
                    await ctx.send('Failed to trim media file.')
                    return

            final_name = os.path.basename(file_path)
            await ctx.send(f'Media file {final_name} has been added.')

        except requests.RequestException as e:
            self._cleanup_media_files(file_name)
            await ctx.send(f'Failed to download the file: {e}')
        except yt_dlp.utils.DownloadError as e:
            self._cleanup_media_files(file_name)
            await ctx.send(f'Failed to download the video: {e}')
        except Exception as e:
            self._cleanup_media_files(file_name)
            await ctx.send(f'Unexpected error: {e}')

    @commands.command(name='delmedia')
    @commands.check(is_admin)
    async def delmedia(self, ctx, name: str = None):
        """Delete a media file after button confirmation.

        Usage: !delmedia <name>
        Matches the exact file name, with or without extension — never a
        prefix or substring (a computed delete target must be exact).
        """
        if not name:
            await ctx.send("Missing required argument.\nUsage: `!delmedia <name>`")
            return

        try:
            entries = os.listdir(self._media_dir)
        except OSError as e:
            await ctx.send(f'Failed to read media directory: {e}')
            return

        query = name.lower()
        matches = [f for f in entries
                   if f.lower() == query or os.path.splitext(f)[0].lower() == query]

        if not matches:
            near = [f for f in sorted(entries, key=str.lower) if query in f.lower()][:5]
            if near:
                listing = '\n'.join(near)
                await ctx.send(f'No exact match for `{name}`. Did you mean:\n{listing}\n'
                               'Nothing was deleted.')
            else:
                await ctx.send(f'No media file named `{name}` found. Nothing was deleted.')
            return

        if len(matches) > 1:
            listing = '\n'.join(sorted(matches, key=str.lower))
            await ctx.send(f'`{name}` matches multiple files:\n{listing}\n'
                           'Re-run with the full file name (including extension). '
                           'Nothing was deleted.')
            return

        file_name = matches[0]
        file_path = os.path.join(self._media_dir, file_name)
        try:
            size = _format_size(os.path.getsize(file_path))
        except OSError:
            size = 'unknown size'

        embed = discord.Embed(
            title='Delete media file?',
            description=f'`{file_name}` ({size})',
            color=discord.Color.red(),
        )
        view = ConfirmDeleteView(ctx, file_path)
        view.message = await ctx.send(embed=embed, view=view)

    @commands.command(name='listmedia')
    async def listmedia(self, ctx, prefix: str = None):
        """List available media files. Optionally filter by a starting prefix.

        Usage: !listmedia [prefix]
        """
        allowed = ('.mp4', '.ogg', '.webm', '.mp3')

        if not os.path.isdir(self._media_dir):
            await ctx.send('Media directory not found.')
            return

        try:
            entries = os.listdir(self._media_dir)
        except OSError as e:
            await ctx.send(f'Failed to read media directory: {e}')
            return

        files = [f for f in entries if f.lower().endswith(allowed)]
        # The !<name> trigger serves EVERY file in media/, not just the
        # allowlisted extensions — surface the rest so the two views agree.
        others = [f for f in entries if not f.lower().endswith(allowed)]

        if prefix:
            p = prefix.lower()
            files = [f for f in files if f.lower().startswith(p)]
            others = [f for f in others if f.lower().startswith(p)]

        files.sort(key=str.lower)
        others.sort(key=str.lower)

        if not files and not others:
            if prefix:
                await ctx.send(f'No media files found starting with "{prefix}".')
            else:
                await ctx.send('No media files found.')
            return

        # Chunk messages to avoid exceeding Discord limits (shared splitter —
        # the inline accumulator this replaces declared a 1900 limit but
        # compared against 2000)
        from core.utils import recursive_split
        header = f'Media files ({len(files)} total' + (f', filtered by "{prefix}"' if prefix else '') + '):\n'
        body = header + '\n'.join(files)
        if others:
            body += (f'\n\nOther files ({len(others)} — non-standard extension, '
                     'still served by `!<name>`):\n' + '\n'.join(others))
        for chunk in recursive_split(body, 2000):
            await ctx.send(chunk)

async def setup(bot):
    await bot.add_cog(Media(bot))
