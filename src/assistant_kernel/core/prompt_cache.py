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
    result["prompt_cache"]["stable_prefix_match"] = None if session_reused else matched
    result["prompt_cache"]["turn_kind"] = "resumed_delta" if session_reused else "fresh"
    return result


def merge_results(first, last):
    """Keep context/final answer from last, and all invocation receipts from repairs."""
    from dataclasses import replace
    receipts = []
    seen = set()
    for result in (first, last):
        entries = result.metadata.get("invocations") or [{
            "invocation_id": result.archive_uri,
            "usage": result.metadata.get("invocation_usage"),
            "source": result.metadata.get("invocation_usage_source", "unknown")}]
        for entry in entries:
            key = entry.get("invocation_id")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            receipts.append(entry)
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "cache_creation_input_tokens")
    totals = {field: sum((r.get("usage") or {})[field] for r in receipts)
              if all((r.get("usage") or {}).get(field) is not None for r in receipts) else None
              for field in fields}
    totals["complete"] = all(totals[k] is not None for k in ("input_tokens", "output_tokens"))
    return replace(last, events=first.events + last.events,
                   metadata={**last.metadata, "invocations": receipts, "run_usage": totals})


def cumulative_delta(after, before, *, fresh=False, same_session=True):
    """Codex counters are session totals. Missing/reset baselines never become free usage."""
    after, before = after or {}, before or {}
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    result = {}
    for key in fields:
        current = after.get(key)
        baseline = 0 if fresh else before.get(key)
        result[key] = current - baseline if (same_session and isinstance(current, int) and
                         isinstance(baseline, int) and current >= baseline >= 0) else None
    return result


def preserve_failed_repair(exc, first):
    """Attach safe known evidence to the existing run error; failure semantics stay unchanged."""
    metadata = dict(getattr(exc, "model_metadata", {}) or first.metadata)
    entries = list(first.metadata.get("invocations") or [])
    entries.extend(metadata.get("invocations") or [] if metadata != first.metadata else [])
    entries.append({"invocation_id": None, "status": "failed_repair", "usage": None, "source": "unknown"})
    metadata.update(invocations=entries, run_usage={"complete": False, "input_tokens": None,
                                                  "cached_input_tokens": None, "output_tokens": None})
    exc.model_metadata = metadata
