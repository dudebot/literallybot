import requests
import bs4
import discord
from discord.ext import commands
import os

class Danbooru(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.posted_danbooru = set()
        self.danbooru_base = "http://danbooru.donmai.us"
        #self.danbooru_base = "https://testbooru.donmai.us"

    @commands.command(name="danbooru")
    async def danbooru(self, ctx, *tags):
        """Fetch a random image from Danbooru based on tags."""
        # Validate tags
        if not tags:
            await ctx.send("Usage: !danbooru tag1 tag2 ...")
            return
        # Retrieve API key and login from environment
        api_key = os.getenv("DANBOORU_API_KEY")
        login = os.getenv("DANBOORU_LOGIN")
        # Build tag query string
        tag_string = "+".join(tags)
        url = f"{self.danbooru_base}/posts.json?tags={tag_string}&limit=100"
        if api_key and login:
            url += f"&login={login}&api_key={api_key}"
        try:
            response = requests.get(url)
            data = response.json()
        except Exception as e:
            await ctx.send("Error fetching from Danbooru API.")
            return
        # Get the first unposted image
        for post in data:
            post_id = post.get("id")
            if post_id not in self.posted_danbooru:
                self.posted_danbooru.add(post_id)
                file_url = post.get("file_url")
                if file_url:
                    await ctx.send(file_url)
                    return
        # No posts found; use the autocomplete endpoint for suggestions (HTML response)
        first_tag = tags[0]
        autocomplete_url = f"{self.danbooru_base}/autocomplete?search[query]={first_tag}&search[type]=tag_query"
        try:
            auto_resp = requests.get(autocomplete_url)
            html_content = auto_resp.text
            soup = bs4.BeautifulSoup(html_content, "html.parser")
            li_tags = soup.find_all("li", class_="ui-menu-item")
            suggestions = [li.get("data-autocomplete-value") for li in li_tags][:5]
        except Exception as e:
            suggestions = []
        if suggestions:
            suggestion_text = ", ".join(suggestions)
            await ctx.send(f"Did you mean `{suggestion_text}`?")
        else:
            await ctx.send("No new image found.")

async def setup(bot):
    await bot.add_cog(Danbooru(bot))
