"""Danbooru image search.

The rating policy lives in `search()` — the ONE service both the `!danbooru`
command and the `search_danbooru` op call. In any channel that is not marked
NSFW (and in DMs, which have no NSFW flag at all), user-supplied `rating:`
tags are stripped and `rating:general` is forced. Keeping the policy inside the
service is the point: a caller cannot obtain unrated results by forgetting to
reimplement the check, because there is no path to the API that bypasses it.

Ops: `search_danbooru` returns the post URL plus that post's tags, score,
and favorite count; it does NOT post to Discord. The caller decides what
to do with the result (issue #64 — a tool that needs `ctx.send` is not
headless).
"""
import asyncio
import os

import bs4
import requests
from discord.ext import commands

from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op

# Danbooru's Cloudflare rejects the default python-requests User-Agent (and
# spoofed browser UAs) with an HTML challenge page; an honest bot UA passes.
REQUEST_HEADERS = {"User-Agent": "literallybot/1.0 (Discord bot)"}


def _channel_is_nsfw(channel) -> bool:
    """DMs and other channels with no NSFW concept count as NOT NSFW, so the
    safe-rating policy applies to them."""
    is_nsfw = getattr(channel, "is_nsfw", None)
    return bool(is_nsfw()) if callable(is_nsfw) else False


def apply_rating_policy(tags: list, channel) -> list:
    """Force rating:general outside NSFW channels, stripping any user-supplied
    rating: tag so it can't be overridden with e.g. rating:explicit."""
    if _channel_is_nsfw(channel):
        return list(tags)
    return [t for t in tags if not t.lower().startswith("rating:")] + ["rating:general"]


def _serialize_search(result: dict) -> dict:
    """Wire payload for `search_danbooru`. `status` always travels — the agent
    guidance tells the model to branch on it, and 'no_results' with
    `suggestions` is a useful outcome, not an error. post_id is a snowflake-ish
    int id, so it goes out as a string for the same 2**53 JSON reason ids do."""
    payload = {"status": result.get("status"), "tags": result.get("tags") or []}
    if result.get("url"):
        payload["url"] = result["url"]
    if result.get("post_id") is not None:
        payload["post_id"] = str(result["post_id"])
    if result.get("score") is not None:
        payload["score"] = int(result["score"])
    if result.get("favs") is not None:
        payload["favs"] = int(result["favs"])
    if result.get("suggestions"):
        payload["suggestions"] = list(result["suggestions"])
    if result.get("message"):
        payload["message"] = result["message"]
    return payload


class Danbooru(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.posted_danbooru = set()
        self.danbooru_base = "https://danbooru.donmai.us"

    # --- service --------------------------------------------------------------

    async def search(self, tags: list, channel) -> dict:
        """Find one not-yet-posted image for `tags`, subject to the rating
        policy for `channel`.

        Returns a result dict whose `status` is one of:
        - "ok"          — `url` holds the post's file URL; `tags` is the
                          post's `tag_string` split on spaces (not the query);
                          `score` and `favs` are the post's net score and
                          favorite count;
        - "no_results"  — nothing new matched; `suggestions` may hold
                          alternative tag spellings from the autocomplete API;
        - "error"       — `message` explains the failure.

        Never sends anything; the caller presents the outcome.
        """
        tags = apply_rating_policy(list(tags), channel)
        config = self.bot.config
        api_key = config.get(None, "DANBOORU_API_KEY", scope="global") or os.getenv("DANBOORU_API_KEY")
        login = config.get(None, "DANBOORU_LOGIN", scope="global") or os.getenv("DANBOORU_LOGIN")
        url = f"{self.danbooru_base}/posts.json"
        params = {"tags": " ".join(tags), "limit": 100}
        auth = (login, api_key) if api_key and login else None
        try:
            # requests is blocking; run it off the event loop
            response = await asyncio.to_thread(requests.get, url,
                                               params=params, auth=auth,
                                               headers=REQUEST_HEADERS, timeout=15)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or any(not isinstance(post, dict) for post in data):
                raise ValueError("Expected a list of posts")
        except Exception:
            self.logger.exception("Danbooru posts.json request failed")
            return {"status": "error", "message": "Error fetching from Danbooru API.",
                    "tags": tags}
        for post in data:
            # Query syntax can express alternatives; enforce the channel policy
            # on returned posts as well. Missing ratings are not verified safe.
            if not _channel_is_nsfw(channel) and post.get("rating") != "g":
                continue
            post_id = post.get("id")
            if post_id in self.posted_danbooru:
                continue
            file_url = post.get("file_url")
            if file_url:
                self.posted_danbooru.add(post_id)
                return {"status": "ok", "url": file_url, "post_id": post_id,
                        "tags": (post.get("tag_string") or "").split(),
                        "score": post.get("score") or 0,
                        "favs": post.get("fav_count") or 0}
        return {"status": "no_results", "tags": tags,
                "suggestions": await self._suggest(tags[0]) if tags else []}

    async def _suggest(self, first_tag: str) -> list:
        """Alternative spellings for a tag that returned nothing, from the
        autocomplete endpoint (which answers in HTML, not JSON)."""
        autocomplete_url = f"{self.danbooru_base}/autocomplete"
        try:
            auto_resp = await asyncio.to_thread(requests.get, autocomplete_url,
                                                params={"search[query]": first_tag, "search[type]": "tag_query"},
                                                headers=REQUEST_HEADERS, timeout=15)
            auto_resp.raise_for_status()
            soup = bs4.BeautifulSoup(auto_resp.text, "html.parser")
            li_tags = soup.find_all("li", class_="ui-menu-item")
            return [li.get("data-autocomplete-value") for li in li_tags][:5]
        except Exception:
            self.logger.exception("Danbooru autocomplete request failed")
            return []

    # --- command --------------------------------------------------------------

    @commands.command(name="danbooru", aliases=["db"])
    async def danbooru(self, ctx, *tags):
        """Fetch a random image from Danbooru based on tags."""
        if not tags:
            await ctx.send("Usage: !danbooru (or !db) tag1 tag2 ...")
            return
        result = await self.search(list(tags), ctx.channel)
        if result["status"] == "ok":
            await ctx.send(result["url"])
        elif result["status"] == "error":
            await ctx.send(result["message"])
        elif result["suggestions"]:
            await ctx.send(f"Did you mean `{', '.join(result['suggestions'])}`?")
        else:
            await ctx.send("No new image found.")

    # --- op -------------------------------------------------------------------
    #
    # An op without a serializer sends the frontend nothing but {"ok": true}
    # (Op.serialize_result), so every op that RETURNS data must declare one.

    @op(
        "search_danbooru",
        "Search Danbooru for an image matching space-separated tags and return "
        "its URL, tags, score, and favs. Outside NSFW-marked channels the "
        "search is forced to rating:general. Does not post anything.",
        PermissionLevel.EVERYONE,
        params=[
            OpParam("channel", ParamKind.CHANNEL,
                    "Channel the result is intended for; its NSFW flag decides "
                    "whether non-safe ratings are allowed."),
            OpParam("tags", ParamKind.STRING,
                    "Space-separated Danbooru tags, e.g. 'cat_girl smiling'."),
        ],
        serialize=_serialize_search,
        agent_guidance=(
            "Returns a URL, the pulled post's tags, score, and favs — it does "
            "NOT post the image; send it yourself with send_message if the "
            "user wanted it posted. On status ok, `tags` is the post's "
            "tag_string, not the query (artist and character are already in "
            "that list). On no_results, `tags` is still the query that was "
            "searched. Pass the channel the image is destined for, not the "
            "one you were asked in, or the rating check applies to the wrong "
            "channel. A 'no_results' status may carry suggestions: better "
            "tag spellings to retry with."),
        scope=OpScope.GUILD,
        group="integrations",
        group_label="Integrations",
    )
    async def op_search_danbooru(self, ctx, channel, tags: str) -> dict:
        return await self.search(str(tags).split(), channel)


async def setup(bot):
    await bot.add_cog(Danbooru(bot))
