"""Provider-agnostic LLM client, backed by pydantic-ai.

Owns provider config resolution, chat completions (OpenAI-compatible and
Anthropic), streaming, model discovery, and usage/cost tracking. Callers
(cogs) should not talk to provider SDKs directly for chat completions --
go through `LLMClient`.

The public surface (LLMClient, LLMResponse, ProviderConfig, the
call_ai_api/call_anthropic_api aliases) is unchanged from the pre-pydantic-ai
implementation. Internally, requests are built as pydantic-ai messages and
executed via `pydantic_ai.direct.model_request(_stream)`, which keeps this
layer a thin client while leaving a clear upgrade path to tool-calling:
`ModelRequestParameters.function_tools` already carries OpenAI-style tool
schemas converted to `ToolDefinition`s, and a future agent loop can reuse
`_build_model()` to construct `pydantic_ai.Agent(model, tools=...)` directly.

Notable behavior notes vs. the old implementation:
- The o3/o4/gpt-5/o1 `max_completion_tokens` heuristics and the
  max_tokens<->max_completion_tokens retry-swap are gone: pydantic-ai model
  profiles decide which wire field `max_tokens` maps to per provider/model.
  Ollama's OpenAI-compat endpoint ignores `max_completion_tokens` (verified
  against a live server), so the ollama model is built with a profile
  override that forces the plain `max_tokens` wire field.
- Per-model token caps still come from config: `max_completion_tokens`
  wins over `max_tokens`, falling back to DEFAULT_MAX_TOKENS.
- `reasoning_effort` from model config is passed via the
  `openai_reasoning_effort` model setting and reaches the wire verbatim
  (including the non-standard "none" used to disable qwen3.5 thinking).
- `metadata` passthrough uses `extra_body={"metadata": ...}` plus
  `openai_store=True`, producing the same request JSON as the old
  `metadata=`/`store=` SDK kwargs. Anthropic drops metadata, as before.
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
import openai

from pydantic_ai import Agent
from pydantic_ai.direct import model_request, model_request_stream
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest as PaiModelRequest,
    ModelResponse as PaiModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool, ToolDefinition
from pydantic_ai.usage import RequestUsage, UsageLimits

from .usage import UsageRecord, estimate_cost

# Provider aliases (also used by the cog for command-level aliasing).
PROVIDER_ALIASES: Dict[str, str] = {
    "oai": "openai",
    "claude": "anthropic",
    "anth": "anthropic",
}

DEFAULT_PROVIDER = "xai"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MAX_TOKENS = 3000


@dataclass
class ProviderConfig:
    """Resolved provider/model configuration for a single call site.

    Mirrors the dict shape the old `get_provider_config` returned, kept as a
    dict-like dataclass so existing call sites using `provider_config["x"]`
    style access still work if needed (see __getitem__ below).
    """
    provider: str
    model: Optional[str]
    provider_info: Dict[str, Any]
    all_providers: Dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class LLMResponse:
    """Result of a chat call, including usage/cost tracking."""
    text: str
    provider: str
    model: str
    usage: Optional[UsageRecord] = None
    raw: Any = None


class LLMClient:
    """Provider-agnostic async LLM client.

    Tool-call capable: pass `tools` (OpenAI-style tool schema) through to
    `chat()`. Streaming-capable: `chat_stream()` yields text deltas; it is
    not currently wired into any Discord-facing command.
    """

    def __init__(self, config, logger=None):
        """
        Args:
            config: the bot's config accessor (same object as `bot.config`),
                used for `ai_providers`, per-guild current provider/model,
                and API keys.
            logger: optional logger; falls back to a no-op logger if absent.
        """
        self.config = config
        self.logger = logger or _NullLogger()

    # ------------------------------------------------------------------
    # Provider/model resolution
    # ------------------------------------------------------------------

    def get_provider_config(self, ctx) -> ProviderConfig:
        """Get the current provider configuration for a guild."""
        config = self.config

        all_providers = config.get(None, "ai_providers", scope="global") or {}

        current_provider = config.get(ctx, "current_ai_provider") or DEFAULT_PROVIDER
        current_provider = PROVIDER_ALIASES.get(current_provider, current_provider)

        current_model = config.get(ctx, "current_ai_model") or None

        provider_info = all_providers.get(current_provider)
        if not provider_info:
            self.logger.warning(
                f"Provider {current_provider} not found in config, falling back to {DEFAULT_PROVIDER}"
            )
            current_provider = DEFAULT_PROVIDER
            provider_info = all_providers.get(DEFAULT_PROVIDER, {})

        if not current_model:
            current_model = provider_info.get("default_model")

        return ProviderConfig(
            provider=current_provider,
            model=current_model,
            provider_info=provider_info,
            all_providers=all_providers,
        )

    def _get_api_key(self, provider: str) -> Optional[str]:
        api_key_name = f"{provider.upper()}_API_KEY"
        return self.config.get(None, api_key_name, scope="global") or os.environ.get(api_key_name)

    def _resolve_api_key(self, provider: str, provider_info: Dict[str, Any]) -> str:
        """Resolve the API key for a provider, honoring `requires_api_key: false`.

        Local/keyless providers (e.g. a local Ollama server) opt out of the
        key requirement via "requires_api_key": false. Defaults to True so
        xai/openai/anthropic keep requiring a real key. The underlying SDK
        still needs a non-empty string against keyless local servers; the
        value itself is never checked.
        """
        api_key = self._get_api_key(provider)
        if not api_key:
            if not provider_info.get("requires_api_key", True):
                api_key = "not-needed"
            else:
                raise ValueError(f"No API key found for provider {provider}")
        return api_key

    # ------------------------------------------------------------------
    # pydantic-ai request assembly
    # ------------------------------------------------------------------

    def _build_model(
        self,
        provider: str,
        model: str,
        provider_info: Dict[str, Any],
        api_key: str,
    ) -> Model:
        """Construct the pydantic-ai model for a provider/model pair.

        A future agent loop should reuse this to build
        `pydantic_ai.Agent(self._build_model(...), tools=[...])`.
        """
        api_type = provider_info.get("api_type", "openai")

        if api_type == "anthropic":
            # The configured base_url points at the messages endpoint, not a
            # base URL; the old implementation hardcoded the endpoint too, so
            # the provider default is used deliberately.
            return AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))

        base_url = provider_info.get("base_url")
        if provider == "ollama":
            # Ollama's OpenAI-compat endpoint silently ignores
            # `max_completion_tokens` (verified live), so force the plain
            # `max_tokens` wire field. The override merges on top of the
            # provider's per-model profile (qwen/llama/... detection).
            return OpenAIChatModel(
                model,
                provider=OllamaProvider(base_url=base_url, api_key=api_key),
                profile=OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False),
            )

        if base_url:
            # Third-party OpenAI-compatible endpoints (xai/grok, and others
            # reached via base_url) expect the plain `max_tokens` wire field;
            # pydantic-ai defaults to `max_completion_tokens` (OpenAI's newer
            # field), which xai currently tolerates but does not document. Force
            # `max_tokens` unless the model's config explicitly opted into
            # `max_completion_tokens` (an o-series/gpt-5-style cap). The old
            # hand-rolled client sent `max_tokens` here and had a retry-swap
            # fallback that this migration dropped; this restores the wire shape.
            model_info = (provider_info.get("models", {}) or {}).get(model, {})
            if "max_completion_tokens" not in model_info:
                return OpenAIChatModel(
                    model,
                    provider=OpenAIProvider(base_url=base_url, api_key=api_key),
                    profile=OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False),
                )
            return OpenAIChatModel(model, provider=OpenAIProvider(base_url=base_url, api_key=api_key))
        return OpenAIChatModel(model, provider=OpenAIProvider(api_key=api_key))

    def _build_settings(
        self,
        provider_info: Dict[str, Any],
        model: str,
        metadata: Optional[Dict],
        tool_choice: Optional[Any] = None,
    ) -> ModelSettings:
        """Build pydantic-ai model settings from per-model config.

        The per-model token cap comes from config (`max_completion_tokens`
        wins over `max_tokens`); pydantic-ai's model profile decides which
        wire field it maps to, replacing the old prefix heuristics and the
        fallback-on-error retry swap.
        """
        api_type = provider_info.get("api_type", "openai")
        models_info = provider_info.get("models", {})
        model_info = models_info.get(model, {})

        max_tokens = model_info.get(
            "max_completion_tokens", model_info.get("max_tokens", DEFAULT_MAX_TOKENS)
        )
        settings: Dict[str, Any] = {"max_tokens": max_tokens}

        if api_type != "anthropic":
            # Thinking models (e.g. qwen3.5 via ollama) can burn the entire
            # token budget on reasoning and return empty content; "none"
            # disables it. Passed through verbatim via openai_reasoning_effort.
            if "reasoning_effort" in model_info:
                settings["openai_reasoning_effort"] = model_info["reasoning_effort"]

            # Same wire JSON as the old `metadata=`/`store=True` SDK kwargs.
            # (Anthropic's API has no equivalent; the old code dropped
            # metadata there as well.)
            if metadata is not None:
                settings["extra_body"] = {"metadata": metadata}
                settings["openai_store"] = True

        if tool_choice is not None:
            mapped_choice = _map_tool_choice(tool_choice)
            if mapped_choice is not None:
                settings["tool_choice"] = mapped_choice

        return settings  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Chat completion (non-streaming)
    # ------------------------------------------------------------------

    async def chat(
        self,
        provider_config: ProviderConfig,
        messages: List[Dict],
        metadata: Optional[Dict] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """Call the appropriate AI API based on provider configuration.

        Returns an LLMResponse with usage/cost tracking populated when the
        provider returns usage data. `text` is always a string ("" when the
        model returned no content, e.g. a thinking model that spent its
        whole budget reasoning) -- never None, never an exception for empty.
        """
        provider = provider_config.provider
        model = provider_config.model
        provider_info = provider_config.provider_info

        api_key = self._resolve_api_key(provider, provider_info)

        pai_model = self._build_model(provider, model, provider_info, api_key)
        settings = self._build_settings(provider_info, model, metadata, tool_choice=tool_choice)
        request_parameters = _build_request_parameters(tools)

        response = await model_request(
            pai_model,
            _to_pai_messages(messages),
            model_settings=settings,
            model_request_parameters=request_parameters,
        )

        text = "".join(
            part.content for part in response.parts if isinstance(part, TextPart)
        ).strip()
        usage = _usage_from_pai(response.usage, provider=provider, model=model)

        return LLMResponse(text=text, provider=provider, model=model, usage=usage, raw=response)

    # ------------------------------------------------------------------
    # Agent loop (multi-turn tool calling)
    # ------------------------------------------------------------------

    async def run_agent(
        self,
        provider_config: ProviderConfig,
        messages: List[Dict],
        tools: List[Tool],
        metadata: Optional[Dict] = None,
        user_prompt: Optional[str] = None,
        max_tool_calls: int = 8,
    ) -> LLMResponse:
        """Run a multi-turn tool-calling agent loop via `pydantic_ai.Agent`.

        Shares request assembly with `chat()` (`_build_model` /
        `_build_settings` / `_to_pai_messages`), so provider/model/token-cap
        /metadata behavior is identical to a plain chat call. `tools` are
        pydantic-ai `Tool` instances (see core/agent_loop.py, which
        generates them from the ops registry).

        The loop is bounded: at most `max_tool_calls` tool executions
        (pydantic-ai raises `UsageLimitExceeded` beyond that — callers
        should catch it and degrade gracefully). Usage aggregates across
        every request in the run, so cost tracking covers the whole loop.
        """
        provider = provider_config.provider
        model = provider_config.model
        provider_info = provider_config.provider_info

        api_key = self._resolve_api_key(provider, provider_info)

        pai_model = self._build_model(provider, model, provider_info, api_key)
        settings = self._build_settings(provider_info, model, metadata)

        agent = Agent(model=pai_model, tools=tools, model_settings=settings)
        result = await agent.run(
            user_prompt,
            message_history=_to_pai_messages(messages),
            usage_limits=UsageLimits(
                tool_calls_limit=max_tool_calls,
                # Belt-and-suspenders: also cap total model requests so a
                # model that loops without tool calls can't spin either.
                request_limit=max_tool_calls + 2,
            ),
        )

        # RunUsage aggregates across every request in the loop; it is an
        # attribute in pydantic-ai 2.x (not the v1 `.usage()` method) and
        # duck-types RequestUsage's input/output token fields.
        usage = _usage_from_pai(result.usage, provider=provider, model=model)
        text = (result.output or "").strip()
        return LLMResponse(text=text, provider=provider, model=model, usage=usage, raw=result)

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def discover_models(self, provider: str, api_key: str, provider_info: Dict) -> List[str]:
        """Attempt to discover available models from provider API."""
        discovered = []

        try:
            if provider == "xai":
                client = openai.OpenAI(api_key=api_key, base_url=provider_info.get("base_url"))
                models = await asyncio.to_thread(client.models.list)

                for model in models.data:
                    model_id = model.id
                    discovered.append(model_id)

                    if "fast" in model_id.lower() or "mini" in model_id.lower():
                        multiplier = 0.5
                    elif "reasoning" in model_id.lower():
                        multiplier = 1.5
                    else:
                        multiplier = 1.0

                    models_dict = provider_info.get("models", {})
                    if model_id not in models_dict:
                        models_dict[model_id] = {"timeout_multiplier": multiplier}
                        provider_info["models"] = models_dict

            elif provider == "openai":
                client = openai.OpenAI(api_key=api_key)
                models = await asyncio.to_thread(client.models.list)

                for model in models.data:
                    model_id = model.id
                    if not (model_id.startswith("gpt-") or model_id.startswith("o") or model_id.startswith("chatgpt")):
                        continue

                    discovered.append(model_id)

                    if "mini" in model_id.lower():
                        multiplier = 0.5
                    elif model_id.startswith("o3") or model_id.startswith("o4"):
                        multiplier = 2.0
                    elif "turbo" in model_id.lower():
                        multiplier = 0.7
                    else:
                        multiplier = 1.0

                    models_dict = provider_info.get("models", {})
                    if model_id not in models_dict:
                        model_cfg = {"timeout_multiplier": multiplier}
                        if model_id.startswith("o"):
                            model_cfg["max_completion_tokens"] = 16000
                        models_dict[model_id] = model_cfg
                        provider_info["models"] = models_dict

            elif provider == "anthropic":
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_API_VERSION,
                        "content-type": "application/json",
                    }
                    data = {
                        "model": "claude-haiku-4-5",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 1,
                    }
                    async with session.post(
                        ANTHROPIC_MESSAGES_URL,
                        headers=headers,
                        json=data,
                    ) as response:
                        if response.status == 200:
                            self.logger.info("Anthropic API key validated successfully")
                        else:
                            raise ValueError(f"API key validation failed: {await response.text()}")

            if discovered:
                all_providers = self.config.get(None, "ai_providers", scope="global") or {}
                all_providers[provider] = provider_info
                self.config.set(None, "ai_providers", all_providers, scope="global")

            return discovered

        except Exception as e:
            self.logger.error(f"Failed to discover models for {provider}: {e}", exc_info=True)
            raise


class _NullLogger:
    """No-op logger used when the caller doesn't supply one."""
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _to_pai_messages(messages: List[Dict]) -> List[ModelMessage]:
    """Convert OpenAI-style role/content dicts to pydantic-ai messages.

    Consecutive system/user messages are grouped into a single ModelRequest;
    assistant messages become ModelResponses. Order is preserved 1:1 on the
    wire.
    """
    pai_messages: List[ModelMessage] = []
    request_parts: List[Any] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "assistant":
            if request_parts:
                pai_messages.append(PaiModelRequest(parts=request_parts))
                request_parts = []
            pai_messages.append(PaiModelResponse(parts=[TextPart(content=content)]))
        elif role == "system":
            request_parts.append(SystemPromptPart(content=content))
        else:
            request_parts.append(UserPromptPart(content=content))

    if request_parts:
        pai_messages.append(PaiModelRequest(parts=request_parts))

    return pai_messages


def _build_request_parameters(tools: Optional[List[Dict]]) -> Optional[ModelRequestParameters]:
    """Convert OpenAI-style tool schemas to pydantic-ai request parameters."""
    if not tools:
        return None

    function_tools = []
    for tool in tools:
        # Accept both {"type": "function", "function": {...}} and bare {...}.
        fn = tool.get("function", tool)
        function_tools.append(
            ToolDefinition(
                name=fn["name"],
                description=fn.get("description"),
                parameters_json_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return ModelRequestParameters(function_tools=function_tools)


def _map_tool_choice(tool_choice: Any) -> Optional[Any]:
    """Map OpenAI-style tool_choice to pydantic-ai's ModelSettings.tool_choice."""
    if isinstance(tool_choice, str) and tool_choice in ("none", "auto", "required"):
        return tool_choice
    if isinstance(tool_choice, dict):
        # {"type": "function", "function": {"name": "x"}} -> ["x"]
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return [name]
    return None


def _usage_from_pai(usage: Optional[RequestUsage], provider: str, model: str) -> Optional[UsageRecord]:
    if usage is None:
        return None
    prompt_tokens = usage.input_tokens or 0
    completion_tokens = usage.output_tokens or 0
    record = UsageRecord(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        # RunUsage (agent loop) carries tool_calls; RequestUsage (plain
        # chat) doesn't have the attribute — default to 0.
        tool_calls=getattr(usage, "tool_calls", 0) or 0,
    )
    record.estimated_cost_usd = estimate_cost(record)
    return record
