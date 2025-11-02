# Vibe Generation Reference Patterns

- **Scoped config usage** – Access guild, user, and global state through the bot config helper so data lands in the right files (cogs/dynamic/media.py:32-58; cogs/static/admin.py:29-47).
- **Low-friction event listeners** – Exit early for bot/self messages, guard input length, and let the global `bot.on_message` drive command dispatch (`self.bot.process_commands` should not be called inside cogs) (cogs/dynamic/media.py:14-30; bot.py:115-121).
- **Permission and safety checks** – Validate actors before mutating state or performing admin work, and give clear feedback plus logging (cogs/static/admin.py:29-76).
- **Background task management** – Use `tasks.loop` plus `before_loop` and `cog_unload` to manage recurring jobs cleanly (cogs/dynamic/reminders.py:5-64).
- **External API resilience** – Adjust parameters per model family and surface friendly errors when providers change behaviour (cogs/dynamic/gpt.py:90-128).
- **Chunked messaging** – Batch large responses to respect Discord limits while keeping output readable (cogs/dynamic/media.py:78-122).
- **Structured logging** – Reuse `self.bot.logger` for successes, warnings, and failures to simplify triage (cogs/static/dev.py:24-104; bot.py:125-139).
