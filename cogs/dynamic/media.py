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
            media_dir = 'media/'
            for file in os.listdir(media_dir):
                if file.startswith(file_name):
                    await message.channel.send(file=File(os.path.join(media_dir, file)))
                    break

    @commands.command(name='addmedia', description='Download and save YouTube videos or direct file links as media files.')
    @commands.is_owner()
    async def addmedia(self, ctx, link: str, file_name: str):
        file_name = file_name.lower()
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

async def setup(bot):
    await bot.add_cog(Media(bot))
