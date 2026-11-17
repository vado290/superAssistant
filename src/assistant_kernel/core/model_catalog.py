"""Built-in vendor and profile catalog (kernel-models.v2, phase 1): data only, no I/O.

Vendors are described by the protocols they speak, never by special code: any vendor with an OpenAI-compatible endpoint
can be added as a record (or used through `custom_openai`). What a record says a vendor supports is the vendor's claim;
a key is only accepted after `model_secrets.probe` (a real call).

Profiles are the named settings a Model plan row or a route points at; `DEFAULT_PROFILE` gives each capability its
default ("profile: default" in a plan). Prices are budget estimates with their source date — never billing facts.
"""
from __future__ import annotations

from typing import Any

CHECKED = "2026-11-16"           # date the prices below were read from the vendors' pricing pages
USD_CNY = 7.0                    # budget rate only, not today's exchange rate

CAPABILITIES = ("agent", "chat", "vision", "ocr", "asr", "tts", "embedding", "rerank")

VENDORS: dict[str, dict[str, Any]] = {
    "openai": {"name": "OpenAI", "auth": "codex_login",
               "protocols": {"openai_responses": "https://api.openai.com/v1"},
               "capabilities": ("agent", "chat", "vision")},
    "dashscope": {"name": "Alibaba Cloud Bailian (Qwen)", "auth": "api_key",
                  "protocols": {"openai_chat": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
                  "capabilities": ("chat", "vision", "ocr", "asr", "tts", "embedding", "rerank")},
    "volcengine": {"name": "Volcengine Ark (Doubao)", "auth": "api_key",
                   "protocols": {"openai_chat": "https://ark.cn-beijing.volces.com/api/v3"},
                   "capabilities": ("chat", "vision", "ocr")},
    "deepseek": {"name": "DeepSeek", "auth": "api_key",
                 "protocols": {"openai_chat": "https://api.deepseek.com/v1",
                               "anthropic_messages": "https://api.deepseek.com/anthropic"},
                 "capabilities": ("chat",)},
    "zhipu": {"name": "Zhipu GLM", "auth": "api_key",
              "protocols": {"openai_chat": "https://open.bigmodel.cn/api/paas/v4",
                            "anthropic_messages": "https://open.bigmodel.cn/api/anthropic"},
              "capabilities": ("chat", "vision")},
    "moonshot": {"name": "Moonshot Kimi", "auth": "api_key",
                 "protocols": {"openai_chat": "https://api.moonshot.cn/v1"}, "capabilities": ("chat",)},
    "openrouter": {"name": "OpenRouter (many overseas models, one key)", "auth": "api_key",
                   "protocols": {"openai_chat": "https://openrouter.ai/api/v1"}, "capabilities": ("chat", "vision")},
    "custom_openai": {"name": "Any OpenAI-compatible endpoint", "auth": "api_key", "needs_base_url": True,
                      "protocols": {"openai_chat": None}, "capabilities": ("chat", "vision")},
}

PROFILES: dict[str, dict[str, Any]] = {
    "agent.economy": {"capability": "agent", "engine": "codex", "vendor": "openai", "model": "gpt-6.1-sol",
                      "reasoning_effort": "medium"},
    "agent.standard": {"capability": "agent", "engine": "codex", "vendor": "openai", "model": "gpt-6.1-sol",
                       "reasoning_effort": "high"},
    "agent.quality": {"capability": "agent", "engine": "codex", "vendor": "openai", "model": "gpt-6-astra",
                      "reasoning_effort": "high"},
    "chat.economy": {"capability": "chat", "vendor": "dashscope", "model": "qwen3.8-flash"},
    "chat.quality": {"capability": "chat", "vendor": "dashscope", "model": "qwen4-max"},
    "vision": {"capability": "vision", "vendor": "dashscope", "model": "qwen3.8-flash"},
    "vision.quality": {"capability": "vision", "vendor": "dashscope", "model": "qwen3.7-plus"},
    "ocr": {"capability": "ocr", "vendor": "dashscope", "model": "qwen-vl-ocr"},
    "ocr.volcengine": {"capability": "ocr", "vendor": "volcengine", "model": "doubao-seed-2-0-mini-260428"},
    "asr": {"capability": "asr", "vendor": "dashscope", "model": "qwen3-asr-flash"},
    "tts": {"capability": "tts", "vendor": "dashscope", "model": "qwen3-tts-flash"},
    "embedding": {"capability": "embedding", "vendor": "dashscope", "model": "text-embedding-v4"},
    "rerank": {"capability": "rerank", "vendor": "dashscope", "model": "qwen3-rerank"},
}

DEFAULT_PROFILE = {"agent": "agent.standard", "chat": "chat.economy", "vision": "vision", "ocr": "ocr", "asr": "asr",
                   "tts": "tts", "embedding": "embedding", "rerank": "rerank"}

# What can call a capability today (phase 1). Empty = planned in the design, not callable yet (no ModelGateway).
AVAILABLE_VIA: dict[str, tuple[str, ...]] = {
    "agent": ("jobs.yaml agent job", "project agent run by Kernel"),
    "chat": ("kernel_model_call", "volcengine_generate (legacy)"),
    "ocr": ("kernel_model_call", "volcengine_ocr (legacy)"),
    "vision": ("kernel_model_call",), "asr": ("kernel_model_call (OpenAI chat audio)",), "tts": (),
    "embedding": ("kernel_model_call",), "rerank": ("kernel_model_call (DashScope)",),
}

# CNY. tokens: per 1M input / output tokens; audio: per minute; characters: per 10,000 characters.
PRICES: dict[str, dict[str, Any]] = {
    "gpt-6.1-sol": {"input": 2.0 * USD_CNY, "output": 10.0 * USD_CNY, "source": "OpenAI pricing (USD x7)"},
    "gpt-6-astra": {"input": 10.0 * USD_CNY, "output": 50.0 * USD_CNY, "source": "OpenAI pricing (USD x7)"},
    "qwen3.8-flash": {"input": 0.8, "output": 2.7, "source": "Bailian pricing"},
    "qwen4-max": {"input": 10.0, "output": 30.0, "source": "Bailian pricing"},
    "qwen3-asr-flash": {"audio_minute": 0.792 / 60, "source": "Bailian pricing"},
    "qwen3-tts-flash": {"characters_10k": 0.8, "source": "Bailian pricing"},
    "text-embedding-v4": {"input": 0.5, "output": 0.0, "source": "Bailian pricing"},
    "qwen3-rerank": {"input": 0.5, "output": 0.0, "source": "Bailian pricing"},
    "doubao-seed-2-0-mini-260428": {"input": 0.2, "output": 2.0, "source": "Volcengine Ark pricing"},
}


def profile(name: str) -> dict[str, Any] | None:
    return PROFILES.get(name)


def resolve_profile(capability: str, name: str | None) -> tuple[str, dict[str, Any]]:
    """The profile a plan row names ("default" or empty = the capability's default); raises ValueError."""
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}; one of {', '.join(CAPABILITIES)}")
    chosen = DEFAULT_PROFILE[capability] if name in (None, "", "default") else str(name)
    spec = PROFILES.get(chosen)
    if spec is None:
        raise ValueError(f"unknown profile {chosen!r}")
    if spec["capability"] != capability:
        raise ValueError(f"profile {chosen!r} is for {spec['capability']}, not {capability}")
    return chosen, spec


def base_url(vendor: str, protocol: str = "openai_chat") -> str | None:
    return ((VENDORS.get(vendor) or {}).get("protocols") or {}).get(protocol)
