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


class UsageTracker:
    """Simple in-memory aggregator for usage records.

    Not persisted -- resets on bot restart. Intended for lightweight
    `!aiinfo`-style running totals, not billing reconciliation.
    """

    def __init__(self, max_records: int = 1000):
        self._records: List[UsageRecord] = []
        self._max_records = max_records

    def record(self, usage: Optional[UsageRecord]) -> None:
        if usage is None:
            return
        self._records.append(usage)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]

    def totals(self, provider: Optional[str] = None) -> Dict[str, float]:
        """Aggregate token/cost totals, optionally filtered by provider."""
        records = self._records if provider is None else [r for r in self._records if r.provider == provider]
        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        total_tokens = sum(r.total_tokens for r in records)
        known_costs = [r.estimated_cost_usd for r in records if r.estimated_cost_usd is not None]
        total_cost = sum(known_costs) if known_costs else 0.0
        return {
            "calls": len(records),
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(total_cost, 6),
            "cost_unknown_calls": len(records) - len(known_costs),
        }

    def recent(self, limit: int = 10) -> List[UsageRecord]:
        return self._records[-limit:]
