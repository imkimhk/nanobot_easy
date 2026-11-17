"""Isolated integration checks using the operating provider/model and OAuth login."""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

from loguru import logger

from nanobot.agent.hook import AgentHook, SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.agent.tools.skill_decision import SkillDecisionTool
from nanobot.agent.tools.skill_search import SkillSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import load_config, set_config_path
from nanobot.config.schema import Config, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import make_provider
from scripts.improvement_eval.live_part3 import (
    AuditRegistry,
    SlowWrite,
    TimeoutWrite,
    skill_fixture,
    stats,
)
from scripts.improvement_eval.live_search_evidence import UnavailableMemory


class MeasuredProvider(LLMProvider):
    def __init__(self, inner, journal):
        super().__init__()
        self.inner, self.journal = inner, journal
        self.generation = inner.generation
        self.records = []
        self.case = ""

    def get_default_model(self):
        return self.inner.get_default_model()

    async def chat(self, **kwargs):
        if len(self.records) >= 48:
            raise RuntimeError("Integration model-call limit reached")
        row = {"case": self.case, "index": len(self.records) + 1}
        self.records.append(row)
        self.journal.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))
        started = time.monotonic()
        try:
            response = await self.inner.chat(**kwargs)
            row.update(usage=response.usage, finish_reason=response.finish_reason,
                       error_kind=response.error_kind,
                       proposed_tools=[{"name": t.name, "arguments": t.arguments} for t in response.tool_calls])
            return response
        finally:
            row["wall_s"] = round(time.monotonic() - started, 3)
            self.journal.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))


class CancelPreparedWrite(AgentHook):
    def __init__(self, queue):
        super().__init__()
        self.queue, self.injected = queue, False

    async def before_execute_tools(self, ctx):
        if not self.injected and any(c.name == "write_file" for c in ctx.tool_calls):
            self.injected = True
            await self.queue.put(InboundMessage(channel="cli", sender_id="user", chat_id="eval",
                content="취소해줘. a.md를 수정하거나 저장하지 마.", metadata={"message_id": "cancel"}))


class TransientMemory(SearchMemoryTool):
    """Fail once, then use the real scoped search backend."""

    async def execute(self, **kwargs):
        if not getattr(self, "failed_once", False):
            self.failed_once = True
            raise RuntimeError("Synthetic temporary memory index busy; retry may succeed")
        return await super().execute(**kwargs)


async def main(out, config_path, selected=None):
    out.mkdir(parents=True, exist_ok=False)
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    set_config_path(config_path)
    config = load_config(config_path)
    inner = make_provider(config)
    resolved = config.resolve_preset()
    # OAuth keeps the operating credential path and its existing inter-process lock.
    # All evaluation state/config/catalog paths are isolated separately.
    if type(inner).__name__ == "XAIGrokProvider":
        from nanobot.providers import xai_oauth

        auth_root = xai_oauth.get_data_dir()
        xai_oauth.get_data_dir = lambda: auth_root
    isolated = out / "isolated-config.json"
    isolated.write_text("{}\n")
    set_config_path(isolated)
    import nanobot.agent.tools.skill_search as search_module
    import nanobot.skill_store as store_module

    empty = out / "empty-catalog"
    empty.mkdir()
    search_module.SYSTEM_SKILLS_DIR = store_module.BUILTIN_SKILLS_DIR = empty
    provider = MeasuredProvider(inner, out / "call-journal.json")
    report = {"model": resolved.model, "provider": type(inner).__name__,
              "reasoning_effort": resolved.reasoning_effort, "call_limit": 48,
              "operating_options": {"compact": config.agents.defaults.compact_context,
                                    "lightweight": config.agents.defaults.stepwise.lightweight,
                                    "context_selection": config.agents.defaults.context_selection.enabled},
              "isolation": "synthetic workspace; restricted real local tools; no production sessions/DB",
              "cases": []}
    cases = ["baseline-math", "baseline-explain", "combined-math", "combined-explain",
             "save-timeout", "cancel-before", "cancel-during", "search-empty", "search-error",
             "search-transient", "skill-correction"]
    for key in cases:
        if selected and key not in selected:
            continue
        provider.case = key
        workspace = out / key
        workspace.mkdir()
        (workspace / "USER.md").write_text("한국어로 답변해 주세요.\n")
        registry = AuditRegistry(workspace)
        registry.register(ReadFileTool(workspace=workspace, restrict_to_workspace=True))
        writer = (TimeoutWrite if key == "save-timeout" else SlowWrite if key == "cancel-during"
                  else WriteFileTool)(workspace=workspace, restrict_to_workspace=True)
        registry.register(writer)
        if key.startswith("search"):
            search_type = (UnavailableMemory if key == "search-error" else
                           TransientMemory if key == "search-transient" else SearchMemoryTool)
            registry.register(search_type(str(workspace)))
            registry.register(ReadMemoryEventsTool(str(workspace)))
        store = skill_fixture(workspace) if key == "skill-correction" else None
        combined = not key.startswith("baseline")
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace, model=resolved.model,
                         reasoning_effort=resolved.reasoning_effort, max_iterations=9,
                         compact_context=combined, stepwise_lightweight=combined,
                         context_window_tokens=resolved.context_window_tokens,
                         tools_config=ToolsConfig(safe_mode=True), mcp_servers={},
                         restrict_to_workspace=True, unified_session=False)
        # Same controlled catalog on both sides of the token comparison.
        loop.context.skills = SkillsLoader(workspace, builtin_skills_dir=empty, include_system=False)
        session_key = "cli:eval"
        if store:
            decision = SkillDecisionTool(str(workspace))
            decision.set_context(RequestContext(channel="cli", chat_id="eval", session_key=session_key))
            await decision.execute(decision="cold", skill_name="translate", rationale="wrong choice fixture")
            registry.register(decision)
            registry.register(SkillSearchTool(str(workspace)))
            session = loop.sessions.get_or_create(session_key)
            session.add_message("assistant", "번역 스킬을 골랐지만 아직 결과는 만들지 않았습니다.")
            loop.sessions.save(session)
        prompt = ("캐시가 무엇인지 두 문장으로 설명해줘." if key.endswith("explain") else
                  "2+2는 얼마야?" if key.endswith("math") else
                  "내 여권 번호가 뭐였지? 이전 기록에서 찾아줘." if key.startswith("search") else
                  "번역 스킬을 잘못 골랐어. 표 분석 스킬로 정정하고 사과 12, 배 18, 귤 7의 합계를 계산해줘."
                  if store else "a.md에 정확히 saved content를 저장하고 다시 읽어 확인해줘.")
        if key == "cancel-before":
            (workspace / "a.md").write_text("original\n")
        capture = SDKCaptureHook()
        first = len(provider.records)
        row = {"id": key, "input": prompt, "combined_options": combined, "grading": "pending_review"}
        try:
            if key == "cancel-before":
                queue = asyncio.Queue()
                cancellation = CancelPreparedWrite(queue)
                msg = InboundMessage(channel="cli", sender_id="user", chat_id="eval",
                                     content=prompt, metadata={"message_id": key})
                response = await asyncio.wait_for(loop._process_message(
                    msg, session_key=session_key, pending_queue=queue, tools=registry,
                    hooks=[capture, cancellation]), 240)
                row["cancellation_injected"] = cancellation.injected
            elif key == "cancel-during":
                task = asyncio.create_task(loop.process_direct(prompt, session_key=session_key,
                                                               chat_id="eval", tools=registry, hooks=[capture]))
                entered = asyncio.create_task(writer.entered.wait())
                await asyncio.wait({task, entered}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                if not writer.entered.is_set():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    entered.cancel()
                    raise RuntimeError("Slow write was not entered")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                entered.cancel()
                row.update(child_settled=writer.settled, completed_write=writer.completed,
                           cancelled_note=json.loads(json.dumps(loop.sessions.get_or_create(session_key).metadata.get("task_note"))))
                registry.unregister("write_file")
                response = await asyncio.wait_for(loop.process_direct(
                    "방금 저장 작업을 취소했어. a.md의 실제 남은 상태를 읽어 알려줘. 추가 저장하지 마.",
                    session_key=session_key, chat_id="eval", tools=registry, hooks=[capture]), 240)
            else:
                response = await asyncio.wait_for(loop.process_direct(
                    prompt, session_key=session_key, chat_id="eval", tools=registry, hooks=[capture]), 240)
            row.update(text=response.content if response else None, stop_reason=capture.stop_reason,
                       metadata=loop.sessions.get_or_create(session_key).metadata)
            if key == "save-timeout":
                session = loop.sessions.get_or_create(session_key)
                stamp = (workspace / "a.md").stat().st_mtime_ns
                manager = ChannelManager(Config.model_validate({"agents": {"defaults": {"stepwise": {"lightweight": True}}},
                    "channels": {"websocket": {"enabled": False}, "sendMaxRetries": 1}}),
                    MessageBus(), session_manager=loop.sessions)
                channel = type("SyntheticChannel", (), {"send": AsyncMock(side_effect=RuntimeError("synthetic send failure"))})()
                msg = OutboundMessage(channel="telegram", chat_id="eval", content=row["text"],
                    metadata={"_task_delivery": {"session_key": session_key, "run_id": session.metadata["task_note"]["run_id"]}})
                await manager._send_with_retry(channel, msg)
                row["failed_delivery"] = session.metadata["task_note"]["delivery"]
                channel.send = AsyncMock()
                await manager._send_with_retry(channel, msg)
                row.update(final_delivery=session.metadata["task_note"]["delivery"],
                           redelivery_preserved_file=(workspace / "a.md").stat().st_mtime_ns == stamp)
            if store:
                row["skill_stats"] = stats(store)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, stop_reason="harness_or_runtime_error")
        finally:
            loop.stop()
        row.update(calls=provider.records[first:], executed_tools=registry.events)
        if (workspace / "a.md").exists():
            row["final_file"] = (workspace / "a.md").read_text()
        report["cases"].append(row)
        report["operating_config_unchanged"] = hashlib.sha256(config_path.read_bytes()).hexdigest() == config_hash
        (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"case": key, "calls": len(row["calls"]), "stop_reason": row["stop_reason"]}), flush=True)
        if row.get("error_type") or any(c.get("finish_reason") == "error" for c in row["calls"]):
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve(), args.config.resolve(), args.cases))
