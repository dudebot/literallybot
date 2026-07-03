"""Provider-agnostic LLM client package.

Public surface:
    LLMClient       - async client: chat(), chat_stream(), discover_models()
    ProviderConfig  - resolved provider/model config for a call site
    LLMResponse     - text + usage/cost result of a chat() call
    UsageRecord     - per-call token/cost usage
    UsageTracker    - lightweight in-memory usage aggregator
    PROVIDER_ALIASES, DEFAULT_PROVIDER - shared constants
"""

from .client import (
    LLMClient,
    ProviderConfig,
    LLMResponse,
    PROVIDER_ALIASES,
    DEFAULT_PROVIDER,
)
from .usage import UsageRecord, UsageTracker, estimate_cost

__all__ = [
    "LLMClient",
    "ProviderConfig",
    "LLMResponse",
    "UsageRecord",
    "UsageTracker",
    "estimate_cost",
    "PROVIDER_ALIASES",
    "DEFAULT_PROVIDER",
]
