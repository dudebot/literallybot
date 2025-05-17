from discord.ext import commands
import discord

class SetRole(commands.Cog):
    """This is a cog with role commands."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='setrole')
    async def setrole(self, ctx, role: str, member: discord.Member = None):
        """Adds or removes available roles. Optionally apply to a specified user."""
        config = self.bot.config
        whitelist_roles = config.get(ctx, "whitelist_roles", [])
        # Determine target; default to invoking user if no member provided.
        target = member if member else ctx.author
        # If the target is not the invoker, verify admin/superadmin permission.
        if member:
            admins = config.get(ctx, "admins", [])
            if not (ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner or
                    ctx.author.id in admins):
                await ctx.send("You don't have permission to modify roles for other users.")
                return
        role_option = role[0]
        rolename = role[1:].strip()
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You need the manage roles permission to use this command.")
            return
        if role_option not in ["+", "-"]:
            await ctx.send("Use a + or - to add or remove the role (eg: !setrole +Events or !setrole +Events @member)")
            return
        elif rolename.lower() not in [r.lower() for r in whitelist_roles]:
            await ctx.send(f"Role not in whitelist: {rolename}")
            return
        role_obj = discord.utils.get(ctx.guild.roles, name=rolename)
        if role_obj is not None:
            if role_option == "+":
                await target.add_roles(role_obj)
                await ctx.send(f"Added role: {role_obj.name} to {target.mention}")
            elif role_option == "-":
                await target.remove_roles(role_obj)
                await ctx.send(f"Removed role: {role_obj.name} from {target.mention}")
        else:
            await ctx.send(f"Could not find role: {rolename}")

    @commands.command(name='whitelistrole')
    async def whitelistrole(self, ctx, *, role: str):
        """Adds a role to the whitelist roles."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You need the manage roles permission to use this command.")
            return
        config = self.bot.config
        whitelist_roles = config.get(ctx, "whitelist_roles", [])
        if role.lower() in [r.lower() for r in whitelist_roles]:
            await ctx.send(f"Role '{role}' is already whitelisted.")
        else:
            whitelist_roles.append(role)
            config.set(ctx, "whitelist_roles", whitelist_roles)
            await ctx.send(f"Role '{role}' added to the whitelist.")
            
    # Modified: App Command for setting up emoji role toggle; change message_id to string and convert to int.
    @discord.app_commands.command(name="setemojiroletoggle", description="Configure a reaction role toggle (mod-only)")
    @discord.app_commands.default_permissions(manage_messages=True)
    async def setemojiroletoggle(self, ctx: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        try:
            msg_id_int = int(message_id)
        except Exception as e:
            await ctx.response.send_message(f"Invalid message ID provided: {e}", ephemeral=True)
            return
        # Convert emoji string to PartialEmoji
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception as e:
            await ctx.response.send_message(f"Invalid emoji provided: {e}", ephemeral=True)
            return

        guild = ctx.guild
        if not guild:
            await ctx.response.send_message("This command must be used in a guild.", ephemeral=True)
            return

        # For custom emoji: if the emoji isn't in the guild, fetch and add it.
        if partial_emoji.id:
            if not guild.get_emoji(partial_emoji.id):
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(partial_emoji.url) as resp:
                        if resp.status != 200:
                            await ctx.response.send_message("Failed to fetch emoji image.", ephemeral=True)
                            return
                        image_data = await resp.read()
                new_emoji = await guild.create_custom_emoji(name=partial_emoji.name, image=image_data)
                partial_emoji = new_emoji  # update with newly created emoji

        # Pre-populate the reaction on the target message.
        try:
            message = await ctx.channel.fetch_message(msg_id_int)
            await message.add_reaction(partial_emoji)
        except Exception as e:
            await ctx.response.send_message(f"Failed to add reaction to the message: {e}", ephemeral=True)
            return

        # Store the emoji-role mapping; use emoji.id (as string) for custom emoji, else emoji.name.
        key = str(partial_emoji.id) if partial_emoji.id else partial_emoji.name
        config = self.bot.config
        toggles = config.get(ctx, "emoji_role_toggles", {})
        msg_key = str(msg_id_int)
        if msg_key not in toggles:
            toggles[msg_key] = {}
        toggles[msg_key][key] = role.id
        config.set(ctx, "emoji_role_toggles", toggles)

        await ctx.response.send_message("Emoji role toggle configured.", ephemeral=True)

    @discord.app_commands.command(name="removeemojiroletoggle", description="Remove a reaction role toggle (mod-only)")
    @discord.app_commands.default_permissions(manage_messages=True)
    async def removeemojiroletoggle(self, interaction: discord.Interaction, message_id: str, emoji: str):
        try:
            msg_id_int = int(message_id)
        except Exception as e:
            await interaction.response.send_message(f"Invalid message ID provided: {e}", ephemeral=True)
            return
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception as e:
            await interaction.response.send_message(f"Invalid emoji provided: {e}", ephemeral=True)
            return
        key = str(partial_emoji.id) if partial_emoji.id else partial_emoji.name
        config = self.bot.config
        toggles = config.get(interaction, "emoji_role_toggles", {})
        msg_key = str(msg_id_int)
        if msg_key in toggles and key in toggles[msg_key]:
            del toggles[msg_key][key]
            if not toggles[msg_key]:
                del toggles[msg_key]
            config.set(interaction, "emoji_role_toggles", toggles)
            await interaction.response.send_message("Emoji role toggle removed.", ephemeral=True)
        else:
            await interaction.response.send_message("Emoji role toggle not found.", ephemeral=True)

    async def _process_reaction_toggle(self, payload, add: bool):  
        toggles = self.bot.config.get(payload.guild_id, "emoji_role_toggles", {})
        msg_key = str(payload.message_id)
        if msg_key not in toggles:
            return
        mapping = toggles[msg_key]
        emoji_identifier = str(payload.emoji.id) if payload.emoji.id else payload.emoji.name
        if emoji_identifier not in mapping:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        target_role = guild.get_role(mapping[emoji_identifier])
        if not target_role:
            return
        try:
            if add:
                await member.add_roles(target_role)
            else:
                await member.remove_roles(target_role)
        except Exception as e:
            print("Error toggling role:", e)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._process_reaction_toggle(payload, True)
            
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._process_reaction_toggle(payload, False)

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(SetRole(bot))
