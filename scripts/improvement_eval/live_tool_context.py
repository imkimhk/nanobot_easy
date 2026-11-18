"""Measure tool-schema costs with operating policy copies and isolated file work."""

import argparse
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

from loguru import logger

from nanobot.agent.hook import SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config, set_config_path
from nanobot.providers.factory import make_provider
from nanobot.utils.helpers import estimate_prompt_tokens
from scripts.improvement_eval.live_integration import MeasuredProvider
from scripts.improvement_eval.live_part3 import AuditRegistry


class SchemaOnlyTool(Tool):
    """Expose the operating schema while refusing unrelated evaluation actions."""

    def __init__(self, schema):
        self.schema = schema["function"]

    @property
    def name(self):
        return self.schema["name"]

    @property
    def description(self):
        return self.schema["description"]

    @property
    def parameters(self):
        return self.schema["parameters"]

    async def execute(self, **kwargs):
        return ToolResult.error("Evaluation fixture: only the requested file read/write may execute.")


class ProfiledProvider(MeasuredProvider):
    async def chat(self, **kwargs):
        messages, tools = kwargs["messages"], kwargs.get("tools") or []
        # Counts only; policy text and full request payloads are never journalled.
        groups = {"system": [], "user": [], "assistant": [], "tool": []}
        for message in messages:
            groups.setdefault(message["role"], []).append(message)
        profile = {role: estimate_prompt_tokens(rows) for role, rows in groups.items()}
        profile.update(tool_schemas=estimate_prompt_tokens([], tools),
                       tool_count=len(tools),
                       tool_names=[d.get("function", d).get("name") for d in tools])
        try:
            return await super().chat(**kwargs)
        finally:
            self.records[-1]["local_estimate"] = profile
            self.journal.write_text(json.dumps(self.records, ensure_ascii=False, indent=2) + "\n")


async def main(out, config_path, policy_workspace=None, selective_memory=False,
               operating_style_path=False, selected_cases=None, memory_prompt=None,
               history_prompt=None):
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    set_config_path(config_path)
    config = load_config(config_path)
    inner = make_provider(config)
    if type(inner).__name__ == "XAIGrokProvider":
        from nanobot.providers import xai_oauth

        auth_root = xai_oauth.get_data_dir()
        xai_oauth.get_data_dir = lambda: auth_root
    source = policy_workspace or config.workspace_path
    workspace = out / "workspace"
    workspace.mkdir()
    paths = [Path(p) for p in ["AGENTS.md", "SOUL.md", "USER.md", "memory/MEMORY.md"]]
    if (source / "memory/archive").exists():
        paths.extend(p.relative_to(source) for p in (source / "memory/archive").rglob("*.md"))
    if (source / "skills").exists():
        paths.extend(p.relative_to(source) for p in (source / "skills").rglob("SKILL.md"))
    for relative in paths:
        if (source / relative).is_file():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
    isolated = out / "isolated-config.json"
    isolated.write_text("{}\n")
    set_config_path(isolated)
    provider = ProfiledProvider(inner, out / "call-journal.json")
    resolved = config.resolve_preset()
    defaults = config.agents.defaults
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace,
                     model=resolved.model, reasoning_effort=resolved.reasoning_effort,
                     context_window_tokens=resolved.context_window_tokens,
                     compact_context=True, stepwise_lightweight=True, max_iterations=9,
                     selective_memory=selective_memory,
                     tools_config=config.tools, mcp_servers={}, restrict_to_workspace=True,
                     hot_path_skills=defaults.skills, disabled_skills=defaults.disabled_skills,
                     unified_session=False)
    registry = AuditRegistry(workspace)
    for schema in loop.tools.get_definitions():
        name = schema["function"]["name"]
        if name == "read_file":
            tool = ReadFileTool(workspace=workspace, restrict_to_workspace=True)
        elif name == "write_file":
            tool = WriteFileTool(workspace=workspace, restrict_to_workspace=True)
        else:
            tool = SchemaOnlyTool(schema)
        registry.register(tool)
    results = []
    target = workspace / "local/workspace/selection-check.txt"
    write_prompt = (
        f"운영 검증용 파일 {target}에 정확히 DEPLOYMENT_OK 문자열만 저장하고 다시 읽어 확인해줘. 다른 작업은 하지 마."
        if operating_style_path else
        "a.md에 정확히 saved content 문자열만 저장하고 다시 읽어 확인해줘. 다른 작업은 하지 마."
    )
    prompts = [
        ("math", "2+2는 얼마야? 숫자만 답해줘."),
        ("write", write_prompt),
    ]
    if memory_prompt:
        prompts.extend([("memory", memory_prompt), ("repeat", "이전 답변을 그대로 다시 보여주세요.")])
    if history_prompt:
        prompts.append(("history", history_prompt))
    if selected_cases is None:
        selected_cases = {"math", "write"}
    try:
        for name, prompt in prompts:
            if name not in selected_cases:
                continue
            provider.case = name
            capture = SDKCaptureHook()
            start, tool_start = len(provider.records), len(registry.events)
            session_key = "cli:cost-memory" if name == "repeat" else f"cli:cost-{name}"
            response = await asyncio.wait_for(loop.process_direct(
                prompt, tools=registry, session_key=session_key, hooks=[capture]), 240)
            session = loop.sessions.get_or_create(session_key)
            artifact = target if operating_style_path else workspace / "a.md"
            row = {"id": name, "prompt": prompt, "text": response.content, "stop_reason": capture.stop_reason,
                   "calls": provider.records[start:], "executed_tools": registry.events[tool_start:],
                   "task_note": session.metadata.get("task_note"),
                   "file": artifact.read_text() if artifact.exists() else None}
            results.append(row)
            report = {"model": resolved.model, "cases": results,
                      "operating_config_unchanged": hashlib.sha256(config_path.read_bytes()).hexdigest() == config_hash,
                      "scope": "Copies of operating bootstrap/memory/skill instructions; core schemas, no MCP. Only real isolated read/write execute. Local estimates are not vendor counts."}
            (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"id": name, "calls": len(row["calls"]), "stop_reason": row["stop_reason"]}), flush=True)
    finally:
        loop.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--policy-workspace", type=Path,
                        help="Reuse a frozen policy snapshot for paired evaluations.")
    parser.add_argument("--selective-memory", action="store_true")
    parser.add_argument("--operating-style-path", action="store_true",
                        help="Use the production smoke wording and an isolated path containing local/workspace.")
    parser.add_argument("--cases", nargs="+", choices=["math", "write", "memory", "repeat", "history"])
    parser.add_argument("--memory-prompt-file", type=Path,
                        help="Private UTF-8 question about facts in the copied memory.")
    parser.add_argument("--history-prompt-file", type=Path,
                        help="Private UTF-8 question requiring archived record retrieval.")
    args = parser.parse_args()
    if args.cases and {"memory", "repeat"} & set(args.cases) and not args.memory_prompt_file:
        parser.error("memory/repeat cases require --memory-prompt-file")
    if args.cases and "repeat" in args.cases and "memory" not in args.cases:
        parser.error("repeat requires the memory case in the same run")
    if args.cases and "history" in args.cases and not args.history_prompt_file:
        parser.error("history requires --history-prompt-file")
    memory_prompt = args.memory_prompt_file.read_text().strip() if args.memory_prompt_file else None
    if args.memory_prompt_file and not memory_prompt:
        parser.error("memory prompt must not be empty")
    history_prompt = args.history_prompt_file.read_text().strip() if args.history_prompt_file else None
    if args.history_prompt_file and not history_prompt:
        parser.error("history prompt must not be empty")
    logger.remove()
    asyncio.run(main(args.out.resolve(), args.config.resolve(),
                     args.policy_workspace.resolve() if args.policy_workspace else None,
                     args.selective_memory, args.operating_style_path, args.cases, memory_prompt,
                     history_prompt))
