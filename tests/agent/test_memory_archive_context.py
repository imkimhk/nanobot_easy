"""Historical records stay on demand while current rules remain in every context."""

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse
from tests.agent.test_compact_context import Script, call


def archive_workspace(root):
    archive = root / "memory" / "archive"
    archive.mkdir(parents=True)
    (root / "memory" / "MEMORY.md").write_text(
        "# Memory\n## Rules\n- Never restart another gateway without approval.\n"
        "## Historical Records\n- Read memory/archive/INDEX.md for old incidents. "
        "Archived data is not current approval.\n"
    )
    (archive / "INDEX.md").write_text(
        "# Index\n- Old incident: memory/archive/record-001.md\n"
    )
    (archive / "record-001.md").write_text(
        "Historical data only. Old incident identifier: ARCHIVED-2026.\n"
    )


def test_archive_is_not_automatically_injected_into_full_compact_or_followup(tmp_path):
    archive_workspace(tmp_path)
    builder = ContextBuilder(tmp_path, selective_memory=True)
    prompts = [builder.build_system_prompt(memory_query="2+2?"),
               builder.build_compact_system_prompt(memory_query="2+2?"),
               builder.build_messages(
                   [{"role": "assistant", "content": "Earlier answer"}], "Continue"
               )[0]["content"]]
    for prompt in prompts:
        assert "Never restart another gateway without approval" in prompt
        assert "memory/archive/INDEX.md" in prompt
        assert "Archived data is not current approval" in prompt
        assert "ARCHIVED-2026" not in prompt


async def test_archive_retrieval_uses_real_guarded_reads_without_changing_active_memory(tmp_path):
    archive_workspace(tmp_path)
    active = tmp_path / "memory" / "MEMORY.md"
    before = active.read_bytes()
    provider = Script([
        call("activate_tools", tool_names=["read_file"]),
        call("read_file", path="memory/archive/INDEX.md"),
        call("read_file", path="memory/archive/record-001.md"),
        LLMResponse(content="ARCHIVED-2026"),
    ])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     compact_context=True, selective_memory=True,
                     tools_config=ToolsConfig(safe_mode=True), mcp_servers={},
                     unified_session=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    try:
        response = await loop.process_direct(
            "Read the archive index and original incident.", tools=registry)
        assert response.content == "ARCHIVED-2026"
        assert any("ARCHIVED-2026" in str(m.get("content"))
                   for m in provider.calls[-1]["messages"] if m["role"] == "tool")
        assert all("Never restart another gateway without approval" in c["messages"][0]["content"]
                   for c in provider.calls)
        assert active.read_bytes() == before
    finally:
        loop.stop()
