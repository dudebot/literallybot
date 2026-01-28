import os
import glob
import subprocess
import discord
from discord.ext import commands
from discord import File
import yt_dlp
import requests
from core.error_handler import register_error_whitelist_hook, unregister_error_whitelist_hook

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
        if message.author == self.bot:
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
        for pattern in [f'media/{file_name}.*', f'media/{file_name}_tmp.*']:
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
        config = self.bot.config
        admin_ids = config.get(ctx, "admins", [])
        if not admin_ids or ctx.author.id not in admin_ids:
            await ctx.send("You do not have permission to use this command.")
            return

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
                file_path = f'media/{file_name}.{file_extension}'

                with requests.get(link, stream=True) as response:
                    response.raise_for_status()
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
            else:
                # yt-dlp download - let it determine extension
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': f'media/{file_name}.%(ext)s',
                    'merge_output_format': 'mp4',
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])

                # Find what yt-dlp created (exclude temp files)
                matches = [f for f in glob.glob(f'media/{file_name}.*')
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

    @commands.command(name='listmedia')
    async def listmedia(self, ctx, prefix: str = None):
        """List available media files. Optionally filter by a starting prefix.

        Usage: !listmedia [prefix]
        """
        media_dir = 'media/'
        allowed = ('.mp4', '.ogg', '.webm', '.mp3')

        if not os.path.isdir(media_dir):
            await ctx.send('Media directory not found.')
            return

        try:
            files = [f for f in os.listdir(media_dir) if f.lower().endswith(allowed)]
        except OSError as e:
            await ctx.send(f'Failed to read media directory: {e}')
            return

        if prefix:
            p = prefix.lower()
            files = [f for f in files if f.lower().startswith(p)]

        files.sort(key=str.lower)

        if not files:
            if prefix:
                await ctx.send(f'No media files found starting with "{prefix}".')
            else:
                await ctx.send('No media files found.')
            return

        # Chunk messages to avoid exceeding Discord limits
        header = f'Media files ({len(files)} total' + (f', filtered by "{prefix}"' if prefix else '') + '):\n'
        chunk_limit = 1900  # leave room for header/formatting
        current = header
        lines = []
        for name in files:
            line = name + '\n'
            if len(current) + len(line) > 2000:
                await ctx.send(current.rstrip('\n'))
                current = ''
            current += line
        if current:
            await ctx.send(current.rstrip('\n'))

async def setup(bot):
    await bot.add_cog(Media(bot))
