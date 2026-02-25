from discord.ext import commands
import discord
import time
from core.utils import is_superadmin, safe_delete


class Cleanup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    @commands.group(name='cleanup', hidden=True, invoke_without_command=True)
    @commands.check(is_superadmin)
    async def cleanup(self, ctx):
        """Bulk message cleanup utilities."""
        await ctx.send(
            "**Cleanup commands:**\n"
            "`!cleanup user <user_id> [--dry]` - Delete all messages by a user\n"
            "`!cleanup gone [--dry]` - Delete all messages by users who left the server\n"
            "`!cleanup replies <user_id> [--dry]` - Delete replies to a user\n"
            "`!cleanup stale-replies [--dry]` - Delete replies to any user who left the server\n"
            "`!cleanup full <user_id> [--dry]` - All of the above for a specific user + stale replies\n"
            "\nAdd `--dry` to preview without deleting.",
            delete_after=30
        )

    @cleanup.command(name='user')
    @commands.check(is_superadmin)
    async def cleanup_user(self, ctx, user_id: int, *, flags: str = ""):
        """Delete all messages by a specific user ID."""
        dry = "--dry" in flags
        await self._run_cleanup(ctx, dry=dry, target_author_ids={user_id})

    @cleanup.command(name='gone')
    @commands.check(is_superadmin)
    async def cleanup_gone(self, ctx, *, flags: str = ""):
        """Delete all messages by users who have left the server."""
        dry = "--dry" in flags
        await self._run_cleanup(ctx, dry=dry, gone_authors=True)

    @cleanup.command(name='replies')
    @commands.check(is_superadmin)
    async def cleanup_replies(self, ctx, user_id: int, *, flags: str = ""):
        """Delete all replies to a specific user ID."""
        dry = "--dry" in flags
        await self._run_cleanup(ctx, dry=dry, reply_to_ids={user_id})

    @cleanup.command(name='stale-replies')
    @commands.check(is_superadmin)
    async def cleanup_stale_replies(self, ctx, *, flags: str = ""):
        """Delete replies to any user who has left the server."""
        dry = "--dry" in flags
        await self._run_cleanup(ctx, dry=dry, reply_to_gone=True)

    @cleanup.command(name='full')
    @commands.check(is_superadmin)
    async def cleanup_full(self, ctx, user_id: int, *, flags: str = ""):
        """Full cleanup: messages by user, replies to user, and replies to all gone users."""
        dry = "--dry" in flags
        await self._run_cleanup(
            ctx, dry=dry,
            target_author_ids={user_id},
            reply_to_ids={user_id},
            reply_to_gone=True
        )

    async def _run_cleanup(self, ctx, *, dry=False, target_author_ids=None,
                           gone_authors=False, reply_to_ids=None, reply_to_gone=False):
        """Core cleanup engine. Scans all text channels and deletes matching messages.

        Args:
            dry: If True, only count and report — don't delete.
            target_author_ids: Set of user IDs whose messages should be deleted.
            gone_authors: If True, delete messages from any user no longer in the guild.
            reply_to_ids: Set of user IDs — delete messages that reply to them.
            reply_to_gone: If True, delete messages that reply to any user no longer in the guild.
        """
        if ctx.guild is None:
            await ctx.send("This command must be used in a server.")
            return

        await safe_delete(ctx, self.logger)
        mode = "DRY RUN" if dry else "LIVE"
        status = await ctx.send(f"**[{mode}]** Starting cleanup scan across all channels...")

        guild = ctx.guild
        member_ids = {m.id for m in guild.members}
        target_author_ids = target_author_ids or set()
        reply_to_ids = reply_to_ids or set()

        # Track which gone user IDs we discover
        discovered_gone_ids = set()

        total_found = 0
        total_deleted = 0
        total_failed = 0
        channel_results = []

        text_channels = [ch for ch in guild.channels if isinstance(ch, discord.TextChannel)]
        total_channels = len(text_channels)
        last_status_update = 0.0  # monotonic timestamp of last edit

        for i, channel in enumerate(text_channels, 1):
            # Check bot has permissions in this channel
            perms = channel.permissions_for(guild.me)
            if not perms.read_message_history:
                channel_results.append((channel.name, 0, 0, 0, "no read permission"))
                continue
            if not dry and not perms.manage_messages:
                channel_results.append((channel.name, 0, 0, 0, "no manage_messages permission"))
                continue

            # Update status with 4-second cooldown (always update on last channel)
            now = time.monotonic()
            if i == total_channels or now - last_status_update >= 4.0:
                try:
                    await status.edit(
                        content=f"**[{mode}]** Scanning channel {i}/{total_channels}: #{channel.name}... "
                               f"({total_found} found so far)"
                    )
                    last_status_update = now
                except discord.HTTPException:
                    pass

            ch_found = 0
            ch_deleted = 0
            ch_failed = 0
            to_delete = []

            try:
                async for message in channel.history(limit=None, oldest_first=True):
                    should_delete = False
                    reason = None

                    # Check 1: message is by a target author
                    if message.author.id in target_author_ids:
                        should_delete = True
                        reason = "author match"

                    # Check 2: message is by someone who left
                    if not should_delete and gone_authors:
                        if message.author.id not in member_ids and not message.author.bot:
                            should_delete = True
                            reason = "gone author"
                            discovered_gone_ids.add(message.author.id)

                    # Check 3: message replies to a target user
                    if not should_delete and message.reference and message.reference.resolved:
                        ref = message.reference.resolved
                        if isinstance(ref, discord.Message):
                            if ref.author.id in reply_to_ids:
                                should_delete = True
                                reason = "reply to target"
                            elif reply_to_gone and ref.author.id not in member_ids and not ref.author.bot:
                                should_delete = True
                                reason = "reply to gone user"
                                discovered_gone_ids.add(ref.author.id)

                    # Check 4: message replies to a deleted message (reference unresolved)
                    if not should_delete and reply_to_gone and message.reference and not message.reference.resolved:
                        # If the referenced message is gone, it was likely from a departed user
                        # whose messages were already deleted — include these too
                        if message.reference.resolved is None and message.reference.message_id:
                            # Try to determine if the original author is gone
                            # resolved=None means discord couldn't fetch it (deleted)
                            should_delete = True
                            reason = "reply to deleted message"

                    if should_delete:
                        ch_found += 1
                        to_delete.append((message, reason))

            except discord.Forbidden:
                channel_results.append((channel.name, 0, 0, 0, "forbidden"))
                continue
            except discord.HTTPException as e:
                channel_results.append((channel.name, ch_found, 0, 0, f"error: {e}"))
                continue

            # Delete phase
            if not dry and to_delete:
                for message, reason in to_delete:
                    try:
                        await message.delete()
                        ch_deleted += 1
                    except discord.NotFound:
                        ch_deleted += 1  # Already gone, counts as success
                    except (discord.Forbidden, discord.HTTPException) as e:
                        ch_failed += 1
                        self.logger.warning(
                            f"Cleanup: failed to delete message {message.id} in #{channel.name}: {e}"
                        )

            total_found += ch_found
            total_deleted += ch_deleted
            total_failed += ch_failed

            if ch_found > 0:
                note = ""
                if not dry:
                    note = f" (deleted {ch_deleted}, failed {ch_failed})"
                channel_results.append((channel.name, ch_found, ch_deleted, ch_failed, note))

        # Build summary
        lines = [f"**[{mode}] Cleanup complete.**\n"]

        if target_author_ids:
            lines.append(f"Target user IDs: {', '.join(str(x) for x in target_author_ids)}")
        if gone_authors:
            lines.append(f"Scanned for gone authors: yes")
        if reply_to_ids:
            lines.append(f"Reply-to target IDs: {', '.join(str(x) for x in reply_to_ids)}")
        if reply_to_gone:
            lines.append(f"Scanned for replies to gone users: yes")
        if discovered_gone_ids:
            lines.append(f"Gone user IDs discovered: {', '.join(str(x) for x in discovered_gone_ids)}")

        lines.append(f"\n**Total messages {'found' if dry else 'processed'}:** {total_found}")
        if not dry:
            lines.append(f"**Deleted:** {total_deleted}")
            if total_failed:
                lines.append(f"**Failed:** {total_failed}")

        if channel_results:
            lines.append("\n**Per-channel breakdown:**")
            for name, found, deleted, failed, note in channel_results:
                if dry:
                    lines.append(f"  #{name}: {found} messages{note}")
                else:
                    lines.append(f"  #{name}: {found} found{note}")
        else:
            lines.append("\nNo matching messages found in any channel.")

        if dry and total_found > 0:
            lines.append(f"\n*Run again without `--dry` to delete these messages.*")

        summary = "\n".join(lines)

        # Split if too long for Discord
        if len(summary) <= 2000:
            await status.edit(content=summary)
        else:
            await status.edit(content=summary[:2000])
            # Send overflow as follow-up
            remaining = summary[2000:]
            while remaining:
                chunk = remaining[:2000]
                remaining = remaining[2000:]
                await ctx.send(chunk)

        self.logger.info(
            f"Cleanup [{mode}] by {ctx.author} (ID: {ctx.author.id}): "
            f"found={total_found} deleted={total_deleted} failed={total_failed} "
            f"author_ids={target_author_ids} gone={gone_authors} "
            f"reply_to={reply_to_ids} reply_to_gone={reply_to_gone}"
        )


async def setup(bot):
    await bot.add_cog(Cleanup(bot))
