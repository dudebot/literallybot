"""DM transcript storage.

Inbound DMs already arrive at the bot (bot.py on_message) but were previously
log-only. This module persists them as per-user JSONL so an agent can read a
conversation back later.

Rows are append-only. Both directions are stored so a reader sees the whole
conversation, distinguished by `direction`:
    "in"  — user -> bot
    "out" — bot -> user

Timestamps are naive local time (Discord returns UTC-aware; callers convert
before handing values here) so string comparison against `since` orders
correctly — except across a DST fallback, and except for ties. `message_id`
is stored on every row precisely so callers can cursor on it instead:
snowflakes are monotonic, so `after_id` is the lossless poll cursor.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

DM_LOG_DIR = Path('logs/dms')


def _dm_file(user_id: int) -> Path:
    return DM_LOG_DIR / f'user_{user_id}.jsonl'


def row_from_message(message, human_user_id: int) -> Dict:
    """Build a transcript row from a discord.Message.

    `human_user_id` is the HUMAN side of the conversation regardless of
    direction — that is what makes a per-user file a conversation rather
    than a mailbox. Direction is derived from authorship. Attachments keep
    filename + CDN url so image/file DMs survive as more than empty content
    (note Discord CDN urls are signed and expire after ~24h).
    """
    return {
        "timestamp": message.created_at.astimezone().replace(tzinfo=None).isoformat(),
        "direction": "in" if message.author.id == human_user_id else "out",
        "user_id": human_user_id,
        "author_id": message.author.id,
        "message_id": message.id,
        "content": message.content,
        "attachments": [
            {"filename": a.filename, "url": a.url}
            for a in (message.attachments or [])
        ],
    }


def log_dm(user_id: int, row: Dict):
    """Append one DM row to the user's transcript."""
    DM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_dm_file(user_id), 'a') as f:
        f.write(json.dumps(row) + '\n')


def load_dms(user_id: int, limit: Optional[int] = None,
             since: Optional[str] = None,
             after_id: Optional[int] = None) -> List[Dict]:
    """Load a user's DM transcript, oldest first.

    `after_id` is a message id; only rows with a strictly greater id are
    returned. This is the poll cursor — monotonic and tie-proof, unlike
    timestamps. `since` (ISO timestamp string, rows at or before it skipped)
    is kept for coarse human-readable filtering but can drop a message that
    shares its timestamp with the cursor row.

    `limit` semantics depend on the read mode: with `after_id` set the
    OLDEST N surviving rows are kept, so repeated polls walk forward
    losslessly even when the backlog exceeds one page (advance the cursor
    to the last returned row's id). Without it, the most recent N are
    kept — the "what did they say lately" tail read.

    A corrupt line is skipped rather than failing the whole read — a
    half-written row must not make an entire conversation unreadable. A
    missing file is a legitimately empty transcript ([]); an unreadable
    file RAISES, because "they never wrote" and "the read failed" must not
    look identical to a caller deciding whether to re-contact someone.
    """
    path = _dm_file(user_id)
    if not path.exists():
        return []

    rows: List[Dict] = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if after_id is not None:
                try:
                    if int(row.get('message_id') or 0) <= after_id:
                        continue
                except (TypeError, ValueError):
                    continue
            if since is not None and str(row.get('timestamp', '')) <= since:
                continue
            rows.append(row)

    if limit is not None and len(rows) > limit:
        rows = rows[:limit] if after_id is not None else rows[-limit:]
    return rows


def list_dm_users() -> List[int]:
    """User ids that have a stored transcript."""
    if not DM_LOG_DIR.exists():
        return []
    ids = []
    for p in DM_LOG_DIR.glob('user_*.jsonl'):
        try:
            ids.append(int(p.stem[len('user_'):]))
        except ValueError:
            continue
    return sorted(ids)
