"""Structured tool-usage telemetry for agentic runs.

One JSONL row per `run_agent_turn`, appended to a daily file under
logs/agent_tools/. The row keeps the ORDERED call sequence so frequency,
transition (bigram/Markov) and repeated-call analysis can be done offline
(scripts/tool_usage.py): a model repeatedly chaining the same calls is the
signal for a prompt problem or a missing bundled tool.

Each row also carries the run's model requests (round trips), token counts
and estimated cost, so a prompt or tool change can be compared before and
after on the same kind of request.

Rows carry tool names, statuses, timings and counters only — never arguments
or results, so SELF exchanges stay out of the file. Writing is best-effort: a
telemetry failure must never break the turn it describes.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional

TOOL_LOG_DIR = Path('logs/agent_tools')


def record_run(row: Dict, logger=None):
    """Append one run row to today's file; failures are logged, not raised."""
    try:
        TOOL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOOL_LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl", 'a') as f:
            f.write(json.dumps(row) + '\n')
    except Exception as error:
        if logger is not None:
            logger.warning(f"tool telemetry write failed: {error}")


def iter_runs(since: Optional[str] = None,
              directory: Optional[Path] = None) -> Iterator[Dict]:
    """Yield run rows oldest first; `since` is a YYYY-MM-DD lower bound.

    Corrupt lines are skipped so one half-written row cannot hide a day.
    """
    directory = directory or TOOL_LOG_DIR
    if not directory.exists():
        return
    for path in sorted(directory.glob('*.jsonl')):
        if since is not None and path.stem < since:
            continue
        with open(path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
