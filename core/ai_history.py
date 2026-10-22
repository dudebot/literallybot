"""Cost-aware history selection for Discord conversations.

Cache lifetime and hit rate are assumptions, not provider guarantees. Compare
complete serialized messages: editing an earlier message invalidates its suffix.
History is fetched again on each invocation to reflect edits and deletions.
"""
from collections import OrderedDict
from dataclasses import dataclass
import math
import hashlib


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


def estimate_message_tokens(message):
    """Cheap nonblocking cost proxy, not an invoice tokenizer (UTF-8 / 4)."""
    return (4 + math.ceil(len(message.get("content", "").encode("utf-8")) / 4)
            + 1792 * len(message.get("images", ())))


def comparison_message(message):
    """Detach comparison state from image bytes without changing equality."""
    snapshot = dict(message)
    if 'images' in snapshot:
        snapshot['images'] = [
            (hashlib.sha256(image['data']).hexdigest(), image['media_type'], len(image['data']))
            if isinstance(image, dict) else image
            for image in snapshot['images']]
    return snapshot


def prefix_tokens(previous, current):
    total = 0
    for old, new in zip(previous, current):
        if comparison_message(old) != comparison_message(new):
            break
        total += estimate_message_tokens(new)
    return total


def input_units(messages, previous, ratio):
    """Input cost in units of one full-price token."""
    total = sum(map(estimate_message_tokens, messages))
    cached = prefix_tokens(previous, messages)
    return total - (1 - ratio) * cached


@dataclass
class HistoryState:
    """Last successful request: anchor, comparison snapshot, and lifetime."""

    anchor: int
    messages: list
    sent_at: float
    expires_at: float


class HistoryWindows:
    """Bounded map keyed by (guild, channel, provider, model, tool names).

    Keep rendered text and image fingerprints to detect changed prefixes;
    IDs alone cannot do that. OrderedDict provides expected O(1) lookup/update/oldest eviction.
    Ordering follows successful commits, not reads or failed attempts.
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

    def choose(self, baseline, extended, previous, ratio):
        """Keep the anchor only when its matching prefix outweighs extra input.

        The caller bounds both candidates and provides the *current* rendering.
        On equal costs prefer the shorter baseline. No output cost is assumed
        to change as a result of retaining more conversation.
        """
        if previous is None or extended is None:
            return baseline
        old = previous.messages
        if input_units(extended, old, ratio) < input_units(baseline, old, ratio):
            return extended
        return baseline

    def commit(self, key, anchor, messages, sent_at, *, ttl=300):
        # An extension retains the anchor, but always refreshes prompt/time.
        for expired in [k for k, state in self.states.items() if state.expires_at <= sent_at]:
            del self.states[expired]
        self.states[key] = HistoryState(
            anchor, [comparison_message(message) for message in messages], sent_at, sent_at + ttl)
        self.states.move_to_end(key)
        while len(self.states) > self.capacity:
            self.states.popitem(last=False)
