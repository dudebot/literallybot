# Normalization Action Plan

Scope: foundation parity between this bot and its companion repo without cross-linking code or issues.

## Goals
- Core permission helpers: single normalized API
  - get_superadmins(config) always returns list[int]
  - is_superadmin(config, user_id) and is_superadmin(ctx)
  - is_admin(config, ctx) and is_admin(ctx)
- Config parity: treat ctx=None as global; consistent guild/user/global resolution.
- Error logging: keep current scaffold here; upgrade to per-guild + global with full coverage.
- Dev/Admin cogs: align names and UX across repos.
- Scripts: consistent local start script; keep service installer where needed.
- Tests: minimal unit tests around normalization.

## Completed (this commit set)
- Core utils normalized with dual-call signatures and list coercion.
- Error logging scaffold added and wired (on_command_error, on_error, task error).
- Added start.sh for local runs.
- Added basic unit test for superadmin/admin normalization.

## Pending / TODO
- Error logging upgrade (tracked in this repo’s issue #56):
  - Per-guild error channel with global fallback
  - on_app_command_error coverage; loop exception handler for background tasks
  - Rate-limit by (context, type, message[:100], scope)
  - Unit tests: channel resolution + rate limiter
- Dev/Admin cogs: reconcile command names/aliases and UX
  - Choose canonical command set; update docs accordingly
- CI guard (optional): check for drift in foundation files (bot.py, core/, cogs/static/{admin,dev,error_handler}.py, start.sh)
- Docs refresh after upgrades

## Rollout
- This repo: OK to test in prod behind admin-only commands.
- Companion repo: continue changes in its dev repo first, then promote.

## Quick Tests
- `python3 -m unittest -q tests.test_utils_superadmin`
- Manual: set `error_channel_id` via `!seterrorlog #channel` and force an error (e.g., `!testerror`).
