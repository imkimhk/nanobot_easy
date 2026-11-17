"""Paired live token comparison and compact execution/constraint checks, isolated only."""

import argparse
import asyncio
import json
from pathlib import Path

from loguru import logger

from nanobot.agent.hook import SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import set_config_path
from nanobot.config.schema import ToolsConfig
from scripts.improvement_eval.live_part3 import MODEL, MeasuredProvider


async def main(out):
    out.mkdir(parents=True, exist_ok=False)
    config = out / "config.json"
    config.write_text("{}\n")
    set_config_path(config)
    provider = MeasuredProvider(out / "call-journal.json")
    rows = []
    cases = [("math", "2+2는 얼마야?"),
             ("explain", "캐시는 무엇인지 두 문장으로 설명해줘.")]
    for compact in (False, True):
        for name, question in cases + ([("followup", "더 친근한 말투로 바꿔줘."),
                                       ("read", "a.md를 실제로 읽고 내용을 알려줘."),
                                       ("write", "a.md에 정확히 saved content를 저장하고 다시 읽어 확인해줘.")]
                                      if compact else []):
            key = f"{'compact' if compact else 'legacy'}-{name}"
            provider.case = key
            workspace = out / key
            workspace.mkdir()
            (workspace / "a.md").write_text("LOCAL_EVIDENCE_4729", encoding="utf-8")
            # Same user constraints must survive both routes.
            (workspace / "USER.md").write_text("답변은 한국어로 해주세요.", encoding="utf-8")
            registry = ToolRegistry()
            registry.register(ReadFileTool(workspace=workspace, restrict_to_workspace=True))
            registry.register(WriteFileTool(workspace=workspace, restrict_to_workspace=True))
            loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace,
                             model=MODEL, reasoning_effort="medium", max_iterations=8,
                             compact_context=compact, tools_config=ToolsConfig(safe_mode=True),
                             mcp_servers={}, unified_session=False)
            if name == "followup":
                session = loop.sessions.get_or_create("cli:eval")
                session.add_message("user", "업무용 자료 요청 문장을 작성해줘. 한 문장만 써줘.")
                session.add_message("assistant", "자료를 송부하여 주시기 바랍니다.")
                loop.sessions.save(session)
            hook = SDKCaptureHook()
            first = len(provider.records)
            try:
                response = await asyncio.wait_for(loop.process_direct(
                    question, session_key="cli:eval", tools=registry, hooks=[hook]), 180)
                rows.append({"case": key, "question": question, "text": response.content,
                             "stop_reason": hook.stop_reason, "tool_events": hook.tool_events,
                             "calls": provider.records[first:], "file": (workspace / "a.md").read_text()})
            finally:
                loop.stop()
            (out / "results.json").write_text(json.dumps({"model": MODEL, "cases": rows},
                                                         ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"case": key, "calls": len(provider.records) - first,
                              "stop_reason": hook.stop_reason}), flush=True)
            if hook.stop_reason != "completed":
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve()))
