import asyncio
from typing import Any, Dict, List, Optional

import openai


def _is_reasoning_model(model: str) -> bool:
    return model.startswith("o3") or model.startswith("o4") or model in {"o1", "o1-preview", "o1-mini"}


async def call_chat_completion(
    *,
    api_key: str,
    base_url: Optional[str],
    model: str,
    messages: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    provider_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Call an OpenAI-compatible Chat Completions endpoint and return the assistant message text.

    Parameters:
    - api_key: API key for the provider
    - base_url: Optional base URL for OpenAI-compatible providers
    - model: Model name
    - messages: List of role/content dicts
    - metadata: Optional metadata to pass through
    - provider_info: Provider configuration dict; used to look up per-model token limits
    """
    # Create client
    if base_url:
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
    else:
        client = openai.OpenAI(api_key=api_key)

    # Build params
    create_params: Dict[str, Any] = {
        "messages": messages,
        "model": model,
    }

    # Optional metadata/store (ignored by some providers)
    if metadata is not None:
        create_params["metadata"] = metadata
    # Preserve existing behavior
    create_params["store"] = True

    # Token handling for reasoning models
    if _is_reasoning_model(model):
        models_info = (provider_info or {}).get("models", {})
        model_info = models_info.get(model, {})
        max_completion_tokens = model_info.get("max_completion_tokens", 3000)
        create_params["max_completion_tokens"] = max_completion_tokens
    else:
        create_params["max_tokens"] = 3000

    # Execute in thread to avoid blocking
    chat_completion = await asyncio.to_thread(
        client.chat.completions.create,
        **create_params,
    )
    return chat_completion.choices[0].message.content.strip()
