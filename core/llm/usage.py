"""Usage and cost tracking for LLM calls.

The original cog read `response.usage` off the OpenAI SDK response but
never looked at it. This module gives that data a home: a small record
type plus a best-effort USD cost estimate and an in-memory aggregator that
cogs can use for `!aiinfo`-style reporting.

Pricing table is intentionally coarse (per-provider/model prefix, USD per
1M tokens) -- it exists to give a rough running total, not to reconcile
invoices. Unknown models return `None` for estimated cost rather than a
silently wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import time

# USD per 1,000,000 tokens: (prompt_price, completion_price).
# Deliberately coarse; update as pricing changes. Unmatched models fall
# through to None (unknown cost) rather than guessing.
_PRICING_USD_PER_MTOK: Dict[str, Dict[str, tuple]] = {
    # Verified 2026-07-04 against developers.openai.com/api/docs/pricing,
    # docs.x.ai/developers/models, and Anthropic's published pricing.
    "openai": {
        "gpt-5.5": (5.00, 30.00),
        "gpt-5.4": (2.50, 15.00),
        "gpt-5.4-mini": (0.75, 4.50),
        "gpt-5.4-nano": (0.20, 1.25),
    },
    "anthropic": {
        "claude-sonnet-5": (3.00, 15.00),
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-opus-4-8": (5.00, 25.00),
    },
    "xai": {
        "grok-4.3": (1.25, 2.50),
        "grok-4.20-0309-reasoning": (1.25, 2.50),
        "grok-4.20-0309-non-reasoning": (1.25, 2.50),
        "grok-4-fast": (0.20, 0.50),
    },
}


@dataclass
class UsageRecord:
    """Token usage for a single call, with an optional cost estimate."""
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    # Number of tool executions in the call (only meaningful for agent-loop
    # runs; 0 for plain chat). A zero here on an action request is the
    # "model narrated instead of acting" failure signature.
    tool_calls: int = 0


def estimate_cost(record: UsageRecord) -> Optional[float]:
    """Best-effort USD cost estimate for a UsageRecord. None if the model
    isn't in the pricing table (unknown, not zero)."""
    provider_prices = _PRICING_USD_PER_MTOK.get(record.provider)
    if not provider_prices:
        return None

    prices = provider_prices.get(record.model)
    if prices is None:
        # Try prefix match (e.g. "gpt-5-mini-2026-01-01" -> "gpt-5-mini").
        # Longest-prefix-first so specific variants (gpt-5-mini) win over
        # their broader parents (gpt-5) instead of being shadowed by them.
        matches = [
            (known_model, known_prices)
            for known_model, known_prices in provider_prices.items()
            if record.model.startswith(known_model)
        ]
        if matches:
            prices = max(matches, key=lambda pair: len(pair[0]))[1]

    if prices is None:
        return None

    prompt_price, completion_price = prices
    cost = (record.prompt_tokens / 1_000_000) * prompt_price
    cost += (record.completion_tokens / 1_000_000) * completion_price
    return round(cost, 6)
