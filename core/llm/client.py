"""Provider-agnostic LLM client.

Extracted from cogs/dynamic/gpt.py. Owns provider config resolution, the
OpenAI-compatible call path, the Anthropic call path, model discovery, and
usage/cost tracking. Callers (cogs) should not talk to `openai`/`aiohttp`
directly for chat completions anymore -- go through `LLMClient`.

Behavior is preserved 1:1 from the original cog methods:
- get_provider_config
- call_ai_api (OpenAI-compatible path)
- call_anthropic_api
- discover_models
- the o3/o4/gpt-5/o1 -> max_completion_tokens heuristic and its
  max_tokens <-> max_completion_tokens fallback-on-error retry
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
import openai

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
    `chat()`. Streaming-capable in shape: `chat_stream()` yields text deltas;
    it is not currently wired into any Discord-facing command.
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
        provider returns usage data.
        """
        provider = provider_config.provider
        model = provider_config.model
        provider_info = provider_config.provider_info

        api_key = self._get_api_key(provider)
        if not api_key:
            raise ValueError(f"No API key found for provider {provider}")

        api_type = provider_info.get("api_type", "openai")

        if api_type == "anthropic":
            return await self._call_anthropic_api(
                api_key, model, messages, metadata, provider=provider,
                tools=tools, tool_choice=tool_choice,
            )
        else:
            return await self._call_openai_compat_api(
                api_key, model, messages, metadata, provider_info, provider=provider,
                tools=tools, tool_choice=tool_choice,
            )

    # Backwards-compatible alias matching the original cog method name.
    async def call_ai_api(self, provider_config: ProviderConfig, messages: List[Dict], metadata: Dict) -> str:
        """Deprecated-shape helper: returns plain text like the original cog method."""
        response = await self.chat(provider_config, messages, metadata)
        return response.text

    async def _call_openai_compat_api(
        self,
        api_key: str,
        model: str,
        messages: List[Dict],
        metadata: Optional[Dict],
        provider_info: Dict[str, Any],
        provider: str,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        base_url = provider_info.get("base_url")
        if base_url:
            client = openai.OpenAI(api_key=api_key, base_url=base_url)
        else:
            client = openai.OpenAI(api_key=api_key)

        create_params: Dict[str, Any] = {
            "messages": messages,
            "model": model,
        }
        if metadata is not None:
            create_params["metadata"] = metadata
            create_params["store"] = True

        if tools:
            create_params["tools"] = tools
            if tool_choice is not None:
                create_params["tool_choice"] = tool_choice

        models_info = provider_info.get("models", {})
        model_info = models_info.get(model, {})

        uses_completion_tokens = (
            model.startswith("o3")
            or model.startswith("o4")
            or model.startswith("gpt-5")
            or model in {"o1", "o1-preview", "o1-mini"}
            or "max_completion_tokens" in model_info
        )

        if uses_completion_tokens:
            create_params["max_completion_tokens"] = model_info.get("max_completion_tokens", DEFAULT_MAX_TOKENS)
        else:
            create_params["max_tokens"] = model_info.get("max_tokens", DEFAULT_MAX_TOKENS)

        try:
            chat_completion = await asyncio.to_thread(
                client.chat.completions.create,
                **create_params
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "max_tokens" in error_msg or "max_completion_tokens" in error_msg:
                self.logger.warning(f"Token parameter error for model {model}, attempting fallback: {e}")

                if "max_tokens" in create_params:
                    token_value = create_params.pop("max_tokens")
                    create_params["max_completion_tokens"] = token_value
                    self.logger.info("Retrying with max_completion_tokens instead of max_tokens")
                elif "max_completion_tokens" in create_params:
                    token_value = create_params.pop("max_completion_tokens")
                    create_params["max_tokens"] = token_value
                    self.logger.info("Retrying with max_tokens instead of max_completion_tokens")

                chat_completion = await asyncio.to_thread(
                    client.chat.completions.create,
                    **create_params
                )
            else:
                raise

        text = chat_completion.choices[0].message.content.strip()
        usage = _usage_from_openai(chat_completion, provider=provider, model=model)

        return LLMResponse(text=text, provider=provider, model=model, usage=usage, raw=chat_completion)

    # Backwards-compatible alias matching the original cog method signature/behavior.
    async def call_anthropic_api(self, api_key: str, model: str, messages: List[Dict], metadata: Dict) -> str:
        response = await self._call_anthropic_api(api_key, model, messages, metadata, provider="anthropic")
        return response.text

    async def _call_anthropic_api(
        self,
        api_key: str,
        model: str,
        messages: List[Dict],
        metadata: Optional[Dict],
        provider: str = "anthropic",
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """Call Anthropic's Claude API."""
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        system_message = None
        claude_messages = []

        for msg in messages:
            if msg["role"] == "system":
                system_message = msg["content"]
            else:
                role = "assistant" if msg["role"] == "assistant" else "user"
                claude_messages.append({
                    "role": role,
                    "content": msg["content"],
                })

        data: Dict[str, Any] = {
            "model": model,
            "messages": claude_messages,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }

        if system_message:
            data["system"] = system_message

        if tools:
            data["tools"] = tools
            if tool_choice is not None:
                data["tool_choice"] = tool_choice

        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_MESSAGES_URL,
                headers=headers,
                json=data,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise ValueError(f"Anthropic API error: {error_text}")

                result = await response.json()
                text = result["content"][0]["text"]
                usage = _usage_from_anthropic(result, provider=provider, model=model)

                return LLMResponse(text=text, provider=provider, model=model, usage=usage, raw=result)

    # ------------------------------------------------------------------
    # Streaming (shape-only for now; not wired to Discord)
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        provider_config: ProviderConfig,
        messages: List[Dict],
        metadata: Optional[Dict] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Any] = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas for a chat call.

        Only the OpenAI-compatible path streams natively today. The
        Anthropic path falls back to a single non-streaming call and yields
        its full text as one chunk -- correct output, just not incremental.
        Not currently called from any Discord command; provided so the
        client shape supports streaming when a caller wires it up.
        """
        provider = provider_config.provider
        model = provider_config.model
        provider_info = provider_config.provider_info
        api_type = provider_info.get("api_type", "openai")

        if api_type == "anthropic":
            response = await self.chat(provider_config, messages, metadata, tools=tools, tool_choice=tool_choice)
            yield response.text
            return

        api_key = self._get_api_key(provider)
        if not api_key:
            raise ValueError(f"No API key found for provider {provider}")

        base_url = provider_info.get("base_url")
        client = openai.OpenAI(api_key=api_key, base_url=base_url) if base_url else openai.OpenAI(api_key=api_key)

        create_params: Dict[str, Any] = {
            "messages": messages,
            "model": model,
            "stream": True,
        }
        if tools:
            create_params["tools"] = tools
            if tool_choice is not None:
                create_params["tool_choice"] = tool_choice

        models_info = provider_info.get("models", {})
        model_info = models_info.get(model, {})
        uses_completion_tokens = (
            model.startswith("o3")
            or model.startswith("o4")
            or model.startswith("gpt-5")
            or model in {"o1", "o1-preview", "o1-mini"}
            or "max_completion_tokens" in model_info
        )
        if uses_completion_tokens:
            create_params["max_completion_tokens"] = model_info.get("max_completion_tokens", DEFAULT_MAX_TOKENS)
        else:
            create_params["max_tokens"] = model_info.get("max_tokens", DEFAULT_MAX_TOKENS)

        def _make_stream():
            return client.chat.completions.create(**create_params)

        stream = await asyncio.to_thread(_make_stream)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

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
                        "model": "claude-3-5-haiku-latest",
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


def _usage_from_openai(chat_completion, provider: str, model: str) -> Optional[UsageRecord]:
    usage = getattr(chat_completion, "usage", None)
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or (prompt_tokens + completion_tokens)
    record = UsageRecord(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    record.estimated_cost_usd = estimate_cost(record)
    return record


def _usage_from_anthropic(result: Dict[str, Any], provider: str, model: str) -> Optional[UsageRecord]:
    usage = result.get("usage")
    if not usage:
        return None
    prompt_tokens = usage.get("input_tokens", 0) or 0
    completion_tokens = usage.get("output_tokens", 0) or 0
    record = UsageRecord(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    record.estimated_cost_usd = estimate_cost(record)
    return record
