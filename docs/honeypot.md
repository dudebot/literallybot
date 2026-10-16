# Honeypot moderation

Configure one trap per guild with an existing role and text channel:

```text
/honeypot action:enable role:@Restricted channel:#trap minutes:60 strip_all_other_roles:true
/honeypot action:disable
```

The action defaults to enable, the channel to the current text channel, minutes
to 10, and stripping to false. The role is required on first enable; subsequent
enables can reuse the saved role. Selecting a different channel replaces the
trap. Minutes is a message cleanup lookback (0–10080), not a role timeout. Zero
means delete only the triggering message. Omitted minutes and stripping reset to
the defaults on every enable, including after disabling.

Both commands use the standard `is_admin` gate. The slash response is ephemeral
and uses the usual Manage Messages picker pin. A configured bot admin without
that Discord permission can use the hidden, guild-only prefix fallback:

```text
!honeypot [enable|disable] [@role] [#channel] [minutes] [true|false]
!honeypot enable @Restricted #trap 60 true
!honeypot disable
```

The ADMIN, GUILD-scoped `configure_honeypot` op calls the same configuration
service. It requires an explicit `channel_id`, even when disabling, and accepts
optional `role_id`, `enabled`, `minutes`, and `strip_all_other_roles`. Its IDs are
returned as decimal strings. Registration does not enable the op for agents;
normal [tool exposure settings](config-system.md#enabling-cog-tools) apply.

## Permissions and setup

Create the role, channel, warning message, and access rules separately. The bot
needs Manage Roles and a highest role above the selected role. The role must
belong to this guild, must not be @everyone or managed, and must not grant
Administrator. The trap requires View Channel, Read Message History, and Manage
Messages. Send Messages in the trap, Manage Channels, and Move Members are not
required for configuration.

Grant equivalent history/deletion access elsewhere for complete cleanup. Manage
Threads is needed to enumerate all private archived threads; without it, joined
private threads are attempted and the report marks coverage incomplete. Missing
access is reported rather than treated as successful empty history.

A role's channel deny does not override another role's explicit allow. Role
position does not change that rule. Keeping roles that grant access, managed or
higher roles, member-specific allows, or Administrator can prevent isolation.
Verify effective permissions separately; role assignment alone does not promise
isolation. Write a clear warning stating the consequences of posting.

## Trigger and cleanup behavior

A human message in the trap or one of its threads triggers moderation. Bots and
everyone passing `is_admin` (including the owner, Discord administrators,
configured bot admins and superadmins) are exempt from role changes and deletion.
Already holding the selected role does not exempt a member.

The bot first assigns the selected role, rechecking its eligibility in case it
changed after setup. With stripping enabled, it fetches current membership and
removes other manageable roles one at a time. It preserves @everyone, the
selected role, managed roles, and roles at or above its highest role. Failed
removals are reported and do not stop other removals or cleanup. Failed role
assignment prevents stripping and still permits cleanup.

The triggering message is always attempted. With a positive lookback, cleanup
scans only this guild from the trigger's timestamp minus the configured minutes
through the start of cleanup. Only that member's messages are deleted; their
attachments disappear with them. Channel history, active threads and accessible
public/private archived threads are covered, without duplicate thread scans.
Archived enumeration is bounded by archive timestamp, so an old thread with
recent messages is included.

Only one scan runs per guild/member pair. Further trap messages during that scan
are deleted immediately, even after its upper timestamp, and reported separately
from the scan count. State is cleared after completion or failure so later
triggers can retry. Busy guilds and large lookbacks may take time under Discord
rate limits. Restart interrupts a running scan; jobs are not persisted or resumed.
Deleted messages cannot be restored, and removed roles are not journaled or
automatically restored. Disabling prevents new triggers and leaves assigned roles,
server resources, and an already-running cleanup alone.

## Reports

Honeypot registers `honeypot.incident` with the shared
[event logging service](event-logging.md), independently of the optional
membership/activity cog. Reports default to #log. `/logsettings` → Activity lets
admins change the destination or disable feature/category delivery. Reports also
use the local bot logger, even when Discord delivery is disabled.

Reports identify the member, channel, trigger, selected role and role outcome,
removed role IDs, exact cleanup window (or trigger-only mode), deletion count,
and failures. Concurrent trigger deletions have separate counts. Missing access
or failed operations produce an INCOMPLETE result. Mentions are suppressed and
long reports are split. Missing or unwritable logging destinations do not stop
moderation.
