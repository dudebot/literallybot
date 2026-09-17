# AI history caching

The bot starts with the latest **15 messages**. When invoked again soon, it can
keep the same starting message and extend the history up to **30 messages**.
Reusing an unchanged prompt prefix lets the provider charge its cached-input
rate. The bot extends only when the estimated input cost is lower than using
a fresh minimum-sized window; otherwise it moves the starting point forward.

Stable bot, persona, and tool instructions come first, followed by chronological
history. Supplemental reply references and changing request details (invoking
user, trigger message, and name mapping) follow the reusable history. Tool
schemas remain native pydantic-ai definitions.

## Settings

- `/aisettings` → Server config → **History**: guild admins set the minimum and
  maximum message counts (defaults **15 / 30**, range 1–1000). Equal bounds
  give a fixed window. Referenced messages can add context beyond these counts.
- `/aisettings` → Models & Providers → **Input cache**: superadmins set the
  selected provider/model's cached-input price percentage and assumed cache
  lifetime (defaults **10% / 300 seconds**). The percentage is cached price
  divided by regular input price, not the cache hit rate. A zero lifetime
  disables history extension without disabling provider-side caching.

Use the model's actual price ratio: Grok 4.5 is **15%** and Grok 4.6 is **25%**
according to [xAI pricing](https://docs.x.ai/developers/pricing).
xAI's [cache guidance](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/best-practices)
does not guarantee retention for five minutes or an hour; 300 seconds is a
planning assumption. Requests use a stable `x-grok-conv-id` per
guild/channel/model to help provider routing.

## Selection and operation

The selector compares both candidates using:

```
estimated input cost = uncached tokens + cached tokens × cached-input price ratio
```

It estimates tokens from text size and compares complete messages for an
unchanged prefix. Frequent invocations with few intervening messages favor
extension. Long gaps, heavy intervening chat, or expensive cached input favor
a fresh window. Increasing the maximum permits more reuse but does not force
longer prompts or guarantee savings.

State stays in memory for at most 256 guild/channel/provider/model/tool
combinations and resets on restart. Calls within a channel serialize; each
request fetches current history through its trigger to reflect edits and
deletions. Only successful model calls update the remembered starting point.
The `llm_usage` log records actual input, cached input, output, and available
reasoning token counts per API request, along with estimated cost.

See the [config key registry](config-system.md#model-cache-policy) for stored
settings. Code changes take effect after the normal bot restart.
