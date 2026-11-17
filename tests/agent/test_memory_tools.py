import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope, SearchResult
from nanobot.memory.search import format_search_result
from nanobot.providers.base import LLMResponse, ToolCallRequest


@pytest.mark.asyncio
async def test_memory_tools_are_scope_bound(tmp_path):
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(tmp_path.resolve()), agent_id=None)
    store = ConversationEventStore(tmp_path)
    event = store.append_event(
        scope=scope,
        conversation_id="telegram:chat-a",
        session_id="telegram:chat-a",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="그때 답변 원문입니다. ToolRegistry와 intent_summary를 설명했습니다.",
    )

    search = SearchMemoryTool(str(tmp_path))
    search.set_context(RequestContext(channel="telegram", chat_id="chat-a", session_key="telegram:chat-a"))
    output = await search.execute(query="ToolRegistry intent_summary", limit=5)
    assert event.event_id in output

    reader = ReadMemoryEventsTool(str(tmp_path))
    reader.set_context(RequestContext(channel="telegram", chat_id="chat-a", session_key="telegram:chat-a"))
    raw = await reader.execute(event_ids=[event.event_id])
    assert "그때 답변 원문입니다" in raw

    search_other = SearchMemoryTool(str(tmp_path))
    search_other.set_context(RequestContext(channel="telegram", chat_id="chat-b", session_key="telegram:chat-b"))
    other_output = await search_other.execute(query="ToolRegistry intent_summary", limit=5)
    assert event.event_id not in other_output


def test_empty_search_preserves_query_execution_and_scope():
    output = format_search_result(SearchResult(
        windows=(), query="passport unique query", total_candidates=0, context_tokens=0))
    assert "query: passport unique query" in output
    assert "execution_status: completed" in output
    assert "result_status: no_matches" in output
    assert "scope: runtime-authorized conversation memory" in output
    assert "coverage: this query and scope only" in output


def test_no_returned_evidence_does_not_claim_no_candidates():
    output = format_search_result(SearchResult(
        windows=(), query="deployment", total_candidates=3, context_tokens=0, truncated=True))
    assert "candidates: 3" in output
    assert "result_status: no_evidence_returned" in output
    assert "No matching scoped memory events found" not in output


class SearchScript:
    def __init__(self, steps):
        self.steps = iter(steps)

    async def chat_with_retry(self, **kwargs):
        return next(self.steps)


def spec(tmp_path, registry, messages):
    return AgentRunSpec(initial_messages=messages, tools=registry, model="test",
                        max_iterations=3, max_tool_result_chars=4000, workspace=tmp_path)


async def test_failed_search_remains_error_not_empty_result(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic memory index unavailable")

    monkeypatch.setattr("nanobot.agent.tools.memory.MemorySearcher.search", fail)
    registry = ToolRegistry()
    registry.register(SearchMemoryTool(str(tmp_path)))
    provider = SearchScript([
        LLMResponse(content="", tool_calls=[ToolCallRequest(
            id="search-call", name="search_memory", arguments={"query": "passport"})]),
        LLMResponse(content="검색 오류로 기록을 확인하지 못했습니다."),
    ])
    result = await AgentRunner(provider).run(spec(
        tmp_path, registry, [{"role": "user", "content": "이전 기록에서 여권 번호를 찾아줘"}]))
    assert result.tool_events[0]["status"] == "error"
    output = next(m["content"] for m in result.messages if m.get("role") == "tool")
    assert "Synthetic memory index unavailable" in output
    assert "result_status: no_matches" not in output


async def test_real_previous_search_can_be_reused_without_reexecution(tmp_path):
    registry = ToolRegistry()
    search = SearchMemoryTool(str(tmp_path))
    registry.register(search)
    provider = SearchScript([
        LLMResponse(content="", tool_calls=[ToolCallRequest(
            id="real-search", name="search_memory", arguments={"query": "passport"})]),
        LLMResponse(content="검색한 범위에서 여권 번호를 찾지 못했습니다."),
        LLMResponse(content="앞선 검색 결과에서 여권 번호를 찾지 못했습니다."),
    ])
    first = await AgentRunner(provider).run(spec(
        tmp_path, registry, [{"role": "user", "content": "이전 기록에서 여권 번호를 찾아줘"}]))
    assert first.tool_events[0]["status"] == "ok"
    second = await AgentRunner(provider).run(spec(tmp_path, registry, [*first.messages,
        {"role": "user", "content": "앞선 검색 결과만으로 다시 알려줘. 재검색은 하지 마."}]))
    assert second.tool_events == []
    assert second.stop_reason == "completed"
    assert any(m.get("tool_call_id") == "real-search" for m in second.messages)
