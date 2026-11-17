"""Purpose-bound product calls. Agent execution remains in the Codex harness."""
from __future__ import annotations

from typing import Any
import hashlib

import httpx

from assistant_kernel.core import model_secrets, models
from assistant_kernel.model_providers.chat import CatalogChatProvider, PromptCachePolicy
from assistant_kernel.model_providers.qwen import QwenProvider

PROTOCOL_TYPES = {"openai_chat": "openai_compatible", "openai_responses": "openai_responses",
                  "anthropic_messages": "anthropic_native", "gemini": "gemini_native"}


def availability(settings, route: dict) -> dict:
    capability, protocol = route["capability"], route["protocol"]
    adapter = (capability == "agent" and route["spec"].get("engine", "codex") == "codex") or (
        capability == "chat" and protocol in PROTOCOL_TYPES) or (
        capability in {"vision", "ocr", "embedding", "asr"} and protocol == "openai_chat") or (
        capability == "rerank" and route["provider"] == "dashscope")
    configured = model_secrets.ready(settings, route["provider"])
    credential = model_secrets.credential(settings, route["provider"]) if capability != "agent" else None
    if capability != "agent":
        configured = configured and credential is not None
    endpoint = route.get("base_url") or (credential.openai_compatible_base_url if credential else None)
    parameters = bool(route["model"] and (capability == "agent" or endpoint))
    if capability == "rerank":
        parameters = parameters and bool(credential and (credential.rerank_base_url or credential.workspace_id))
    return {"adapter_available": adapter, "configured": configured, "parameters_complete": parameters,
            "callable": bool(adapter and configured and parameters),
            "verified": bool(model_secrets.capability_evidence(settings, route)) if capability != "agent" else False}


def resolve(settings, project_id: str, purpose: str, capability: str, *, profile=None) -> dict:
    data = models.load(settings)
    bindings = ((data.get("projects") or {}).get(project_id) or {}).get("purposes") or {}
    if profile is None and purpose not in bindings:
        raise ValueError("this model purpose has no released binding")
    return models.route(settings, capability, project_id=project_id, purpose=purpose, profile=profile, data=data)


async def _call(settings, project_id: str, purpose: str, capability: str, inputs: dict[str, Any], *, profile=None) -> dict:
    """Only internal preflight may supply profile; products call their released purpose."""
    route = resolve(settings, project_id, purpose, capability, profile=profile)
    state = availability(settings, route)
    if not state["callable"] or capability == "agent":
        raise ValueError(f"model capability is not callable through this gateway: {state}")
    credential = model_secrets.credential(settings, route["provider"])
    if credential is None:
        raise ValueError("provider instance needs its own configured credential")
    model = route["model"]
    config = credential.model_copy(deep=True)
    config.provider_type = PROTOCOL_TYPES.get(route["protocol"], config.provider_type)
    if route.get("base_url"):
        config.openai_compatible_base_url = route["base_url"]
    config.chat_models = {model: {**(config.chat_models.get(model) or {}), "model_id": model}}
    if capability == "chat":
        system_prompt = str(inputs.get("system_prompt") or "")
        cache_key = hashlib.sha256(
            f"{project_id}\0{purpose}\0{route['provider']}\0{model}\0{system_prompt}".encode()
        ).hexdigest()
        cache_policy = PromptCachePolicy(enabled=bool(system_prompt), cache_key=cache_key)
        try:
            result = await CatalogChatProvider(route["provider"], config).chat(
                str(inputs.get("prompt") or ""), model, system_prompt=system_prompt,
                max_output_tokens=int(inputs.get("max_output_tokens", 2048)), cache_policy=cache_policy)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"provider call failed (HTTP {exc.response.status_code})") from None
        except httpx.HTTPError:
            raise RuntimeError("provider connection failed") from None
        output = result.model_dump()
    elif capability == "rerank":
        config.models.reranker = model
        output = (await QwenProvider(config).rerank(inputs["query"], inputs["documents"], inputs.get("top_n"))).model_dump()
    else:
        endpoint = config.openai_compatible_base_url.rstrip("/")
        if capability == "embedding":
            payload = {"model": model, "input": inputs["texts"], "encoding_format": "float"}
            if "dimensions" in inputs:
                payload["dimensions"] = int(inputs["dimensions"])
            path = "/embeddings"
        else:
            content = [{"type": "text", "text": str(inputs.get("prompt") or "Transcribe accurately." if capability == "asr" else inputs.get("prompt") or "Describe the image accurately.")}]
            if capability in {"vision", "ocr"}:
                url = str(inputs["image_url"])
                if not url.startswith(("https://", "data:image/")):
                    raise ValueError("image input must be HTTPS or an image data URL")
                content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                audio = str(inputs["audio"])
                if not audio.startswith(("https://", "data:audio/")):
                    raise ValueError("audio input must be HTTPS or an audio data URL")
                content.append({"type": "input_audio", "input_audio": {"data": audio}})
            payload = {"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": int(inputs.get("max_output_tokens", 2048))}
            path = "/chat/completions"
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(endpoint + path, json=payload,
                                         headers={"Authorization": "Bearer " + config.api_key.get_secret_value()})
            # Do not include provider response bodies or credential-bearing URLs in tool errors.
            if not response.is_success:
                raise RuntimeError(f"provider call failed (HTTP {response.status_code}); previous route retained")
            raw = response.json()
        output = {"data": raw.get("data") if capability == "embedding" else raw.get("choices"),
                  "usage": CatalogChatProvider.normalize_usage(raw.get("usage"))}
    evidence = model_secrets.capability_evidence(settings, route, success=True)
    return {"route": {k: v for k, v in route.items() if k != "spec"}, "result": output, "evidence": evidence}


async def call(settings, project_id: str, purpose: str, capability: str, inputs: dict[str, Any], *, profile=None) -> dict:
    try:
        return await _call(settings, project_id, purpose, capability, inputs, profile=profile)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"provider call failed (HTTP {exc.response.status_code})") from None
    except httpx.HTTPError:
        raise RuntimeError("provider connection failed") from None
