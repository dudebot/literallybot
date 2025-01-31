import os
import discord
from discord.ext import commands
from discord import File
import yt_dlp

class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        if message.content.startswith('!'):
            file_name = message.content[1:].lower()
            file_path = f'media/{file_name}.mp4'
            if os.path.exists(file_path):
                await message.channel.send(file=File(file_path))

    @commands.command(name='addmedia', description='Download and save YouTube videos as mp4 files.')
    @commands.is_owner()
    async def addmedia(self, ctx, youtube_link: str, file_name: str):
        file_name = file_name.lower()
        ydl_opts = {
            'format': 'mp4',
            'outtmpl': f'media/{file_name}.mp4',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_link])
        await ctx.send(f'Media file {file_name}.mp4 has been added.')

async def setup(bot):
    await bot.add_cog(Media(bot))
