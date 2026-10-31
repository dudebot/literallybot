"""Provider-agnostic LLM client package.

Public surface:
    LLMClient       - async client: chat(), run_agent(), discover_models()
    ProviderConfig  - resolved provider/model config for a call site
    LLMResponse     - text + usage/cost result of a chat() call
    UsageRecord     - per-call token/cost usage
    PROVIDER_ALIASES, DEFAULT_PROVIDER - shared constants
"""

from .client import (
    LLMClient,
    ProviderConfig,
    LLMResponse,
    AgentRunLimitExceeded,
    PROVIDER_ALIASES,
    DEFAULT_PROVIDER,
)
from .usage import UsageRecord

__all__ = [
    "LLMClient",
    "ProviderConfig",
    "LLMResponse",
    "AgentRunLimitExceeded",
    "UsageRecord",
    "PROVIDER_ALIASES",
    "DEFAULT_PROVIDER",
]
