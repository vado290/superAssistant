"""Stable prompt layout and cache observability helpers.

The Kernel cannot assume that every vendor implements the same prompt-cache API. It
can, however, keep the stable prefix byte-for-byte stable and record the boundary
so each harness/provider can apply its own cache protocol safely.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptParts:
    stable_prefix: str
    dynamic_context: str = ""
    turn_input: str = ""
    output_contract: str = ""

    def render(self) -> str:
        return "\n\n".join(part.strip() for part in (
            self.stable_prefix, self.dynamic_context, self.turn_input, self.output_contract
        ) if part and part.strip())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metadata(parts: PromptParts, *, provider: str | None = None, model: str | None = None,
             session_reused: bool = False) -> dict[str, Any]:
    """Return non-sensitive cache facts suitable for run receipts and usage reports."""
    rendered = parts.render()
    stable = parts.stable_prefix.strip()
    return {
        "prompt_cache": {
            "layout": "stable_prefix_then_dynamic_context_then_turn_then_output",
            "stable_prefix_sha256": sha256(stable),
            "stable_prefix_chars": len(stable),
            "prompt_sha256": sha256(rendered),
            "provider": provider,
            "model": model,
            "session_reused": bool(session_reused),
        }
    }


def from_prompt(prompt: str, stable_prefix: str | None = None, *, provider: str | None = None,
                model: str | None = None, session_reused: bool = False) -> dict[str, Any]:
    """Build metadata for legacy callers that still pass one assembled prompt.

    A missing prefix is deliberately recorded as empty: callers must opt in to a
    stable boundary instead of Kernel guessing one from mutable text.
    """
    prefix = stable_prefix or ""
    matched = bool(prefix) and prompt.startswith(prefix)
    if not matched:
        prefix = ""
    remainder = prompt[len(prefix):].lstrip() if prefix else prompt
    parts = PromptParts(stable_prefix=prefix, dynamic_context=remainder)
    result = metadata(parts, provider=provider, model=model, session_reused=session_reused)
    result["prompt_cache"]["stable_prefix_match"] = matched
    return result
