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
        "grok-4.5": (2.00, 6.00),   # verified 2026-07 (x.ai)
        "grok-4.3": (1.25, 2.50),
        "grok-4.20-0309-reasoning": (1.25, 2.50),
        "grok-4.20-0309-non-reasoning": (1.25, 2.50),
        "grok-4-fast": (0.20, 0.50),
    },
}


def _match_prices(provider: str, model: str) -> Optional[tuple]:
    """(prompt_price, completion_price) per Mtok for a model — exact match,
    then longest-prefix (so specific variants win over broader parents).
    None if the model isn't in the table (unknown, not zero)."""
    provider_prices = _PRICING_USD_PER_MTOK.get(provider)
    if not provider_prices:
        return None
    prices = provider_prices.get(model)
    if prices is None:
        matches = [(known, p) for known, p in provider_prices.items()
                   if model.startswith(known)]
        if matches:
            prices = max(matches, key=lambda kv: len(kv[0]))[1]
    return prices


def known_output_price(provider: str, model: str) -> Optional[float]:
    """Best-effort $/Mtok OUTPUT price, or None if unknown. The public seam
    for cost-tier seeding (/ai settings cooldown tiers) — same matching
    rules as estimate_cost, so the seeded tier and the usage estimate can
    never disagree about a model's price. Local providers bill no tokens:
    free by definition, which maps to the cheap tier."""
    if provider == "ollama":
        return 0.0
    prices = _match_prices(provider, model)
    return prices[1] if prices else None


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
    prices = _match_prices(record.provider, record.model)
    if prices is None:
        return None

    prompt_price, completion_price = prices
    cost = (record.prompt_tokens / 1_000_000) * prompt_price
    cost += (record.completion_tokens / 1_000_000) * completion_price
    return round(cost, 6)
