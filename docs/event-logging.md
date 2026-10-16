# Registered event logging

Guild event reports use `utils/event_logging.py`, a headless registration and
delivery service. Membership joins/departures, nickname changes, voice activity,
and honeypot incidents are producers. Their listeners remain in their own cogs.
Reports work without the optional `logging` cog whenever another producer is
loaded. The optional `error_handler` cog supplies the shared settings UI.

## Operator controls

Open `/logsettings` → **Activity** (ephemeral), or its hidden, guild-only
`!logsettings` prefix twin. Standard bot admins can configure this guild; callbacks
recheck authorization. Select All event reports, a feature, or a category. The
selector is paginated as registrations grow. Choose a destination, inherit its
default, or turn the selected scope on/off.

All event reports, the feature, and the category must be enabled for delivery.
All default to on. Routing chooses the category destination, then the feature's,
then the all-event destination, then the guild's first text channel named #log.
An explicit destination that disappears does not silently fall back elsewhere.
Destination channels must belong to this guild and allow the bot to view and
send. Routing and toggles are read on every delivery; no restart is needed.
All event mentions are suppressed, including legacy welcome reports.

The Server and Global tabs retain their existing error/denial settings. Activity
controls do not suppress mandatory errors or permission denials, change global
error reporting, or inherit error destinations. The service logs delivery
failures locally and never recursively sends an error report. A missing #log,
disabled route, or failed send cannot abort a producer's moderation action.

See [the configuration registry](config-system.md#guild-scope-guild_idjson) for
`event_logging`. Example partial guild configuration:

```json
{
  "event_logging": {
    "default_channel": 123,
    "features": {
      "membership": {"enabled": true},
      "honeypot": {"channel_id": 456}
    },
    "categories": {
      "membership.join": {"enabled": false}
    }
  }
}
```

IDs here are placeholders. Preserve unrelated configuration and registrations.
Absent settings preserve the old #log reporting default. Disabling a cog removes
its controls but keeps its saved routing for when the cog is enabled again.

## Producer contract

Register stable feature/category IDs and human labels during `cog_load`, and
unregister the same owner during `cog_unload`. Registration validates the whole
feature before publishing it. Roll back earlier registrations if a cog registers
multiple features and a later registration fails. Startup registration does not
write config. Do not register in a module import or constructor: a failed load
must not leave stale categories. The bot lifecycle also clears registrations after failed injection and in its
removal finalizer, including when a consumer teardown fails.

```python
from utils.event_logging import registry, report

async def cog_load(self):
    registry.register(self, "my_feature", "My feature", {"incident": "Incidents"})

def cog_unload(self):
    registry.unregister(self)

async def record_incident(self, guild, text):
    await report(self.bot, guild, "my_feature.incident", text)
```

Feature and category components use lowercase letters, digits and underscores,
starting with a letter. Feature IDs are unique; ownership is identity-based.
The UI discovers categories from these registrations, with no feature inventory
or feature-specific event listener in the delivery service. Registration makes
controls available and does not grant permissions or enable ops. Administrative
writers must authorize callers before using the storage helper. Producer text
is split with `recursive_split`; Discord mentions are always disabled.

Existing producer identifiers are `membership.join`, `membership.leave`,
`activity.nickname`, `activity.voice`, and `honeypot.incident`. Existing voice
exclusions (bots and channels whose names contain `Voice Chat`) remain intact.
