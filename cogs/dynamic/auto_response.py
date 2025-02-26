from discord.ext import commands
import random

reddit = {}
reddit['yikes']=['have sex','rent free','seethe']
reddit['cope']=['have sex','yikes','cringe']
reddit['seethe']=['have sex','cope','rent free']
reddit['cringe']=['have sex','seethe','yikes']
reddit['rent free']=['have sex','cringe','cope']
reddit['have sex']=['touch grass']
reddit['touch grass']=['rent free','yikes','cope','seethe','cringe']
reddit['same']=['accurate']

def generate_karma(string):
    for key in reddit.keys():
        if string.lower().strip() == key:
            return random.choice(reddit[key])

class AutoResponse(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        response = generate_karma(message.content)
        if response:
            await message.channel.send(response)
    
async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(AutoResponse(bot))
