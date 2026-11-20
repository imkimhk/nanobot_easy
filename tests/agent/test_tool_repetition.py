"""Repeat hints must never turn legitimate read/refresh/polling into a failure."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tool_repetition import ReadRepetitionObserver, active_read_observer
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.runtime import external_lookup_signature


def test_only_unchanged_successes_warn_and_changed_range_or_content_resets():
    observer = ReadRepetitionObserver()
    args = {"path": "report.txt", "offset": 1}
    assert not observer.observe("read_file", args, "old", "ok")
    assert not observer.observe("read_file", args, "new", "ok")
    assert not observer.observe("read_file", args, "new", "ok")
    assert observer.observe("read_file", args, "new", "ok")
    assert not observer.observe("read_file", args, "new", "ok")  # one warning per streak
    assert not observer.observe("read_file", {**args, "offset": 2}, "new", "ok")
    assert not observer.observe("read_file", args, "new", "error")
    assert not observer.observe("read_file", args, "new", "ok")


@pytest.mark.parametrize("name,args", [
    ("write_stdin", {"session_id": 1}), ("exec", {"command": "check status"}),
    ("mcp_gateway_status", {}), ("read_file", {"path": "live.log", "force": True}),
])
def test_polling_mcp_and_forced_reads_never_warn(name, args):
    observer = ReadRepetitionObserver()
    assert not any(observer.observe(name, args, "running", "ok") for _ in range(10))


def test_intervening_work_resets_and_argument_order_does_not():
    observer = ReadRepetitionObserver()
    assert not observer.observe("read_file", {"path": "a", "offset": 1}, "a", "ok")
    assert not observer.observe("read_file", {"offset": 1, "path": "a"}, "a", "ok")
    assert not observer.observe("write_file", {"path": "a"}, "saved", "ok")
    assert not observer.observe("read_file", {"path": "a", "offset": 1}, "a", "ok")


def test_changed_file_version_resets_even_when_the_requested_range_is_unchanged():
    observer = ReadRepetitionObserver()
    args = {"path": "a", "limit": 1}
    result = ToolResult("1| unchanged first line")
    result.read_evidence = {"version": {"sha256": "old-version"}}
    assert not observer.observe("read_file", args, result, "ok")
    assert not observer.observe("read_file", args, result, "ok")
    result.read_evidence = {"version": {"sha256": "changed-later-lines"}}
    assert not observer.observe("read_file", args, result, "ok")


def test_size_change_resets_when_only_the_read_range_text_matches():
    observer = ReadRepetitionObserver()
    args = {"path": "notes.txt", "limit": 1}
    same = ToolResult("1| first")
    same.read_evidence = {"version": {"mtime_ns": 1, "size": 12}}
    assert not observer.observe("read_file", args, same, "ok")
    assert not observer.observe("read_file", args, same, "ok")
    changed = ToolResult("1| first")
    changed.read_evidence = {"version": {"mtime_ns": 2, "size": 18}}
    assert not observer.observe("read_file", args, changed, "ok")


def test_web_limit_distinguishes_case_and_extraction_options():
    one = external_lookup_signature("web_fetch", {"url": "https://example.com/Case"})
    assert one != external_lookup_signature("web_fetch", {"url": "https://example.com/case"})
    assert one != external_lookup_signature("web_fetch", {
        "url": "https://example.com/Case", "maxChars": 8000,
    })
    assert external_lookup_signature("web_search", {"query": "x", "count": 1}) != (
        external_lookup_signature("web_search", {"query": "x", "count": 5}))


class SameRead(Tool):
    name = "read_file"
    description = "Read the fixture."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = True

    def __init__(self):
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        result = ToolResult("1| unchanged fixture")
        result.read_evidence = {"start": 1, "end": 1, "extraction_ok": True}
        return result


async def test_runner_warns_once_keeps_evidence_and_completes_with_strict_errors():
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[*[
        LLMResponse(content="", tool_calls=[ToolCallRequest(id=str(i), name="read_file",
                                               arguments={"path": "a.txt"})])
        for i in range(4)
    ], LLMResponse(content="The fixture is unchanged.")])
    tool = SameRead()
    registry = ToolRegistry()
    registry.register(tool)
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "Verify the fixture."}],
        tools=registry, model="test-model", max_iterations=6,
        max_tool_result_chars=8000, fail_on_tool_error=True,
    ))
    assert result.stop_reason == "completed"
    assert tool.calls == 4
    assert sum(bool(e.get("repeat_warning")) for e in result.tool_events) == 1
    results = [m["content"] for m in result.messages if m["role"] == "tool"]
    assert results[2].read_evidence == {"start": 1, "end": 1, "extraction_ok": True}
    assert all(e["status"] == "ok" for e in result.tool_events)
    assert active_read_observer.get() is None


async def test_new_user_instruction_resets_run_observer():
    observer = ReadRepetitionObserver()
    args = {"path": "a.txt"}
    for _ in range(2):
        assert not observer.observe("read_file", args, "old", "ok")
    token = active_read_observer.set(observer)
    try:
        spec = AgentRunSpec(
            initial_messages=[], tools=ToolRegistry(), model="test-model", max_iterations=1,
            max_tool_result_chars=8000, injection_callback=AsyncMock(return_value=[
                {"role": "user", "content": "Please check it again."},
            ]),
        )
        await AgentRunner(MagicMock())._drain_injections(spec)
        assert not observer.observe("read_file", args, "old", "ok")
    finally:
        active_read_observer.reset(token)


async def test_read_without_task_or_context_capture_notices_a_later_line_change(tmp_path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    path = tmp_path / "notes.txt"
    path.write_text("first\nsecond\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path, restrict_to_workspace=True)
    args = {"path": str(path), "offset": 1, "limit": 1}
    first = await tool.execute(path=str(path), offset=1, limit=1, force=True)
    assert "first" in first and "second" not in first
    assert first.read_evidence["version"]["size"] == path.stat().st_size
    observer = ReadRepetitionObserver()
    assert not observer.observe("read_file", args, first, "ok")
    assert not observer.observe("read_file", args, first, "ok")
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")
    second = await tool.execute(path=str(path), offset=1, limit=1, force=True)
    assert "first" in second and "third" not in second
    assert second.read_evidence["version"]["size"] != first.read_evidence["version"]["size"]
    assert not observer.observe("read_file", args, second, "ok")
