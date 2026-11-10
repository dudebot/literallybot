"""History window selection for Discord conversations.

Cache lifetime and hit rate are assumptions, not provider guarantees.
History is fetched again on each invocation to reflect edits and deletions.
"""
from collections import OrderedDict
from dataclasses import dataclass
import math


DEFAULT_HISTORY_MIN = 15
DEFAULT_HISTORY_MAX = 30
MAX_HISTORY = 1000


def history_bounds(config, ctx):
    minimum = config.get(ctx, "ai_history_min_messages", DEFAULT_HISTORY_MIN)
    maximum = config.get(ctx, "ai_history_max_messages", DEFAULT_HISTORY_MAX)
    try:
        minimum = max(1, min(MAX_HISTORY, int(minimum)))
        maximum = max(minimum, min(MAX_HISTORY, int(maximum)))
    except (TypeError, ValueError, OverflowError):
        minimum, maximum = DEFAULT_HISTORY_MIN, DEFAULT_HISTORY_MAX
    return minimum, maximum


def cache_policy(model_info):
    """Cached/full input price ratio and assumed retention, per provider/model."""
    try:
        ratio = float(model_info.get("cache_input_ratio", 0.10))
        seconds = float(model_info.get("cache_ttl_seconds", 300))
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise ValueError
        if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            raise ValueError
        return ratio, seconds
    except (ValueError, TypeError, OverflowError):
        return 0.10, 300.0


@dataclass
class HistoryState:
    """Last successful request: its first message and when it was sent."""

    anchor: int
    sent_at: float
    expires_at: float


class HistoryWindows:
    """Bounded map keyed by (guild, channel, provider, model, tool names).

    Remembers where the last successful request's history started, so the
    next one inside the cache lifetime can start at the same message and
    repeat the same prefix. Ordering follows successful commits, not reads
    or failed attempts.
    """

    def __init__(self, capacity=256):
        self.states = OrderedDict()
        self.capacity = capacity

    def previous(self, key, now, ttl):
        state = self.states.get(key)
        if state and 0 <= now - state.sent_at < ttl:
            return state
        self.states.pop(key, None)
        return None

    def choose(self, baseline, extended, previous):
        """Keep the previous anchor while it is live and the caller could
        still render it within the maximum; otherwise start a fresh window.

        A cost model used to decide this. With max = 2x min it chose the
        extension in nearly every case the caller offers one, so the rule is
        stated directly."""
        if previous is None or extended is None:
            return baseline
        return extended

    def commit(self, key, anchor, sent_at, *, ttl=300):
        # An extension retains the anchor, but always refreshes the time.
        for expired in [k for k, state in self.states.items() if state.expires_at <= sent_at]:
            del self.states[expired]
        self.states[key] = HistoryState(anchor, sent_at, sent_at + ttl)
        self.states.move_to_end(key)
        while len(self.states) > self.capacity:
            self.states.popitem(last=False)
