"""The Model plan of a design / brief solution (kernel-models.v2, phase 1).

The PM writes one fenced YAML block starting with `model_plan:` (skill assistant-model-routing); the PM executor parses it
when the document is submitted and keeps the result on the document record (`model_plan` or `model_plan_error`). Kernel
then decides deterministically — never from what a model says about setup:

- `gate_error`: may the task move to build / release? (required rows need a configured vendor; `defer_until: release`
  postpones a row to the release gate);
- `estimate`: a monthly budget estimate from `volume` × catalog prices (unknown stays unknown, never 0).

No block means the task calls no model, and nothing changes for it.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from assistant_kernel.core import model_catalog

BLOCK = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)
PURPOSE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ROW_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
MAX_ROWS = 50
PER_MONTH = {"per_day": 30.0, "per_week": 30.0 / 7, "per_month": 1.0}
UNITS = ("tokens", "calls", "turns", "audio_minutes", "characters")
BUILD_GATES = {("design", "build"), ("requirements", "build")}


def parse(markdown: str) -> list[dict[str, Any]] | None:
    """The plan's rows, normalised; None when the document has no model_plan block. Raises ValueError."""
    blocks = [b for b in BLOCK.findall(markdown or "") if re.match(r"\s*(#.*\n\s*)*model_plan\s*:", b)]
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ValueError("the document has more than one model_plan block; keep one")
    try:
        data = yaml.safe_load(blocks[0])
    except yaml.YAMLError as exc:
        raise ValueError(f"the model_plan block is not valid YAML: {str(exc)[:200]}") from exc
    rows = (data or {}).get("model_plan") if isinstance(data, dict) else None
    if rows in (None, []):
        return []
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError(f"model_plan must be a list of at most {MAX_ROWS} rows")
    return validate(rows)


def validate(rows: list[Any]) -> list[dict[str, Any]]:
    normalised, seen = [], set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"model_plan row {number} is not a mapping")
        row_id = str(row.get("id") or "").strip()
        where = f"model_plan row {row_id or number}"
        if not ROW_ID.fullmatch(row_id):
            raise ValueError(f"{where}: id must be short, like M1")
        if row_id in seen:
            raise ValueError(f"{where}: duplicate id")
        seen.add(row_id)
        capability = str(row.get("capability") or "").strip()
        purpose = str(row.get("purpose") or "").strip()
        if not PURPOSE.fullmatch(purpose):
            raise ValueError(f"{where}: purpose must be lowercase letters, digits, '.', '_' or '-', like daily-brief.write")
        try:
            name, spec = model_catalog.resolve_profile(capability, row.get("profile"))
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
        defer = row.get("defer_until")
        if defer not in (None, "", "release"):
            raise ValueError(f"{where}: defer_until can only be release")
        volume = row.get("volume")
        if volume is not None and not isinstance(volume, dict):
            raise ValueError(f"{where}: volume must be a mapping")
        if isinstance(volume, dict) and volume.get("unit") not in (None, *UNITS):
            raise ValueError(f"{where}: volume unit must be one of {', '.join(UNITS)}")
        normalised.append({
            "id": row_id, "step": str(row.get("step") or "")[:300], "used_by": str(row.get("used_by") or "")[:200],
            "capability": capability, "purpose": purpose, "profile": name, "vendor": spec["vendor"],
            "model": spec["model"], "required": row.get("required", True) is not False,
            "defer_until": "release" if defer == "release" else None, "volume": volume or {},
            "covers": [str(c) for c in row.get("covers") or []][:20]})
    if len({r["purpose"] for r in normalised}) != len(normalised):
        raise ValueError("model_plan: each row needs its own purpose")
    return normalised


def _monthly_calls(volume: dict[str, Any]) -> float | None:
    for key, factor in PER_MONTH.items():
        if isinstance(volume.get(key), (int, float)) and volume[key] >= 0:
            return float(volume[key]) * factor
    return None


def row_cost(row: dict[str, Any]) -> float | None:
    """Monthly CNY for one row, or None when volume or price is unknown."""
    volume, price = row.get("volume") or {}, model_catalog.PRICES.get(row.get("model") or "")
    calls = _monthly_calls(volume)
    if calls is None or not price:
        return None
    unit, size = volume.get("unit"), volume.get("size")
    number = lambda value: float(value) if isinstance(value, (int, float)) and value >= 0 else None   # noqa: E731
    if unit == "tokens" and number(size) is not None and "input" in price:
        output = number(volume.get("output_tokens")) or 0.0
        per_call = (max(number(size) - output, 0.0) * price["input"] + output * price["output"]) / 1e6
    elif unit in ("calls", "turns") and "input" in price \
            and number(volume.get("input_tokens")) is not None and number(volume.get("output_tokens")) is not None:
        per_call = (number(volume["input_tokens"]) * price["input"] + number(volume["output_tokens"]) * price["output"]) \
            / 1e6 * (number(size) or 1.0)
    elif unit == "audio_minutes" and number(size) is not None and "audio_minute" in price:
        per_call = number(size) * price["audio_minute"]
    elif unit == "characters" and number(size) is not None and "characters_10k" in price:
        per_call = number(size) / 10_000 * price["characters_10k"]
    else:
        return None
    return round(per_call * calls, 4)


def estimate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    costs = {row["id"]: row_cost(row) for row in rows}
    known = round(sum(c for c in costs.values() if c is not None), 2)
    unknown = [row_id for row_id, cost in costs.items() if cost is None]
    return {"monthly_cny": None if unknown else known, "known_monthly_cny": known, "unknown_rows": unknown,
            "rows": costs, "price_date": model_catalog.CHECKED, "note": "budget estimate, not a bill"}


def plan_document(task: dict[str, Any]) -> str:
    """Which document carries the plan: the brief solution on the quick track, the design otherwise."""
    return "task_contract" if task.get("track") == "quick" else "solution_design"


def gate_kind(task: dict[str, Any], target: str) -> str | None:
    if task.get("kind", "delivery") != "delivery":
        return None
    if (task.get("stage"), target) in BUILD_GATES:
        return "build"
    if target == "release":
        return "release"
    return None


def gaps(rows: list[dict[str, Any]], vendor_status: dict[str, dict[str, Any]], gate: str) -> list[dict[str, Any]]:
    """Required rows whose vendor is not usable at this gate."""
    from assistant_kernel.core.model_secrets import ready
    return [row for row in rows if row["required"] and (gate == "release" or row["defer_until"] != "release")
            and not ready(None, row["vendor"], vendor_status)]


def gate_error(task: dict[str, Any], target: str, vendor_status: dict[str, dict[str, Any]]) -> str | None:
    """Why the Model plan blocks this move (None = it does not). Pure: the caller passes model_secrets.status()."""
    gate = gate_kind(task, target)
    if gate is None:
        return None
    document = (task.get("documents") or {}).get(plan_document(task)) or {}
    if document.get("model_plan_error"):
        return f"the Model plan cannot be read: {document['model_plan_error']}"
    rows = document.get("model_plan")
    if not rows:
        return None
    missing = gaps(rows, vendor_status, gate)
    if not missing:
        return None
    vendors = sorted({row["vendor"] for row in missing})
    names = ", ".join(f"{v} ({(model_catalog.VENDORS.get(v) or {}).get('name', v)})" for v in vendors)
    rows_text = ", ".join(f"{row['id']} {row['capability']}" for row in missing)
    hint = ("the PM asks with a model_setup question" if gate == "build"
            else "the PM asks with a model_setup question; deferred rows must be configured now")
    return f"model setup missing for {rows_text}: vendor {names} is not configured ({hint})"


def gate_errors(settings, task: dict[str, Any], targets) -> dict[str, str]:
    """gate_error for several targets with one read of the vendor status (only when a plan exists)."""
    relevant = [t for t in targets if gate_kind(task, t)]
    document = (task.get("documents") or {}).get(plan_document(task)) or {}
    if not relevant or not (document.get("model_plan") or document.get("model_plan_error")):
        return {}
    from assistant_kernel.core import model_secrets
    current = model_secrets.status(settings)
    return {t: e for t in relevant if (e := gate_error(task, t, current))}


def catalog_view(settings, task: dict[str, Any] | None = None) -> dict[str, Any]:
    """What `kernel_model_catalog` returns: never a key."""
    from assistant_kernel.core import model_secrets
    current = model_secrets.status(settings)
    view: dict[str, Any] = {
        "capabilities": {c: {"default_profile": model_catalog.DEFAULT_PROFILE[c],
                             "available_via": list(model_catalog.AVAILABLE_VIA.get(c, ()))}
                         for c in model_catalog.CAPABILITIES},
        "profiles": {name: {**spec, "price_cny": model_catalog.PRICES.get(spec["model"])}
                     for name, spec in model_catalog.PROFILES.items()},
        "vendors": {v: {"name": (model_catalog.VENDORS.get(v) or {}).get("name", v), **s} for v, s in current.items()},
        "price_date": model_catalog.CHECKED,
        "price_note": f"budget estimates in CNY (USD at {model_catalog.USD_CNY}); not billing facts",
    }
    if task:
        document = (task.get("documents") or {}).get(plan_document(task)) or {}
        if document.get("model_plan_error"):
            view["task_plan"] = {"error": document["model_plan_error"]}
        elif document.get("model_plan"):
            rows = document["model_plan"]
            view["task_plan"] = {"document": plan_document(task), "rows": rows, "estimate": estimate(rows),
                                 "missing_for_build": [r["id"] for r in gaps(rows, current, "build")],
                                 "missing_for_release": [r["id"] for r in gaps(rows, current, "release")]}
        else:
            view["task_plan"] = None
    return view
