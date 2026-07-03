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
    "openai": {
        "gpt-5": (1.25, 10.00),
        "gpt-5-mini": (0.25, 2.00),
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
        "o3": (2.00, 8.00),
        "o4-mini": (1.10, 4.40),
    },
    "anthropic": {
        "claude-opus-4-20250514": (15.00, 75.00),
        "claude-sonnet-4-20250514": (3.00, 15.00),
        "claude-3-5-haiku-latest": (0.80, 4.00),
    },
    "xai": {
        "grok-4": (3.00, 15.00),
        "grok-4-fast": (0.20, 0.50),
        "grok-4-fast-non-reasoning": (0.20, 0.50),
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


def estimate_cost(record: UsageRecord) -> Optional[float]:
    """Best-effort USD cost estimate for a UsageRecord. None if the model
    isn't in the pricing table (unknown, not zero)."""
    provider_prices = _PRICING_USD_PER_MTOK.get(record.provider)
    if not provider_prices:
        return None

    prices = provider_prices.get(record.model)
    if prices is None:
        # Try prefix match (e.g. "gpt-5-mini-2026-01-01" -> "gpt-5-mini").
        for known_model, known_prices in provider_prices.items():
            if record.model.startswith(known_model):
                prices = known_prices
                break

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
