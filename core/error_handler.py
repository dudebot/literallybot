"""
Simple error logging utility for Discord channel integration.
"""

import discord
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict

# Simple rate limiting storage
_error_timestamps: Dict[str, datetime] = {}
_rate_limit_minutes = 5


def _should_send_error(error_key: str) -> bool:
    now = datetime.now()
    if error_key not in _error_timestamps:
        _error_timestamps[error_key] = now
        return True
    last_sent = _error_timestamps[error_key]
    if now - last_sent >= timedelta(minutes=_rate_limit_minutes):
        _error_timestamps[error_key] = now
        return True
    return False


def _create_error_key(error: Exception, context: str) -> str:
    error_type = type(error).__name__
    error_msg = str(error)[:100]
    return f"{context}:{error_type}:{error_msg}"


async def log_error_to_discord(bot, error: Exception, context: str, extra_info: str = ""):
    if not hasattr(bot, 'config'):
        return
    error_channel_id = bot.config.get_global("error_channel_id")
    if not error_channel_id:
        return
    error_key = _create_error_key(error, context)
    if not _should_send_error(error_key):
        return
    channel = bot.get_channel(error_channel_id)
    if not channel:
        return
    embed = discord.Embed(
        title="🚨 System Error Detected",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Error Type", value=f"`{type(error).__name__}`", inline=True)
    embed.add_field(name="Context", value=f"`{context}`", inline=True)
    error_msg = str(error)
    if len(error_msg) > 1000:
        error_msg = error_msg[:1000] + "..."
    embed.add_field(name="Error Message", value=f"```{error_msg}```", inline=False)
    if extra_info:
        if len(extra_info) > 1000:
            extra_info = extra_info[:1000] + "..."
        embed.add_field(name="Additional Info", value=f"```{extra_info}```", inline=False)
    tb = traceback.format_exc()
    if len(tb) > 1000:
        tb = "..." + tb[-1000:]
    embed.add_field(name="Traceback", value=f"```python\n{tb}\n```", inline=False)
    embed.set_footer(text="Error Logging System")
    try:
        await channel.send(embed=embed)
    except Exception:
        pass
