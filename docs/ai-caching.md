# AI history caching

Each chat turn sends recent channel messages as history. The window is chosen
so that consecutive turns can repeat the same prefix and hit the provider's
input cache. Bounds count messages, not tokens; defaults are **15** (min) and
**30** (max).

## The rule

The bot remembers where the last successful request's history started (its
first message, the *anchor*).

- If the last successful send in this conversation was within
  `cache_ttl_seconds`, and the anchor is still within the newest **max**
  messages, the new window starts at the same anchor and runs through the
  trigger message. The prefix repeats; the window grows.
- Otherwise (or when that window would be no longer than min) the window is
  the newest **min** messages, and its first message becomes the new anchor.

There is no cost comparison and no token estimate. A conversation is keyed by
`(guild, channel, provider, model, tool names)`, so changing the model or the
offered tools starts fresh. State lives in memory (`core/ai_history.py`), is
capped at 256 entries, and resets on restart. Only a successful model call
commits it: an extension keeps the anchor and refreshes the send time. Turns
in one channel run one at a time.

History is fetched again every turn, through the trigger message, so edits and
deletions show and later messages are excluded. Messages a turn replies to
(referenced messages) are added if missing and placed at their chronological
position. Only this bot's own messages become assistant turns; other bots'
messages are user turns like anyone else's.

The system prompt comes first, then history, then the changing request context
(invoking user, trigger id, user-name mapping). xAI requests carry a stable
`x-grok-conv-id` per guild/channel/model.

## Operator settings

`/aisettings` → **Server config** has **History Min** and **History Max**
dropdowns. **Custom min / max…** accepts any bounds from 1 to 1000 messages.
Raising Min above Max raises Max with it; Max cannot be below Min.

- **Min** is the normal context size. To always send more context, raise Min.
- **Max** caps how far a kept anchor may fall behind. Equal bounds give a
  fixed-size window. Referenced messages can add messages beyond Max.

`/aisettings` → **Models & Providers** → **Input cache** (superadmin) sets two
per-model values:

- **Assumed cache lifetime in seconds** (`cache_ttl_seconds`, default 300;
  0 always starts fresh). This is the only setting the window rule reads.
- **Cached input price** (`cache_input_ratio`, default 10%). Used only for cost
  estimates, and only when the model also has `cost_per_mtok_input` and
  `cost_per_mtok_output` set. It does not affect history.

Neither setting changes provider billing or forces cache retention. See the
[config registry](config-system.md#model-cache-policy).

## Image inputs

Image input is on unless the model entry sets `vision: false`. Attachment
images and embed images/thumbnails belong to their source turn, including
referenced messages and bot posts. Pydantic-ai response parts cannot carry
input images, so bot-posted pixels go in a labeled user part right after the
bot's text.

Each request sends at most eight images. Trigger images come first, then the
reply target's, each in attachment/embed order. Remaining slots keep earlier
selections for this conversation while they stay in the window, so text-only
follow-ups keep the prefix stable; only new trigger/reply images evict the
oldest. Free slots fill from the newest other images. Images over the limit,
failed downloads and images on `vision: false` models become
`[image: filename]` placeholders.

`core/ai_images.py` keeps an LRU of normalized JPEG/PNG bytes (64 entries,
64 MiB), keyed by attachment ID or by URL without query/fragment. Failed loads
are cached for 30 seconds. At most four downloads run at once, and Pillow work
runs off the event loop. Only Discord CDN hosts are fetched, bodies are capped
at 8 MiB, images are scaled to at most 2048 pixels, and redirects are refused.
A signed-URL 403/404 refreshes the source message once, then retries.

## Usage and cost logs

Each model request logs an `llm_usage` line with prompt, cached prompt,
completion and reasoning tokens and an estimated cost. Token counts are read
from the provider response; reasoning tokens are reported separately and not
added to completion tokens again.

Each chat turn also appends one row to `logs/agent_tools/YYYY-MM-DD.jsonl`
(`core/tool_telemetry.py`): the ordered tool calls with status and timing,
plus `requests`, `prompt_tokens`, `cached_prompt_tokens`,
`completion_tokens`, `reasoning_tokens` and `est_cost_usd`. Rows never hold
arguments or results. `venv/bin/python scripts/tool_usage.py [--days N]
[--guild ID] [--model NAME]` summarizes them, with a per-model table.

Cost uses the model's `cost_per_mtok_input` and `cost_per_mtok_output` from
`ai_providers` (cached input priced by `cache_input_ratio`) when both are set;
otherwise the built-in table in `core/llm/usage.py`. An unknown model's cost
is `null`, not zero.
