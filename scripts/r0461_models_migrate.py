"""0.46.1 one-off: move the model settings to the one place for them, config/models.yaml (core/models.py).

- Creates assistant-data/config/models.yaml when it does not exist: one profile `strong` (codex, openai, gpt-6-astra,
  high reasoning) used by every role.
- In every project's agents/*/config.yaml, removes model / provider / reasoning_effort only where they still hold the
  old template defaults (gpt-5.6-sol, openai, medium), so those agents follow models.yaml; a value a project chose
  itself is kept and reported. Context policy and time limits stay in each agent's config. Only changed files are
  committed.

Run with the prod venv after deploying 0.46.1:
  python scripts/r0461_models_migrate.py [--dry-run]
"""
from __future__ import annotations

import argparse
import copy

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import models, projectgit

OLD = {"model": "gpt-5.6-sol", "provider": "openai", "reasoning_effort": "medium"}
MODELS_YAML = f"""# Kernel-wide model settings (core/models.py). Each value comes from the first place that sets it:
# an agent's own config.yaml (model / provider / reasoning_effort, only for a real exception), the profile it names
# (harness.profile), defaults[role], default_profile, then Codex's own default. Read on every run: no restart needed.
# Check the result in Discord with /models.
schema_version: {models.SCHEMA}
profiles:
  strong: {{engine: codex, provider: openai, model: gpt-6-astra, reasoning_effort: high}}
default_profile: strong
defaults: {{}}   # optional, e.g. {{triage: strong}}; roles: pm, supervisor, triage, or a product agent's name
"""


def clean(data: dict) -> tuple[dict, list[str]]:
    """The config without the old model defaults, and what was removed or kept."""
    data, notes = copy.deepcopy(data), []
    for holder in (data, data.get("harness")):
        if not isinstance(holder, dict):
            continue
        for key, old in OLD.items():
            if key not in holder:
                continue
            if holder[key] == old:
                holder.pop(key)
                notes.append(f"删除 {key}: {old}")
            else:
                notes.append(f"保留 {key}: {holder[key]}（项目自己设的）")
    return data, notes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    dry = parser.parse_args().dry_run
    settings = KernelSettings.from_environment()
    target = models.path(settings)
    if target.is_file():
        print(f"{target} 已存在，保留不动")
    else:
        print(f"新建 {target}：\n{MODELS_YAML}")
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(MODELS_YAML, "utf-8")
    vaults = settings.data_root / "vaults"
    for vault in sorted(p for p in vaults.iterdir() if (p / "project.yaml").is_file()):
        changed = []
        for path in sorted((vault / "agents").glob("*/config.yaml")):
            original = yaml.safe_load(path.read_text("utf-8")) or {}
            new, notes = clean(original)
            if not notes:
                continue
            print(f"{vault.name}/{path.relative_to(vault).as_posix()}：" + "；".join(notes))
            if new != original and not dry:
                path.write_text(yaml.safe_dump(new, allow_unicode=True, sort_keys=False), "utf-8")
                changed.append(path.relative_to(vault).as_posix())
        if changed:
            commit = projectgit.commit_paths(vault, changed, "模型设置改由 config/models.yaml 统一管理（Kernel 0.46.1）")
            print(f"  已提交 {vault.name}：{commit}")
    print("\n（dry-run：没有写入任何文件）" if dry else "\n完成")


if __name__ == "__main__":
    main()
