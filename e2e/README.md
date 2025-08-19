# E2E harness for LiterallyBot

This folder contains a pragmatic end-to-end harness that can:
- Start the bot against a Discord test guild using a bot token
- Drive scenarios by creating a temporary Channel Webhook and posting commands through it
- Wait for the bot’s reply in-channel and assert basics (content/timing)
- Assert on the reply text and timing

Why webhooks?
- Posting messages with the Bot token makes the message authored by the bot, and your bot ignores its own messages.
- Webhook messages are authored by a webhook user, so your bot will process prefix commands.

Notes:
- Optional: runs only when you set env vars.
- Needs a test server where the bot has permission to Manage Webhooks and Read/Send Messages in the channel.
- Uses Discord REST + simple polling; no gateway connection from the harness.

## Config via env vars

Set the following environment variables (or put them in a `.env` file and source it):

- DISCORD_TOKEN: Bot token to run the bot
- E2E_GUILD_ID: Target guild ID
- E2E_CHANNEL_ID: Text channel ID where tests run
- E2E_OWNER_ID: Your Discord user ID (for admin-only commands where needed)

## Quick run (Windows)

- Open two terminals.
- Terminal A: start the bot (same as production): use the VS Code task "Run Bot" or run it yourself.
- Terminal B: run the harness:

```powershell
python .\e2e\harness.py
```

## What it does

- Creates a temporary webhook in the target channel.
- Posts `!echo` and `!ping` via the webhook, so the bot will process them.
- Provides utilities to wait for the bot message and basic assertions.

This is intentionally simple and avoids heavy frameworks.
