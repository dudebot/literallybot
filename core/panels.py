"""Discord panel visibility, ownership, and expiry behavior."""
import discord
from discord import app_commands


# Discord picker pin for bot-gated slash panels. Manage Messages is the
# typical moderator bit: ordinary members don't have it, and people we add
# to the bot's guild-admin ACL generally do. This is VISIBILITY, not the
# authorization gate — that remains is_admin / is_superadmin. A superadmin
# who is not a moderator in a given guild still has the prefix twin
# (`!aisettings`, `!cogs`, …); Discord will native-refuse the slash form.
PANEL_SLASH_PERMISSIONS = discord.Permissions(manage_messages=True)


def panel_slash_pin():
    """Picker pin matching PANEL_SLASH_PERMISSIONS.

    Visibility only — authorization stays is_admin / is_superadmin.
    """
    return app_commands.default_permissions(manage_messages=True)


class InvokerOnlyView:
    """Mixin giving an interactive panel its single-invoker lifecycle.

    Mix in BEFORE the discord view base so these hooks win the MRO:

        class MyView(InvokerOnlyView, discord.ui.View):
            panel_command = "!mypanel"

    Supplies the two halves every panel in this repo hand-rolled
    identically: `interaction_check` rejecting non-invokers ephemerally,
    and an `on_timeout` that disables every child and best-effort-edits
    the source message (swallowing HTTPException, since an expired panel
    whose message was deleted must not raise).

    It is a mixin rather than a base class because panels sit on two
    different discord bases — `discord.ui.View` and Components-V2
    `discord.ui.LayoutView` — and child enumeration differs between them
    (`walk_children` when present, else `children`).

    Subclasses set `panel_command` (used to build both the rejection and
    expiry text) or override `rejection_text` / `expiry_text` outright;
    an `expiry_text` of None leaves the message content untouched and
    only disables the controls.

    Requires `self.invoker_id`; `self.message` defaults to None so a panel
    that never stored its message still times out cleanly.
    """

    panel_command = None
    message = None
    invoker_id = None

    @property
    def rejection_text(self) -> str:
        if self.panel_command:
            return (f"This panel isn't yours — run {self.panel_command} "
                    f"to open your own.")
        return "This panel isn't yours."

    @property
    def expiry_text(self):
        if self.panel_command:
            return f"Panel expired — run {self.panel_command} again."
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                self.rejection_text, ephemeral=True)
            return False
        return True

    def _all_children(self):
        """Every component, across both view bases.

        LayoutView nests its components inside containers, so `children`
        alone would leave the inner controls live after expiry.
        """
        walk = getattr(self, "walk_children", None)
        return walk() if callable(walk) else self.children

    async def on_timeout(self):
        for child in self._all_children():
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is None:
            return
        try:
            text = self.expiry_text
            if text is None:
                await self.message.edit(view=self)
            else:
                await self.message.edit(content=text, view=self)
        except discord.HTTPException:
            # Message deleted or otherwise unreachable — expiry is
            # best-effort; the controls are dead either way.
            pass
