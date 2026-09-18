# AI history caching

The bot normally sends the latest **15 Discord messages**. Recent invocations
can retain the same starting message and grow the window to **30 messages**,
but only when reusing the unchanged prefix is estimated to cost less than a
fresh minimum-sized window. These bounds count messages, not tokens.

## Operator settings

`/aisettings` → **Server config** has **History Min** and **History Max**
dropdowns. **Custom min / max…** accepts any bounds from 1 to 1000 messages.
Raising Min above Max raises Max with it; Max cannot be below Min.

- **Min:** normal context size, or all available messages in a shorter channel.
- **Max:** ceiling for retaining older history when estimated caching savings
  justify it. Reply references can add supplemental messages beyond this cap.
- Start with **Max = 2× Min** (the default 15/30). Larger caps permit longer
  reuse chains, but do not guarantee savings or more context. To consistently
  provide more context, raise Min. Equal bounds give a fixed-size window.

`/aisettings` → **Models & Providers** → **Input cache** lets superadmins set
the cached/full input price ratio and assumed cache lifetime (defaults
**10% / 300 seconds**). Use the model's actual price ratio: Grok 4.5 is **15%**
and Grok 4.6 is **25%** ([pricing](https://docs.x.ai/developers/pricing)). This
percentage is a price, not a hit rate. A zero lifetime disables extension.
These settings neither force cache retention nor change provider billing.

## Selection and lifecycle

Each invocation fetches current history through its trigger message, honoring
edits/deletions and excluding later arrivals. It compares a fresh Min-sized
window with the previous anchor's window, if that anchor is still present
within Max and the previous send is within the assumed lifetime:

```
estimated input cost = uncached tokens + cached tokens × cached/full price ratio
```

Token estimates use UTF-8 bytes / 4 plus message overhead. Cached tokens are
estimated from the identical whole-message prefix of the previous prompt.
This is a planning estimate, not a tokenizer or a guarantee of a provider hit.
Ties choose the fresh window. Frequent invocations with few intervening
messages favor extension; lots of chat between invocations can defeat it.

Stable system instructions precede chronological history. Supplemental reply
references and changing request details follow it. Each reference is resolved
at most once per invocation, shared by both candidates, and fetched afresh on
the next invocation. The lifetime is rechecked after these reads. xAI requests
use a stable `x-grok-conv-id` per guild/channel/model for routing.

Only a successful model call commits state. An extension **keeps the same
anchor ID and refreshes the prompt and last-send timestamp**; it can extend
repeatedly, not just once. Failed calls do not refresh state. Calls in a channel
serialize. State resets on restart and is capped at 256 entries, evicting the
least recently committed entry. Expired entries are removed on access.

## Data structure and complexity

The key is `(guild, channel, provider, model, tool names)`; the value contains
the anchor ID, last-send monotonic timestamp, and previous rendered prompt.
An ID/timestamp pair alone cannot detect edits or measure a matching prefix.

For K stored entries (at most 256), with fixed-size routing keys:

| Structure | Lookup / update | Oldest-entry eviction | Assessment |
|---|---|---|---|
| Current flat `OrderedDict` | Expected O(1) | O(1) | Direct lookup and one global cap |
| Nested guild/channel dictionaries | Expected O(1) | O(K) without a separate ordering index | More bookkeeping, no lookup advantage |
| List of tuples | O(K) search | O(K) search/removal | Simpler records, slower access |

Prompt storage dominates memory: O(K + total retained prompt bytes), rather
than just O(K) IDs. Rendering H history/reference messages with B text bytes
takes O(H log H + B), plus mention processing and Discord I/O; ID lookups use
dictionaries. Candidate cost comparison is O(B + H). No persistent message
cache, token index, or background cleanup is needed for this bounded store.

The `llm_usage` log records actual input, cached input, output, and available
reasoning tokens per request. See the [config registry](config-system.md#model-cache-policy)
for stored settings. Settings apply immediately; code changes require restart.
