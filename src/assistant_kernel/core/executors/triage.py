"""Triage executor (docs/ROUND2_PLAN.md R2.1): a read-only first look at a PM-channel message while the
project is idle. It answers in the PM's voice, or asks Kernel to start a task or route to another project."""
from __future__ import annotations

import asyncio
from typing import Any

from assistant_kernel.core.executors.common import (
    PROFILE_HEADING, READ_ONLY_TOOLS, USER_PROFILE, call_for_json, capabilities, charter, knowledge_text, load_agent,
    project_catalog_for_run, prompt_text,
)
from assistant_kernel.core.executors.supervisor import CATALOG_HEADING, render_messages
from assistant_kernel.core.images import select_images


async def execute_triage(run: dict[str, Any], ctx) -> dict[str, Any]:
    kernel, settings = ctx.kernel, ctx.services["settings"]
    project_id = run["project_id"]
    caps = await asyncio.to_thread(capabilities, settings, project_id)
    project_kind = await kernel.project_kind(project_id)
    if project_kind == "chief_of_staff":
        caps = caps & {"project_management"}  # Chief of Staff has no routing/shared-context authority
    config = await asyncio.to_thread(load_agent, settings, project_id, "pm", purpose="triage",
                                     project_kind=project_kind)
    message = await kernel.get_message(run["input"]["message_id"])
    stable_sections = [f"# Project {project_id} · PM triage"]
    project_charter = await asyncio.to_thread(charter, settings, project_id)
    if project_charter:
        stable_sections.append("## Project charter\n\n" + project_charter)
    profile = await asyncio.to_thread(knowledge_text, settings, project_id, USER_PROFILE)
    if profile:
        stable_sections.append(PROFILE_HEADING + profile)
    stable_sections.append("## Your role in this project\n\n" + config.role_text)
    if config.skills_text:
        stable_sections.append(config.skills_text)
    sections = list(stable_sections)
    if caps & {"routing", "project_management"} or await kernel.project_kind(project_id) == "chief_of_staff":
        catalog = await project_catalog_for_run(kernel, settings, run)
        sections.append(CATALOG_HEADING + catalog)
    sections.append("## Recent conversation\n\n" + render_messages(await kernel.recent_messages(project_id)))
    context_id = run["input"].get("context_task_id")
    task = await kernel.get_task(context_id) if context_id else None
    if task:   # daily operations: the project is busy with this task (machine.user_input)
        sections.append(f"## The task the project is working on\n\n- Goal: {task['goal']}\n"
                        + (f"- Why it was started: {task['reason']}\n" if task.get("reason") else "")
                        + f"- Stage: {task['stage']}\n- State: {task['state']}\n"
                        f"- Ledger: `runs/{task['task_id']}/state.md` (read-only tools)")
        sections.append(f"## The user's new message in the PM channel (while that task is running)\n\n"
                        f"{message['body'].get('text', '')}")
        sections.append(prompt_text("triage-busy"))
    else:
        sections.append(f"## The user's new message in the PM channel (the project has no active task)\n\n"
                        f"{message['body'].get('text', '')}")
        sections.append(prompt_text("triage"))
        if "routing" in caps:
            sections.append(prompt_text("routing"))
        if "project_management" in caps:
            sections.append(prompt_text("triage-projects"))
    images, image_note, _ = select_images(await kernel.image_inputs(run))
    parsed, model_result = await call_for_json(
        ctx, run, config, "\n\n".join(sections) + image_note, task_id=None, stage=None, session_id=None,
        tools=READ_ONLY_TOOLS, sandbox="read-only", images=images,
        stable_prefix="\n\n".join(stable_sections))
    if parsed is None:
        return {"decision": "invalid_output", "raw": model_result.final_text[:2000]}
    return {**parsed, "config_sha256": config.config_sha256, "skills": list(config.skill_labels),
            "usage": model_result.usage.model_dump(), "model_metadata": model_result.metadata,
            "_capabilities": sorted(caps), "_input_images": list(images)}
