"""kernel-models.v2 phase 1: Model plan parsing, estimates, gates, the vendor key store and model setup answers."""
from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from assistant_kernel.core import model_plan, model_secrets

DESIGN = """# Design

## 3 Design points

| id | step | capability | profile |
|---|---|---|---|
| M1 | transcribe | asr | default |

```yaml
# model-plan
model_plan:
  - id: M1
    step: transcribe the daily voice memo
    used_by: job:daily-brief
    capability: asr
    purpose: daily-brief.transcribe
    profile: default
    volume: {per_day: 1, unit: audio_minutes, size: 30}
    required: true
  - id: M2
    step: write the brief
    used_by: agent:brief-writer
    capability: agent
    purpose: daily-brief.write
    profile: agent.economy
    volume: {per_day: 1, unit: turns, size: 1}
```
"""


@pytest.fixture
def settings(tmp_path):
    auth = tmp_path / "vaults" / "p" / "runtime" / "codex-home" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    return SimpleNamespace(data_root=tmp_path, provider_secrets_path=tmp_path / "config" / "provider-secrets.json",
                           shared_volcengine_path=tmp_path / "shared-models" / "volcengine.json")


def task_with(rows, *, stage="design", track="full", kind="delivery", error=None):
    plan_doc = "task_contract" if track == "quick" else "solution_design"
    documents = {"task_contract": {"sha256": "a"}, "solution_design": {"sha256": "b"}}
    documents[plan_doc] = {**documents[plan_doc], **({"model_plan": rows} if rows is not None else {}),
                           **({"model_plan_error": error} if error else {})}
    return {"task_id": "t1", "kind": kind, "stage": stage, "track": track, "documents": documents,
            "approvals": {"requirements": {"doc_sha": "a"}}}


def test_parse_normalises_rows_and_resolves_default_profiles():
    rows = model_plan.parse(DESIGN)
    assert [r["id"] for r in rows] == ["M1", "M2"]
    assert rows[0]["profile"] == "asr" and rows[0]["vendor"] == "dashscope" and rows[0]["required"] is True
    assert rows[1]["profile"] == "agent.economy" and rows[1]["vendor"] == "openai"


def test_no_block_means_no_plan_and_bad_blocks_are_errors():
    assert model_plan.parse("# Design\n\n```yaml\nfoo: 1\n```\n") is None
    with pytest.raises(ValueError, match="more than one"):
        model_plan.parse(DESIGN + "\n```yaml\nmodel_plan: []\n```\n")
    with pytest.raises(ValueError, match="is for asr"):
        model_plan.parse("```yaml\nmodel_plan:\n  - {id: M1, capability: chat, purpose: a.b, profile: asr}\n```")
    with pytest.raises(ValueError, match="purpose"):
        model_plan.parse("```yaml\nmodel_plan:\n  - {id: M1, capability: chat, purpose: Has Spaces}\n```")
    with pytest.raises(ValueError, match="own purpose"):
        model_plan.parse("```yaml\nmodel_plan:\n  - {id: M1, capability: chat, purpose: a}\n"
                         "  - {id: M2, capability: chat, purpose: a}\n```")


def test_estimate_keeps_unknown_unknown():
    estimate = model_plan.estimate(model_plan.parse(DESIGN))
    assert estimate["rows"]["M1"] == pytest.approx(30 * 30 * 0.792 / 60, rel=1e-3)
    assert estimate["unknown_rows"] == ["M2"]          # turns without token sizes cannot be priced
    assert estimate["monthly_cny"] is None and estimate["known_monthly_cny"] > 0


def test_gate_blocks_build_until_vendor_configured_and_defer_moves_it_to_release(settings):
    rows = model_plan.parse(DESIGN)
    status = model_secrets.status(settings)
    assert status["openai"]["configured"] is True and status["dashscope"]["configured"] is False
    error = model_plan.gate_error(task_with(rows), "build", status)
    assert error and "M1 asr" in error and "dashscope" in error and "M2" not in error
    deferred = [{**rows[0], "defer_until": "release"}, rows[1]]
    assert model_plan.gate_error(task_with(deferred), "build", status) is None
    assert "M1" in model_plan.gate_error(task_with(deferred, stage="review"), "release", status)
    model_secrets.put(settings, "dashscope", "sk-test", validated=True)
    assert model_plan.gate_error(task_with(rows), "build", model_secrets.status(settings)) is None


def test_gate_ignores_tasks_without_plan_other_kinds_and_other_moves(settings):
    status = model_secrets.status(settings)
    rows = model_plan.parse(DESIGN)
    assert model_plan.gate_error(task_with(None), "build", status) is None
    assert model_plan.gate_error(task_with(rows, kind="evolution"), "build", status) is None
    assert model_plan.gate_error(task_with(rows), "design", status) is None
    assert "cannot be read" in model_plan.gate_error(task_with(None, error="bad yaml"), "build", status)
    quick = task_with(rows, stage="requirements", track="quick")
    assert "M1" in model_plan.gate_error(quick, "build", status)
    assert model_plan.gate_errors(settings, task_with(None), ["build", "release"]) == {}


def test_secret_store_keeps_other_entries_and_never_returns_keys(settings):
    path = settings.provider_secrets_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"internal": {"kernel_api_token": "keep-me"},
                                "providers": {"dashscope": {"region": "cn-beijing"}}}), "utf-8")
    model_secrets.put(settings, "dashscope", "sk-secret-123", validated=False)
    data = json.loads(path.read_text("utf-8"))
    assert data["internal"]["kernel_api_token"] == "keep-me"
    assert data["providers"]["dashscope"]["region"] == "cn-beijing"
    assert data["providers"]["dashscope"]["api_key"] == "sk-secret-123"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sk-secret-123" not in json.dumps(model_secrets.status(settings))
    assert "sk-secret-123" not in json.dumps(model_plan.catalog_view(settings, task_with(model_plan.parse(DESIGN))))
    assert model_secrets.credential(settings, "dashscope").api_key.get_secret_value() == "sk-secret-123"
    model_secrets.mark_failing(settings, "dashscope", "401 from vendor")
    assert model_secrets.ready(settings, "dashscope") is False
    with pytest.raises(ValueError):
        model_secrets.put(settings, "openai", "sk-x", validated=True)   # Codex login, not a stored key
    assert model_secrets.delete(settings, "dashscope") and not model_secrets.status(settings)["dashscope"]["configured"]


@pytest.mark.parametrize("probe_result, note, stored", [
    ("valid", "(model setup: configured dashscope)", True),
    ("unverified", "(model setup: configured dashscope; the key could not be verified yet)", True),
    ("rejected", "(model setup: rejected by dashscope; the key was not saved)", False),
])
def test_setup_answer_stores_only_accepted_keys(settings, monkeypatch, probe_result, note, stored):
    monkeypatch.setattr(model_secrets, "probe", lambda vendor, key, base_url="": probe_result)
    spec = {"capability": "asr", "purpose": "daily-brief.transcribe", "recommended_vendor": "dashscope"}
    assert model_secrets.apply_setup_answer(settings, spec, "sk-abc") == note
    assert model_secrets.status(settings)["dashscope"]["configured"] is stored


def test_setup_answer_words_vendor_override_and_custom_endpoint(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(model_secrets, "probe", lambda vendor, key, base_url="": seen.append((vendor, base_url)) or "valid")
    spec = {"recommended_vendor": "dashscope"}
    assert "skipped" in model_secrets.apply_setup_answer(settings, spec, "跳过")
    assert "deferred until release" in model_secrets.apply_setup_answer(settings, spec, "later")
    assert model_secrets.apply_setup_answer(settings, spec, "deepseek: sk-ds") == "(model setup: configured deepseek)"
    assert "needs" in model_secrets.apply_setup_answer(settings, spec, "custom_openai: sk-only")
    assert "configured custom_openai" in model_secrets.apply_setup_answer(
        settings, spec, "custom_openai: https://llm.example.org/v1 sk-c")
    assert seen == [("deepseek", ""), ("custom_openai", "https://llm.example.org/v1")]
    assert model_secrets.status(settings)["dashscope"]["configured"] is False


def test_supervisor_cannot_propose_a_blocked_move_and_sees_why(settings):
    from assistant_kernel.core import machine
    from assistant_kernel.core.executors.supervisor import allowed_moves, model_gates
    task = task_with(model_plan.parse(DESIGN))
    gates = model_gates(settings, task)
    assert "build" in gates["full"]
    assert "build: not possible now (model setup missing" in allowed_moves(task, gates)
    proposal = {"decision": "propose_next", "target_stage": "build", "instruction": "build it", "_model_gates": gates}
    with pytest.raises(machine.InvalidResult, match="model setup missing"):
        machine._supervisor_item(task, {}, proposal)
    item = machine._supervisor_item(task, {}, {**proposal, "_model_gates": {}})
    assert item["content"]["target_stage"] == "build"
