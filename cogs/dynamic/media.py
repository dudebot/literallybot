import os
import discord
from discord.ext import commands
from discord import File
import yt_dlp
import requests

class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot:
            return

        if message.content.startswith('!'):
            file_name = message.content[1:].lower()
            if len(file_name) < 4:
                return
            media_dir = 'media/'
            for file in os.listdir(media_dir):
                if file.startswith(file_name):
                    await message.channel.send(file=File(os.path.join(media_dir, file)))
                    break

    @commands.command(name='addmedia')
    async def addmedia(self, ctx, link: str = None, file_name: str = None):
        """Download and save YouTube videos or direct file links as media files."""
        config = self.bot.config
        admin_ids = config.get(ctx, "admins", [])
        if not admin_ids or ctx.author.id not in admin_ids:
            await ctx.send("You do not have permission to use this command.")
            return

        if not link or not file_name:
            await ctx.send("Missing required arguments.\nUsage: `!addmedia <link> <filename>`")
            return
            
        file_name = file_name.lower()
        
        if not link or not link.strip():
            await ctx.send("Link cannot be empty.\nUsage: `!addmedia <link> <filename>`")
            return
            
        if len(file_name) < 4:
            await ctx.send("Filename must be at least 4 characters long.\nUsage: `!addmedia <link> <filename>`")
            return
            
        file_extension = link.split('.')[-1]
        file_path = f'media/{file_name}.{file_extension}'

        if link.endswith(('.mp4', '.ogg', '.webm', '.mp3')):
            try:
                response = requests.get(link)
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                await ctx.send(f'Media file {file_name} has been added.')
            except Exception as e:
                await ctx.send(f'Failed to download the file: {e}')
        else:
            ydl_opts = {
                'format': 'mp4',
                'outtmpl': file_path,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])
                await ctx.send(f'Media file {file_name}.mp4 has been added.')
            except yt_dlp.utils.DownloadError as e:
                await ctx.send(f'Failed to download the video: {e}')

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
