import os
from discord.ext import commands
import discord
import random

class MusicPlayer(commands.Cog):
    """This is a cog with commands to play music via local mp3 files or audio stream. 
    """
    def __init__(self, bot):
        self.bot = bot
        self.queue = []  # P418f

    @commands.command(name='play', aliases=['p'])
    async def play(self, ctx, *, url: str):
        """Plays a local mp3 file or a audio stream.
        
        Args:
            self
            ctx
            url (str): The url of the audio file or stream.
        """
        # Check if the user is in a voice channel
        if not ctx.author.voice:
            return await ctx.send('You are not connected to a voice channel.')
        
        # Check if the bot is in a voice channel
        if ctx.guild.voice_client is None:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
        
        self.queue.append(url)  # P418f
        if not ctx.guild.voice_client.is_playing():
            ctx.guild.voice_client.play(discord.FFmpegPCMAudio(url))
            await ctx.send(f'Now playing: {url}')
        else:
            await ctx.send(f'Added to queue: {url}')

    @commands.command(name='stop',)
    async def stop(self, ctx):
        """Stops the audio stream.
        
        Args:
            self
            ctx
        """
        # Check if the bot is in a voice channel
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        
        ctx.guild.voice_client.stop()
        await ctx.send('Audio stream stopped.')

    @commands.command(name='pause')
    async def pause(self, ctx):
        """Pauses the audio stream.
        
        Args:
            self
            ctx
        """
        # Check if the bot is in a voice channel
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        elif ctx.guild.voice_client.is_paused():
            return await ctx.send('Audio stream is already paused.')
        else:
            ctx.guild.voice_client.pause()
            await ctx.send('Audio stream paused.')

    @commands.command(name='resume')
    async def resume(self, ctx):
        """Resumes the audio stream.
        
        Args:
            self
            ctx
        """
        # Check if the bot is in a voice channel
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        elif ctx.guild.voice_client.is_playing():
            return await ctx.send('Audio stream is already playing.')
        else:
            ctx.guild.voice_client.resume()
            await ctx.send('Audio stream resumed.')

    @commands.command(name='skip')
    async def skip(self, ctx):
        """Skips the current audio stream.
        
        Args:
            self
            ctx
        """
        # Check if the bot is in a voice channel
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        else:
            ctx.guild.voice_client.stop()
            if self.queue:
                next_url = self.queue.pop(0)
                ctx.guild.voice_client.play(discord.FFmpegPCMAudio(next_url))
                await ctx.send(f'Now playing: {next_url}')
            else:
                await ctx.send('Queue is empty.')

    @commands.command(name='queue')
    async def queue(self, ctx):
        """Shows the current audio stream queue.
        
        Args:
            self
            ctx
        """
        if not self.queue:
            await ctx.send('The queue is empty.')
        else:
            queue_list = '\n'.join(self.queue)
            await ctx.send(f'Current queue:\n{queue_list}')

    @commands.command(name='volume')
    async def volume(self, ctx, volume: int):
        """Sets the volume of the audio stream.
        
        Args:
            self
            ctx
            volume (int): The volume level.
        """
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        ctx.guild.voice_client.source.volume = volume / 100
        await ctx.send(f'Volume set to {volume}%')

    @commands.command(name='loop')
    async def loop(self, ctx):
        """Loops the current audio stream.
        
        Args:
            self
            ctx
        """
        if ctx.guild.voice_client is None:
            return await ctx.send('I am not connected to a voice channel.')
        ctx.guild.voice_client.loop = not ctx.guild.voice_client.loop
        await ctx.send(f'Looping is now {"enabled" if ctx.guild.voice_client.loop else "disabled"}.')

    @commands.command(name='shuffle')
    async def shuffle(self, ctx):
        """Shuffles the current audio stream queue.
        
        Args:
            self
            ctx
        """
        if not self.queue:
            await ctx.send('The queue is empty.')
        else:
            random.shuffle(self.queue)
            await ctx.send('Queue shuffled.')

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(MusicPlayer(bot))
