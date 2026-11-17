"""Verify scoped search, unavailable/error outcomes and real earlier-result reuse."""

import argparse
import asyncio
import json
from pathlib import Path

from loguru import logger

from nanobot.agent.compact_context import CompactTools
from nanobot.agent.context import ContextBuilder
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.config.loader import set_config_path
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from scripts.improvement_eval.live_part3 import MODEL, AuditRegistry, MeasuredProvider


class UnavailableMemory(SearchMemoryTool):
    async def execute(self, **kwargs):
        raise RuntimeError("Synthetic memory index unavailable; search failed, absence unverified")


CASES = ["fresh-empty", "reuse-earlier", "tool-error", "unavailable", "compact-empty", "empty-db"]


async def main(out, selected=None):
    out.mkdir(parents=True, exist_ok=False)
    config = out / "isolated-config.json"
    config.write_text("{}\n")
    set_config_path(config)
    provider = MeasuredProvider(out / "call-journal.json")
    report = {"model": MODEL, "reasoning_effort": "medium", "cases": [],
              "isolation": "fresh synthetic DBs; no production sessions or external searches"}
    previous = None
    for key in [key for key in CASES if not selected or key in selected]:
        provider.case = key
        workspace = out / key
        workspace.mkdir()
        registry = AuditRegistry(workspace)
        scope = MemoryScope.from_runtime(owner_id="eval", workspace_id=str(workspace), agent_id=None)
        store = ConversationEventStore(workspace)
        if key != "empty-db":
            store.append_event(scope=scope, conversation_id="fixture", session_id="history",
                               actor="user", event_type="USER_MESSAGE",
                               content="배포는 금요일 오전 10시로 결정했습니다.")
        if key != "unavailable":
            for tool in [(UnavailableMemory if key == "tool-error" else SearchMemoryTool)(str(workspace)),
                         ReadMemoryEventsTool(str(workspace))]:
                tool.set_context(RequestContext(channel="cli", chat_id="eval", session_key="cli:eval"))
                registry.register(tool)
        question = "내 여권 번호가 뭐였지? 이전 기록에서 찾아줘."
        builder = ContextBuilder(workspace)
        messages = builder.build_messages(history=[], current_message=question, channel="cli")
        if key == "reuse-earlier":
            if previous is None:
                raise ValueError("reuse-earlier requires fresh-empty in the selected cases")
            # Reuse a real preceding result and original registry/scope. Never fabricate tool history.
            registry, builder, workspace, messages = previous
            question = "방금 실제로 수행한 검색 결과만으로 다시 알려줘. 새 검색은 하지 마."
            messages = [*messages, {"role": "user", "content": question}]
        tools = registry
        if key == "compact-empty":
            full = messages[0]["content"]
            messages[0] = {"role": "system", "content": builder.build_compact_system_prompt(channel="cli")}
            tools = CompactTools(registry, full, messages[0])
        start = len(provider.records)
        tool_start = len(registry.events)
        spec = AgentRunSpec(initial_messages=messages, tools=tools, model=MODEL,
                            workspace=workspace, max_iterations=5, max_tool_result_chars=6000,
                            max_tokens=4096, reasoning_effort="medium", llm_timeout_s=100)
        result = await asyncio.wait_for(AgentRunner(provider).run(spec), 180)
        row = {"id": key, "input": question, "text": result.final_content,
               "stop_reason": result.stop_reason, "tool_events": result.tool_events,
               "tool_messages": [m for m in result.messages if m.get("role") == "tool"],
               "executed_tools": registry.events[tool_start:], "calls": provider.records[start:],
               "grading": "pending_review"}
        if key == "fresh-empty":
            previous = registry, builder, workspace, result.messages
        report["cases"].append(row)
        (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"case": key, "calls": len(row["calls"]),
                          "tools": len(row["executed_tools"]), "stop_reason": result.stop_reason}), flush=True)
        if any(c.get("finish_reason") == "error" for c in row["calls"]):
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=CASES)
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve(), args.cases))
