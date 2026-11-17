from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote
from typing import Any

import httpx
from pydantic import BaseModel, Field

from assistant_kernel.config.settings import ProviderCredential


@dataclass(frozen=True)
class PromptCachePolicy:
    """Vendor-neutral cache intent; adapters only emit supported protocol fields."""

    enabled: bool = False
    cache_key: str | None = None
    # auto uses the provider protocol. Explicit none is useful for diagnostics.
    protocol: str = "auto"


class ChatResult(BaseModel):
    provider: str
    model: str
    text: str
    usage: dict = Field(default_factory=dict)
    request_id: str = ""


class CatalogChatProvider:
    """Unified async chat caller with conservative, protocol-specific prompt caching."""

    def __init__(self, provider_name: str, config: ProviderCredential, timeout_seconds: float = 180):
        self.provider_name = provider_name
        self.config = config
        self.timeout_seconds = timeout_seconds

    def _api_key(self) -> str:
        key = self.config.api_key.get_secret_value()
        if not self.config.enabled or not key:
            raise RuntimeError(f"provider {self.provider_name} is not configured")
        return key

    def _model_id(self, model: str) -> tuple[str, dict]:
        model_config = self.config.chat_models.get(model)
        if model_config is None:
            raise ValueError(f"unknown model {model!r} for provider {self.provider_name}")
        if ('model_id' in model_config or 'endpoint_id' in model_config) and not (model_config.get('endpoint_id') or model_config.get('model_id')):
            raise ValueError('The selected model identifier is explicitly unconfigured')
        return str(model_config.get("endpoint_id") or model_config.get("model_id") or model), model_config

    def _base_url(self) -> str:
        base = self.config.openai_compatible_base_url or self.config.native_base_url
        if not base:
            raise RuntimeError(f"provider {self.provider_name} has no base URL")
        return base.rstrip("/")

    @staticmethod
    def normalize_usage(raw: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize common OpenAI, Anthropic and Gemini usage spellings without dropping raw fields."""
        raw = dict(raw or {})
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        cached = (
            raw.get("cached_input_tokens")
            or raw.get("cache_read_input_tokens")
            or raw.get("cachedContentTokenCount")
            or details.get("cached_tokens")
            or 0
        )
        cache_write = raw.get("cache_creation_input_tokens") or raw.get("cache_write_input_tokens") or 0
        input_tokens = raw.get("input_tokens") or raw.get("prompt_tokens") or raw.get("promptTokenCount") or 0
        output_tokens = raw.get("output_tokens") or raw.get("completion_tokens") or raw.get("candidatesTokenCount") or 0
        normalized = dict(raw)
        normalized.update({
            "input_tokens": int(input_tokens),
            "cached_input_tokens": int(cached),
            "cache_creation_input_tokens": int(cache_write),
            "output_tokens": int(output_tokens),
            "cache_hit": bool(cached),
            "cache_hit_ratio": round(int(cached) / int(input_tokens), 4) if int(input_tokens) else None,
            "usage_source": "provider_response",
        })
        return normalized

    @staticmethod
    def _cache_protocol(policy: PromptCachePolicy | None, provider_type: str, model_config: dict) -> str:
        if not policy or not policy.enabled or policy.protocol == "none":
            return "none"
        if policy.protocol != "auto":
            return policy.protocol
        if provider_type == "anthropic_native":
            return "anthropic"
        if provider_type == "openai_responses" or str(model_config.get("api_style") or "") == "responses":
            return "openai_responses"
        if provider_type == "openai_compatible" and model_config.get("prompt_cache") == "openai":
            return "openai_chat"
        return "none"

    async def chat(
        self,
        prompt: str,
        model: str,
        system_prompt: str = "",
        max_output_tokens: int = 2048,
        temperature: float | None = None,
        cache_policy: PromptCachePolicy | None = None,
    ) -> ChatResult:
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if not 1 <= max_output_tokens <= 131072:
            raise ValueError("max_output_tokens must be between 1 and 131072")
        model_id, model_config = self._model_id(model)
        provider_type = self.config.provider_type
        protocol = self._cache_protocol(cache_policy, provider_type, model_config)
        if provider_type == "gemini_native":
            return await self._gemini(prompt, model_id, system_prompt, max_output_tokens, temperature)
        if provider_type == "anthropic_native":
            return await self._anthropic(prompt, model_id, system_prompt, max_output_tokens, temperature, protocol == "anthropic")
        api_style = str(model_config.get("api_style") or "")
        if provider_type == "openai_responses" or api_style == "responses":
            return await self._responses(prompt, model_id, system_prompt, max_output_tokens, temperature,
                                         cache_policy if protocol == "openai_responses" else None)
        return await self._chat_completions(prompt, model_id, system_prompt, max_output_tokens, temperature,
                                            cache_policy if protocol == "openai_chat" else None)

    async def _responses(self, prompt: str, model: str, system: str, max_tokens: int,
                         temperature: float | None, cache_policy: PromptCachePolicy | None) -> ChatResult:
        payload: dict = {"model": model, "input": prompt, "max_output_tokens": max_tokens}
        if system:
            payload["instructions"] = system
        if cache_policy and cache_policy.cache_key:
            payload["prompt_cache_key"] = cache_policy.cache_key
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._post_openai("/responses", payload, model, responses=True)

    async def _chat_completions(self, prompt: str, model: str, system: str, max_tokens: int,
                                temperature: float | None, cache_policy: PromptCachePolicy | None) -> ChatResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if cache_policy and cache_policy.cache_key:
            payload["prompt_cache_key"] = cache_policy.cache_key
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._post_openai("/chat/completions", payload, model, responses=False)

    async def _post_openai(self, path: str, payload: dict, model: str, *, responses: bool) -> ChatResult:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url()}{path}",
                headers={"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()
        if responses:
            text = str(raw.get("output_text") or "")
            if not text:
                pieces = []
                for output in raw.get("output", []):
                    for content in output.get("content", []):
                        if content.get("type") in {"output_text", "text"}:
                            pieces.append(str(content.get("text", "")))
                text = "".join(pieces)
        else:
            text = str(raw.get("choices", [{}])[0].get("message", {}).get("content") or "")
        return ChatResult(provider=self.provider_name, model=model, text=text,
                          usage=self.normalize_usage(raw.get("usage") or {}),
                          request_id=response.headers.get("x-request-id", ""))

    async def _anthropic(self, prompt: str, model: str, system: str, max_tokens: int,
                         temperature: float | None, cache_enabled: bool) -> ChatResult:
        system_value: str | list[dict[str, Any]] = system
        if cache_enabled and system:
            system_value = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        payload: dict = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system_value:
            payload["system"] = system_value
        if temperature is not None:
            payload["temperature"] = temperature
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url()}/messages",
                headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()
        text = "".join(str(block.get("text", "")) for block in raw.get("content", []) if block.get("type") == "text")
        return ChatResult(provider=self.provider_name, model=model, text=text,
                          usage=self.normalize_usage(raw.get("usage") or {}),
                          request_id=response.headers.get("request-id", ""))

    async def _gemini(self, prompt: str, model: str, system: str, max_tokens: int, temperature: float | None) -> ChatResult:
        generation: dict = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            generation["temperature"] = temperature
        payload: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self._base_url()}/models/{quote(model, safe='')}:generateContent"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, params={"key": self._api_key()}, json=payload)
            response.raise_for_status()
            raw = response.json()
        text = "".join(
            str(part.get("text", ""))
            for candidate in raw.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        )
        return ChatResult(provider=self.provider_name, model=model, text=text,
                          usage=self.normalize_usage(raw.get("usageMetadata") or {}),
                          request_id=response.headers.get("x-request-id", ""))
