import json
import sqlite3
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.skill_decision import SkillDecisionTool
from nanobot.agent.tools.skill_search import SkillSearchTool
from nanobot.skill_store import SkillStore


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str,
    status: str = "candidate",
    category: str = "general",
    risk_level: str = "low",
    requires_exec: bool = False,
    conflicts_with: list[str] | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {json.dumps(description)}",
        "metadata:",
        "  nanobot:",
        f"    id: {name}-id",
        "    version: 1.0.0",
        f"    status: {status}",
        f"    category: {category}",
        f"    risk_level: {risk_level}",
        f"    requires_exec: {'true' if requires_exec else 'false'}",
    ]
    if conflicts_with:
        lines.append("    conflicts_with:")
        lines.extend(f"      - {item}" for item in conflicts_with)
    lines.extend(["---", "", f"# {name}", "Use this skill."])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def _index_and_approve(workspace: Path, *names: str) -> SkillStore:
    """Reindex, then run the human approval transition for the given skills.

    Workspace files always enter the registry as draft regardless of any
    self-declared frontmatter status, so tests must approve explicitly.
    """
    store = SkillStore(workspace)
    store.reindex(builtin_dir=workspace / "empty_builtin")
    for name in names:
        store.approve_draft(name)
    return store


@pytest.mark.asyncio
async def test_skill_search_batches_results_and_records_trace(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "answer-comparison",
        description='Compare alternatives. Trigger examples: "A vs B", "which is better"',
        category="answer.compare",
    )
    _write_skill(
        skills,
        "coding-fix",
        description='Fix bugs and run tests. Trigger examples: "fix this bug"',
        category="coding.fix",
        risk_level="medium",
        requires_exec=True,
    )
    _write_skill(skills, "draft-only", description="Hidden draft", status="draft")
    _index_and_approve(tmp_path, "answer-comparison", "coding-fix")

    tool = SkillSearchTool(str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))

    raw = await tool.execute(
        queries=[
            {"query": "A vs B which is better", "category": "answer.compare", "top_k": 3, "wave_no": 1},
            {
                "query": "fix this bug and run tests",
                "intent_summary": "User wants a coding bug fixed and verified with tests.",
                "category": "coding.fix",
                "top_k": 3,
            },
        ]
    )
    payload = json.loads(raw)

    assert len(payload["results"]) == 2
    first = payload["results"][0]["candidates"][0]
    second = payload["results"][1]["candidates"][0]
    assert first["name"] == "answer-comparison"
    assert "when_to_use" in first
    assert "when_not_to_use" in first
    assert first["match_grade"] in {"strong", "moderate"}
    assert second["name"] == "coding-fix"
    assert second["requires_exec"] is True
    assert "draft-only" not in raw

    with sqlite3.connect(tmp_path / ".skillstore" / "skillstore.db") as conn:
        count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        wave_no = conn.execute(
            "SELECT wave_no FROM traces WHERE wave_no IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()[0]
        intent_summary = conn.execute(
            "SELECT intent_summary FROM traces WHERE intent_summary IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()[0]
    assert count == 2
    assert wave_no == 1
    assert intent_summary == "User wants a coding bug fixed and verified with tests."


@pytest.mark.asyncio
async def test_skill_search_conflict_warning_for_close_scores(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "alpha-review",
        description="review document",
        conflicts_with=["beta-review"],
    )
    _write_skill(
        skills,
        "beta-review",
        description="review document",
        conflicts_with=["alpha-review"],
    )
    _index_and_approve(tmp_path, "alpha-review", "beta-review")

    raw = await SkillSearchTool(str(tmp_path)).execute(
        queries=[{"query": "review document", "top_k": 2}]
    )
    result = json.loads(raw)["results"][0]

    assert result["conflict_warning"]
    assert "conflict" in result["conflict_warning"]


@pytest.mark.asyncio
async def test_skill_search_uses_stats_weighting(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "stable-review", description="review document")
    _write_skill(skills, "noisy-review", description="review document")
    store = _index_and_approve(tmp_path, "stable-review", "noisy-review")
    for idx in range(5):
        store.record_skill_outcome("stable-review", gate_result="ok")
    for idx in range(5):
        store.record_skill_outcome(
            "noisy-review",
            gate_result="error",
            user_feedback="routing_failure",
        )

    raw = await SkillSearchTool(str(tmp_path)).execute(
        queries=[{"query": "review document", "top_k": 2}]
    )
    candidates = json.loads(raw)["results"][0]["candidates"]

    assert candidates[0]["name"] == "stable-review"
    assert candidates[0]["success_count"] == 5
    assert candidates[1]["routing_failure_count"] == 5


def test_skill_search_tool_is_registered_by_loader(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ctx = type("Ctx", (), {"workspace": str(tmp_path)})()

    ToolLoader().load(ctx, registry)

    assert registry.has("skill_search")
    assert registry.has("skill_decision")


@pytest.mark.asyncio
async def test_skill_decision_records_final_selection_trace(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "answer-comparison", description="Compare alternatives")
    SkillStore(tmp_path).reindex(builtin_dir=tmp_path / "empty_builtin")

    tool = SkillDecisionTool(str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))

    raw = await tool.execute(
        decision="hot",
        skill_name="answer-comparison",
        intent_summary="User wants a direct comparison between two alternatives.",
        rationale="Active Skill card matched a two-option comparison request.",
        wave_no=2,
    )
    assert json.loads(raw)["ok"] is True
    assert json.loads(raw)["phase"] == "selected"
    assert SkillStore(tmp_path).get_skill("answer-comparison")["usage_count"] == 0
    assert SkillStore(tmp_path).get_skill("answer-comparison")["selection_count"] == 1

    with sqlite3.connect(tmp_path / ".skillstore" / "skillstore.db") as conn:
        row = conn.execute(
            "SELECT selected_skill, selection_reason, intent_summary, notes, wave_no FROM traces ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert row == (
        "answer-comparison",
        "hot",
        "User wants a direct comparison between two alternatives.",
        "Active Skill card matched a two-option comparison request.",
        2,
    )


@pytest.mark.asyncio
async def test_skill_selection_correction_only_counts_actual_application(tmp_path: Path) -> None:
    for name in ("translate", "table-sum"):
        _write_skill(tmp_path / "skills", name, description=name)
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    tool = SkillDecisionTool(str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))
    first = json.loads(await tool.execute(decision="cold", skill_name="translate", rationale="wrong choice"))
    selected = json.loads(await tool.execute(decision="cold", skill_name="table-sum", rationale="corrected choice"))
    for _ in range(2):
        applied = json.loads(await tool.execute(
            decision="cold", skill_name="table-sum", rationale="used arithmetic Method",
            trace_id=selected["trace_id"], phase="applied", application_evidence="12+18+7=37"))
        assert applied["trace_id"] == selected["trace_id"]
    alpha, beta = store.get_skill("translate"), store.get_skill("table-sum")
    assert (alpha["selection_count"], alpha["usage_count"]) == (1, 0)
    assert (beta["selection_count"], beta["usage_count"], beta["success_count"]) == (1, 1, 0)
    assert first["trace_id"] != selected["trace_id"]
    store.record_trace(trace_id=selected["trace_id"], session_key="cli:direct", selected_skill="table-sum",
                       phase="applied", application_evidence="12+18+7=37", gate_result="ok")
    await tool.execute(decision="cold", skill_name="table-sum", rationale="application replay after grading",
                       trace_id=selected["trace_id"], phase="applied", application_evidence="12+18+7=37")
    assert store.get_skill("table-sum")["success_count"] == 1
    result = await tool.execute(decision="cold", skill_name="table-sum", rationale="delayed selection replay",
                                trace_id=selected["trace_id"])
    assert json.loads(result)["phase"] == "applied"
    assert store.get_skill("table-sum")["usage_count"] == 1
    # A different application remains independent, even for the same skill/session.
    await tool.execute(decision="cold", skill_name="table-sum", rationale="second real calculation",
                       phase="applied", application_evidence="2+3=5")
    assert store.get_skill("table-sum")["usage_count"] == 2


@pytest.mark.asyncio
async def test_application_requires_evidence_and_same_selection_scope(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "alpha", description="Alpha")
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    tool = SkillDecisionTool(str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="one", session_key="cli:one"))
    selected = json.loads(await tool.execute(decision="hot", skill_name="alpha", rationale="choice", wave_no=1))
    no_evidence = await tool.execute(decision="hot", skill_name="alpha", rationale="claim", phase="applied")
    assert no_evidence.is_error
    wrong_wave = await tool.execute(decision="hot", skill_name="alpha", rationale="claim", phase="applied",
                                    application_evidence="result", trace_id=selected["trace_id"], wave_no=2)
    assert wrong_wave.is_error
    token = bind_request_context(RequestContext(channel="cli", chat_id="two", session_key="cli:two"))
    try:
        wrong_session = await tool.execute(
            decision="hot", skill_name="alpha", rationale="claim", phase="applied",
            application_evidence="result", trace_id=selected["trace_id"], wave_no=1)
        assert wrong_session.is_error
    finally:
        reset_request_context(token)
    assert store.get_skill("alpha")["usage_count"] == 0
    assert store.get_skill("alpha")["selection_count"] == 1


def test_context_builder_uses_skill_search_hint_when_many_skills(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    for idx in range(31):
        _write_skill(
            skills,
            f"skill-{idx:02d}",
            description=f"Skill {idx} description",
        )

    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "skills are indexed" in prompt
    assert "Use the `skill_search` tool" in prompt
    assert "Skill 00 description" not in prompt


def test_context_builder_loads_system_skill_and_subagent_excludes_it(tmp_path: Path) -> None:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "Composite Task Orchestration" in prompt
    assert "Skill Composer" in prompt
    assert "nanobot skill approve" in prompt
    assert "Topic Memory" in prompt

    provider = type("Provider", (), {"get_default_model": lambda self: "test-model"})()
    mgr = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )

    subagent_prompt = mgr._build_subagent_prompt(workspace=tmp_path)

    assert "Composite Task Orchestration" not in subagent_prompt
    assert "composite-task" not in subagent_prompt
